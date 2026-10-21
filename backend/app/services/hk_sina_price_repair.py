"""Repair HK Yahoo price frames whose latest session is missing, using Sina.

Yahoo's daily history for HK often lacks the most recent completed session for
hours (or days): the bar is absent or carries NaN OHLC, as seen for ``^HSI`` and
``2800.HK`` on 2026-09-28. Yahoo stays the primary HK source because it is
batched and dividend/split adjusted. Sina (via akshare) is only asked for the
recent bars Yahoo is missing, and those bars are appended to the Yahoo frame.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

import pandas as pd

from .price_row_normalization import drop_non_finite_close_rows
from .price_value_policy import is_usable_adjusted_close

logger = logging.getLogger(__name__)

SINA_HK_TIMEOUT_SECONDS = 20
# Stop calling Sina after this many consecutive failures so an outage or a
# block from CI runners costs a few timeouts, not one per HK symbol.
SINA_HK_MAX_CONSECUTIVE_FAILURES = 10
# ponytail: process-wide breaker because callers repair one 50-symbol batch at
# a time; a success resets it. Per-run state if repair ever runs concurrently.
_breaker = {"consecutive_failures": 0}

_SINA_HK_INDEX_CODES = {"^HSI": "HSI"}

RecentBarsFetcher = Callable[[str], pd.DataFrame | None]


def fetch_sina_hk_bars(symbol: str) -> pd.DataFrame | None:
    """Return unadjusted Sina daily OHLCV for an HK stock (``0700.HK``) or ``^HSI``."""
    import akshare as ak

    from .cn_market_data_service import _call_with_timeout

    index_code = _SINA_HK_INDEX_CODES.get(symbol.upper())
    if index_code is not None:
        fetch = lambda: ak.stock_hk_index_daily_sina(symbol=index_code)  # noqa: E731
    elif symbol.upper().endswith(".HK"):
        code = symbol.upper()[:-3].zfill(5)
        fetch = lambda: ak.stock_hk_daily(symbol=code, adjust="")  # noqa: E731
    else:
        return None

    raw = _call_with_timeout(
        fetch,
        timeout_seconds=SINA_HK_TIMEOUT_SECONDS,
        operation_name=f"Sina HK daily bars for {symbol}",
    )
    if raw is None or raw.empty:
        return None
    frame = raw.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop("date")), name="Date")
    return frame[["Open", "High", "Low", "Close", "Volume"]].sort_index()


def _stale_anchor(
    payload: Mapping[str, Any], expected_ts: pd.Timestamp
) -> tuple[pd.DataFrame, pd.DatetimeIndex, pd.Series, pd.Timestamp] | None:
    """Return ``(valid, valid_dates, anchor_row, anchor_ts)`` when the frame lacks ``expected_ts``.

    Only frames Yahoo returned history for qualify: that history anchors the
    adjusted-close ratio.
    """
    price_data = payload.get("price_data")
    if payload.get("has_error") or price_data is None or price_data.empty:
        return None
    valid = drop_non_finite_close_rows(price_data)
    if valid is None or valid.empty:
        return None
    valid_dates = valid.index.normalize()
    if valid.index.tz is not None:
        valid_dates = valid_dates.tz_localize(None)
    if expected_ts in valid_dates:
        return None
    before_expected = valid_dates < expected_ts
    if not before_expected.any():
        return None
    return valid, valid_dates, valid[before_expected].iloc[-1], valid_dates[before_expected].max()


def stale_symbols(results: Mapping[str, dict[str, Any]], *, expected_session: date) -> list[str]:
    """Symbols :func:`repair_missing_latest_sessions` would try to repair."""
    expected_ts = pd.Timestamp(expected_session)
    return [symbol for symbol, payload in results.items() if _stale_anchor(payload, expected_ts)]


def repair_missing_latest_sessions(
    results: Mapping[str, dict[str, Any]],
    *,
    expected_session: date,
    fetch_recent: RecentBarsFetcher = fetch_sina_hk_bars,
    source: str = "sina",
    max_consecutive_failures: int | None = SINA_HK_MAX_CONSECUTIVE_FAILURES,
) -> dict[str, int]:
    """Fill ``fetch_recent`` bars into Yahoo frames lacking a valid ``expected_session`` bar.

    The hole can be at the end, or interior once Yahoo publishes the next
    (intraday) bar while still missing the completed one. Fetched bars after
    the last valid Yahoo bar before ``expected_session``, up to that session,
    fill any date Yahoo lacks.

    Mutates ``results`` in place. Only symbols Yahoo returned history for are
    repaired: their Yahoo history anchors the adjusted-close ratio. Filled bars
    get ``Adj Close = Close * (anchor Adj Close / Close)``, or ``Close`` itself
    when the anchor's Adj Close is missing or not positive (a stored NULL or 0
    would otherwise give the bar an Adj Close of 0, which Market RS drops);
    Yahoo's next full refetch replaces them with its own adjusted values.

    ``max_consecutive_failures=None`` bypasses the process-wide breaker, for
    fetchers that already hold their bars (a per-symbol miss is not an outage).
    """
    stats = {"stale": 0, "repaired": 0, "failed": 0, "skipped": 0}
    expected_ts = pd.Timestamp(expected_session)
    use_breaker = max_consecutive_failures is not None
    for symbol, payload in results.items():
        stale = _stale_anchor(payload, expected_ts)
        if stale is None:
            continue
        valid, valid_dates, anchor_row, anchor_ts = stale

        stats["stale"] += 1
        if use_breaker and _breaker["consecutive_failures"] >= max_consecutive_failures:
            stats["skipped"] += 1
            continue
        try:
            fetched = fetch_recent(symbol)
        except Exception as exc:  # pragma: no cover - provider/network variability
            logger.warning("%s price repair failed for %s: %s", source, symbol, exc)
            fetched = None
        if fetched is not None and not fetched.empty:
            fetched = drop_non_finite_close_rows(fetched)
        if fetched is None or fetched.empty:
            if use_breaker:
                _breaker["consecutive_failures"] += 1
            stats["failed"] += 1
            continue
        if use_breaker:
            _breaker["consecutive_failures"] = 0

        session_dates = fetched.index.normalize()
        missing = fetched[
            (session_dates > anchor_ts)
            & (session_dates <= expected_ts)
            & ~session_dates.isin(valid_dates)
        ].copy()
        if missing.empty:
            stats["failed"] += 1
            continue

        adj_ratio = 1.0
        if (
            "Adj Close" in valid.columns
            and anchor_row["Close"]
            and is_usable_adjusted_close(anchor_row["Adj Close"])
        ):
            adj_ratio = float(anchor_row["Adj Close"]) / float(anchor_row["Close"])
        missing["Adj Close"] = missing["Close"] * adj_ratio
        if valid.index.tz is not None:
            missing.index = missing.index.tz_localize(valid.index.tz)
        # Yahoo frames also carry action columns (Dividends, Stock Splits).
        missing = missing.reindex(columns=valid.columns).fillna(0.0)
        # Stored rows read back with an all-NULL column (adj_close can be)
        # come as object None; concat would warn about inferring its dtype.
        all_null = valid.columns[valid.isna().all().to_numpy()]
        if len(all_null):
            valid = valid.astype({column: "float64" for column in all_null})

        payload["price_data"] = pd.concat([valid, missing]).sort_index()
        payload["repaired_by"] = source
        payload["repaired_sessions"] = [ts.date().isoformat() for ts in missing.index]
        stats["repaired"] += 1
    return stats
