"""Static-site daily price refresh orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable

import pandas as pd

from app.domain.markets.key_markets import key_market_price_symbols
from app.domain.markets.registry import critical_price_symbols
from app.domain.providers.data_plan import (
    DATASET_PRICES,
    PROVIDER_YAHOO_QUOTE,
    provider_data_plan_registry,
)
from app.domain.providers.price_symbol_support import split_supported_price_symbols
from app.models.stock import StockPrice
from app.models.stock_universe import StockUniverse
from app.services.price_row_normalization import (
    drop_non_finite_close_rows,
    stock_price_row_from_ohlcv,
)
from app.services.stock_price_persistence import persist_stock_price_mappings
from app.services.yahoo_quote_price_repair import YAHOO_QUOTE_RETRY_BACKOFF_SECONDS
from app.services.breadth_history_price_coverage import (
    BreadthHistoryPriceCoverageService,
    DEFAULT_BREADTH_HISTORY_PRICE_LOOKBACK_DAYS,
)
from app.services.bulk_data_fetcher import BulkDataFetcher
from app.services.group_history_price_coverage import (
    GroupHistoryPriceCoverageService,
)
from app.services.market_calendar_service import MarketCalendarService
from app.services.price_history_coverage import classify_price_history
from app.services.price_refresh_planning import (
    NO_HISTORY_PRICE_BOOTSTRAP_PERIOD,
    STALE_PRICE_TOP_UP_PERIOD,
)
from app.services.rs_anchor_price_coverage import (
    RS_ANCHOR_LOOKAHEAD_SESSIONS,
    RsAnchorGaps,
    RsAnchorPriceCoverageService,
)


STATIC_DAILY_PRICE_REFRESH_PERIOD = STALE_PRICE_TOP_UP_PERIOD
STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD = NO_HISTORY_PRICE_BOOTSTRAP_PERIOD
STATIC_DAILY_PRICE_REFRESH_BATCH_SIZE = 250
# Calendar days at the end of the window that the 7d top-up always refetches
# (7d minus margin for delayed runs). Group-history anchors in this tail are
# left to the top-up instead of forcing a full 2y bootstrap.
STATIC_DAILY_PRICE_TOP_UP_TAIL_DAYS = 4
# A top-up bar whose Adj Close differs from the stored bar for the same date by
# more than this means Yahoo back-adjusted history (split or dividend); splicing
# the top-up onto the old series would leave a false jump, so refetch 2y.
STATIC_ADJUSTMENT_DRIFT_TOLERANCE = 1e-3

# Markets where Yahoo's 429 backoff windows are long enough that a single
# refresh pass routinely leaves a tail of rate-limited symbols. For these
# markets we wait ``STATIC_RATE_LIMITED_RETRY_WAIT_SECONDS`` after the main
# loop and replay only the symbols whose failure looks transient, in a
# smaller batch (``STATIC_RATE_LIMITED_RETRY_BATCH_SIZE``).
STATIC_RATE_LIMITED_RETRY_MARKETS = frozenset({"IN"})
STATIC_RATE_LIMITED_RETRY_WAIT_SECONDS = 300
STATIC_RATE_LIMITED_RETRY_BATCH_SIZE = 25

# The per-batch Yahoo quote repair drops a whole 100-symbol batch when its
# retries are rate-limited (three batches, 294 US symbols, on 2026-10-02, #478).
# After the refresh, symbols still stored without the as-of session get one more
# quote repair once Yahoo's 429 burst has cleared (about a minute).
STATIC_SESSION_REPAIR_WAIT_SECONDS = 60
RS_ANCHOR_UNRESOLVED_SAMPLE_LIMIT = 20


class PriceStageDeadlineReached(Exception):
    """The price stage must stop so the run can checkpoint before its timeout."""


@dataclass(frozen=True)
class _StaticHistoryCoverageOutcome:
    incomplete_symbols: tuple[str, ...]
    status: str
    error: str | None = None
    required_dates: int = 0
    bootstrap_symbols: tuple[str, ...] | None = None
    missing_through_date_symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class _RsAnchorCoverage:
    status: str
    history_gaps: RsAnchorGaps = RsAnchorGaps()
    tail_gap_symbols: tuple[str, ...] = ()
    history_dates: frozenset[date] = frozenset()
    error: str | None = None


def _history_bootstrap_symbols(
    outcome: _StaticHistoryCoverageOutcome,
) -> tuple[str, ...]:
    return (
        outcome.incomplete_symbols
        if outcome.bootstrap_symbols is None
        else outcome.bootstrap_symbols
    )


def static_daily_price_refresh_batch_size(market: str | None) -> int:
    if market:
        from app.services.rate_budget_policy import get_rate_budget_policy

        return get_rate_budget_policy().get_batch_size("yfinance", market)
    return STATIC_DAILY_PRICE_REFRESH_BATCH_SIZE


def _iter_chunks(items: list[str], chunk_size: int) -> list[list[str]]:
    return [items[index:index + chunk_size] for index in range(0, len(items), chunk_size)]


def _is_rate_limit_error(error: str) -> bool:
    error = error.lower()
    indicators = ("rate", "429", "too many", "limit", "throttl")
    return any(token in error for token in indicators)


def _rate_limited_symbols(failures: dict[str, str]) -> list[str]:
    return [symbol for symbol, error in failures.items() if _is_rate_limit_error(error)]


def _key_market_price_symbols(market: str | None) -> list[str]:
    return list(key_market_price_symbols(market))


def _frame_price_rows(symbol: str, frame: pd.DataFrame) -> list[dict[str, Any]]:
    rows = (
        stock_price_row_from_ohlcv(
            symbol=symbol, row_date=pd.Timestamp(stamp).date(), row=row
        )
        for stamp, row in frame.iterrows()
    )
    return [row for row in rows if row is not None]


def _has_session(frame: pd.DataFrame, session: date) -> bool:
    valid = drop_non_finite_close_rows(frame)
    return valid is not None and any(pd.Timestamp(stamp).date() == session for stamp in valid.index)


def _dedupe_symbols(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in symbols:
        symbol = str(raw or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        result.append(symbol)
    return result


class StaticDailyPriceRefreshService:
    """Refresh price rows needed by the static-site snapshot build."""

    def __init__(
        self,
        *,
        session_factory,
        price_cache,
        fetcher: BulkDataFetcher,
        batch_size_for_market: Callable[[str | None], int] = static_daily_price_refresh_batch_size,
        calendar_service: MarketCalendarService | None = None,
        group_history_price_coverage: GroupHistoryPriceCoverageService | None = None,
        breadth_history_price_coverage: BreadthHistoryPriceCoverageService | None = None,
        breadth_history_price_lookback_days: int = (
            DEFAULT_BREADTH_HISTORY_PRICE_LOOKBACK_DAYS
        ),
        sleep: Callable[[float], None] | None = None,
        fetch_quotes: Callable[[list[str]], list[dict[str, Any]]] | None = None,
        rs_anchor_price_coverage: RsAnchorPriceCoverageService | None = None,
        deadline: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """``deadline`` is a ``clock()`` (default ``time.monotonic``) value after
        which no provider batch or wait starts; ``refresh`` then returns
        ``status: "resumable"`` with every finished batch already committed.
        """
        self._session_factory = session_factory
        self._deadline = deadline
        if clock is None:
            import time

            clock = time.monotonic
        self._clock = clock
        self._fetch_quotes = fetch_quotes
        self._price_cache = price_cache
        self._fetcher = fetcher
        self._batch_size_for_market = batch_size_for_market
        self._calendar_service = calendar_service or MarketCalendarService()
        self._group_history_price_coverage = (
            group_history_price_coverage
            or GroupHistoryPriceCoverageService(
                calendar_service=self._calendar_service
            )
        )
        self._breadth_history_price_coverage = (
            breadth_history_price_coverage
            or BreadthHistoryPriceCoverageService(
                calendar_service=self._calendar_service,
                lookback_days=breadth_history_price_lookback_days,
            )
        )
        self._rs_anchor_price_coverage = (
            rs_anchor_price_coverage
            or RsAnchorPriceCoverageService(calendar_service=self._calendar_service)
        )
        if sleep is None:
            import time

            sleep = time.sleep
        self._sleep = sleep

    def refresh(
        self,
        *,
        as_of_date: date,
        market: str | None = None,
        ensure_static_history: bool = False,
        rs_anchor_lookahead_sessions: int = RS_ANCHOR_LOOKAHEAD_SESSIONS,
    ) -> dict[str, Any]:
        try:
            return self._refresh(
                as_of_date=as_of_date,
                market=market,
                ensure_static_history=ensure_static_history,
                rs_anchor_lookahead_sessions=rs_anchor_lookahead_sessions,
            )
        except PriceStageDeadlineReached:
            print(
                f"[static-daily prices] Price stage deadline reached for {market} "
                f"{as_of_date}; stopping with every finished batch committed.",
                flush=True,
            )
            return {
                "status": "resumable",
                "reason": "price_stage_deadline",
                "market": market,
                "as_of_date": as_of_date.isoformat(),
            }

    def _check_deadline(self, wait_seconds: float = 0.0) -> None:
        """Raise if a fetch, or a ``wait_seconds`` wait, would start past the deadline."""
        if self._deadline is not None and self._clock() + wait_seconds >= self._deadline:
            raise PriceStageDeadlineReached

    def _seconds_until_deadline(self) -> float | None:
        """Seconds left before the deadline (negative once past it); ``None`` without one."""
        if self._deadline is None:
            return None
        return self._deadline - self._clock()

    def _refresh(
        self,
        *,
        as_of_date: date,
        market: str | None,
        ensure_static_history: bool,
        rs_anchor_lookahead_sessions: int,
    ) -> dict[str, Any]:
        with self._session_factory() as db:
            query = (
                db.query(StockUniverse.symbol)
                .filter(StockUniverse.is_active.is_(True))
                .order_by(StockUniverse.market_cap.desc().nullslast(), StockUniverse.symbol.asc())
            )
            if market is not None:
                query = query.filter(StockUniverse.market == market)
            active_symbols = [symbol for symbol, in query.all()]
            key_market_symbols = _key_market_price_symbols(market)
            critical_symbols = list(critical_price_symbols(market))
            refresh_candidates = _dedupe_symbols(
                active_symbols + key_market_symbols + critical_symbols
            )
            supported_symbols, skipped_symbols = split_supported_price_symbols(refresh_candidates)
            active_symbol_set = set(_dedupe_symbols(active_symbols))
            volume_required_symbols = [
                symbol for symbol in supported_symbols
                if symbol in active_symbol_set
            ]
            coverage = classify_price_history(
                db,
                symbols=supported_symbols,
                as_of_date=as_of_date,
                symbols_requiring_positive_volume=volume_required_symbols,
            )
            rrg_history_coverage = self._rrg_history_coverage(
                db,
                market=market,
                through_date=as_of_date,
                symbols=coverage.fresh + coverage.stale,
                enabled=ensure_static_history,
            )
            breadth_history_coverage = self._breadth_history_coverage(
                db,
                market=market,
                through_date=as_of_date,
                symbols=tuple(
                    symbol
                    for symbol in coverage.fresh + coverage.stale
                    if symbol in active_symbol_set
                ),
                enabled=ensure_static_history,
            )
            # Benchmarks too: a benchmark hole fails RS for the whole market.
            rs_anchor_symbol_set = (
                active_symbol_set | set(key_market_symbols) | set(critical_symbols)
            )
            rs_anchor_coverage = self._rs_anchor_coverage(
                db,
                market=market,
                through_date=as_of_date,
                symbols=tuple(
                    symbol
                    for symbol in coverage.fresh + coverage.stale
                    if symbol in rs_anchor_symbol_set
                ),
                enabled=ensure_static_history,
                lookahead_sessions=rs_anchor_lookahead_sessions,
            )

        rrg_history_incomplete_symbols = list(rrg_history_coverage.incomplete_symbols)
        rrg_history_tail_gap_symbols = list(
            rrg_history_coverage.missing_through_date_symbols
        )
        breadth_history_incomplete_symbols = list(
            breadth_history_coverage.incomplete_symbols
        )
        breadth_history_bootstrap_symbols = list(
            _history_bootstrap_symbols(breadth_history_coverage)
        )
        breadth_history_missing_through_date_symbols = list(
            breadth_history_coverage.missing_through_date_symbols
        )
        history_incomplete_symbols = _dedupe_symbols(
            [
                *rrg_history_incomplete_symbols,
                *breadth_history_bootstrap_symbols,
            ]
        )
        db_fresh_symbols = list(coverage.fresh)
        history_incomplete_symbol_set = set(history_incomplete_symbols)
        stale_symbols = [
            symbol
            for symbol in _dedupe_symbols(
                [
                    *coverage.stale,
                    *breadth_history_missing_through_date_symbols,
                    *rrg_history_tail_gap_symbols,
                    *rs_anchor_coverage.tail_gap_symbols,
                ]
            )
            if symbol not in history_incomplete_symbol_set
        ]
        no_history_symbols = list(coverage.no_history)
        bootstrap_symbols = _dedupe_symbols(
            [*history_incomplete_symbols, *no_history_symbols]
        )
        rs_anchor_repair_symbols = list(
            rs_anchor_coverage.history_gaps.missing_dates_by_symbol
        )

        if not stale_symbols and not bootstrap_symbols and not rs_anchor_repair_symbols:
            print(
                f"[static-daily prices] Database already has fresh price rows for "
                f"{len(db_fresh_symbols):,} supported symbols as of {as_of_date}.",
                flush=True,
            )
            return {
                "status": "skipped",
                "market": market,
                "as_of_date": as_of_date.isoformat(),
                "total_active_symbols": len(active_symbols),
                "supported_symbols": len(supported_symbols),
                "key_market_symbols": len(key_market_symbols),
                "db_fresh_symbols": len(db_fresh_symbols),
                "stale_symbols": len(stale_symbols),
                "no_history_symbols": len(no_history_symbols),
                "history_incomplete_symbols": len(history_incomplete_symbols),
                "rrg_history_incomplete_symbols": len(
                    rrg_history_incomplete_symbols
                ),
                "breadth_history_incomplete_symbols": len(
                    breadth_history_incomplete_symbols
                ),
                "breadth_history_bootstrap_symbols": len(
                    breadth_history_bootstrap_symbols
                ),
                "breadth_history_missing_through_date_symbols": len(
                    breadth_history_missing_through_date_symbols
                ),
                "rrg_history_coverage_status": rrg_history_coverage.status,
                "rrg_history_coverage_error": rrg_history_coverage.error,
                "breadth_history_coverage_status": breadth_history_coverage.status,
                "breadth_history_coverage_error": breadth_history_coverage.error,
                "breadth_history_required_dates": (
                    breadth_history_coverage.required_dates
                ),
                "skipped_unsupported_symbols": len(skipped_symbols),
                "yahoo_fetched_symbols": 0,
                "yahoo_failed_symbols": 0,
                "rs_anchor_repair": self._rs_anchor_repair_stats(rs_anchor_coverage),
            }

        batch_size = self._batch_size_for_market(market)
        # Critical symbols go first, as their own small calls, so a throttled
        # one meets the fetcher's backoff at once instead of behind the
        # universe (SPY sat in batch 52/55 on 2026-10-20 and the US site
        # stayed a day behind).
        critical_symbol_set = set(critical_symbols)
        stale_symbol_set = set(stale_symbols)
        bootstrap_symbol_set = set(bootstrap_symbols)
        fetch_groups = [
            (symbols, period)
            for symbols, period in (
                (
                    [s for s in critical_symbols if s in stale_symbol_set],
                    STATIC_DAILY_PRICE_REFRESH_PERIOD,
                ),
                (
                    [s for s in critical_symbols if s in bootstrap_symbol_set],
                    STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
                ),
                (
                    [s for s in stale_symbols if s not in critical_symbol_set],
                    STATIC_DAILY_PRICE_REFRESH_PERIOD,
                ),
                (
                    [s for s in bootstrap_symbols if s not in critical_symbol_set],
                    STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
                ),
            )
            if symbols
        ]
        total_batches = sum(
            (len(symbols) + batch_size - 1) // batch_size for symbols, _ in fetch_groups
        )

        print(
            f"[static-daily prices] Refreshing {len(stale_symbols):,} stale and "
            f"{len(no_history_symbols):,} no-history symbols in {total_batches} batches for {as_of_date} "
            f"(DB fresh: {len(db_fresh_symbols):,}, unsupported skipped: {len(skipped_symbols):,}).",
            flush=True,
        )
        if history_incomplete_symbols:
            print(
                f"[static-daily prices] Hydrating {len(history_incomplete_symbols):,} "
                "symbols with short history for static backfills.",
                flush=True,
            )
        if rrg_history_incomplete_symbols:
            print(
                f"[static-daily prices] RRG startup history is short for "
                f"{len(rrg_history_incomplete_symbols):,} symbols.",
                flush=True,
            )
        if breadth_history_bootstrap_symbols:
            print(
                f"[static-daily prices] Breadth/exposure history is short for "
                f"{len(breadth_history_bootstrap_symbols):,} symbols.",
                flush=True,
            )
        if breadth_history_missing_through_date_symbols:
            print(
                "[static-daily prices] Breadth/exposure current session is "
                f"missing for {len(breadth_history_missing_through_date_symbols):,} "
                "symbols; using stale top-up.",
                flush=True,
            )

        # Symbol -> dates of its discarded, drift-triggering top-up frame; the
        # replacement must cover them too (e.g. the new as-of bar).
        readjusted_symbols: dict[str, set[date]] = {}
        # Symbol -> its stored frame, while that frame lacks the as-of session.
        missing_session_frames: dict[str, pd.DataFrame] = {}
        # Every symbol some pass has stored.
        stored_symbols: set[str] = set()
        refreshed = 0
        failed = 0
        rate_limited_by_period: dict[str, list[str]] = {
            STATIC_DAILY_PRICE_REFRESH_PERIOD: [],
            STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD: [],
        }
        for symbols, period in fetch_groups:
            group_refreshed, group_failed, group_failures = self._fetch_and_store(
                symbols,
                period=period,
                batch_size=batch_size,
                market=market,
                as_of_date=as_of_date,
                readjusted_symbols=(
                    readjusted_symbols
                    if period == STATIC_DAILY_PRICE_REFRESH_PERIOD
                    else None
                ),
                missing_session_frames=missing_session_frames,
                stored_symbols=stored_symbols,
            )
            refreshed += group_refreshed
            failed += group_failed
            rate_limited_by_period[period].extend(_rate_limited_symbols(group_failures))
        retry_readjusted_symbols: dict[str, set[date]] = {}
        retry_stats = self._retry_rate_limited_failures(
            market=market,
            rate_limited_symbols_by_period=rate_limited_by_period,
            readjusted_symbols=retry_readjusted_symbols,
            as_of_date=as_of_date,
            missing_session_frames=missing_session_frames,
            stored_symbols=stored_symbols,
        )
        refreshed += retry_stats["recovered"]
        failed -= retry_stats["recovered"]
        # Their throttled first attempt was counted as failed; the re-bootstrap
        # below now owns their outcome.
        failed -= len(retry_readjusted_symbols)
        readjusted_symbols.update(retry_readjusted_symbols)
        if readjusted_symbols:
            print(
                f"[static-daily prices] Re-bootstrapping {len(readjusted_symbols):,} "
                "symbols whose history was back-adjusted (split or dividend).",
                flush=True,
            )
            readjusted_refreshed, readjusted_failed, _ = self._fetch_and_store(
                list(readjusted_symbols),
                period=STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
                batch_size=batch_size,
                market=market,
                as_of_date=as_of_date,
                replacement_required_dates=readjusted_symbols,
                missing_session_frames=missing_session_frames,
                stored_symbols=stored_symbols,
            )
            refreshed += readjusted_refreshed
            failed += readjusted_failed
        primary_benchmark = critical_symbols[0] if critical_symbols else None
        primary_was_stored = primary_benchmark in stored_symbols
        primary_benchmark_retry = self._retry_primary_benchmark(
            symbol=primary_benchmark,
            fetch_candidate=primary_benchmark in supported_symbols,
            period=(
                STATIC_DAILY_PRICE_REFRESH_PERIOD
                if primary_benchmark in stale_symbol_set
                else STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD
                if primary_benchmark in bootstrap_symbol_set
                else None
            ),
            market=market,
            as_of_date=as_of_date,
            readjusted_symbols=readjusted_symbols,
            stored_symbols=stored_symbols,
            missing_session_frames=missing_session_frames,
        )
        if not primary_was_stored and primary_benchmark in stored_symbols:
            refreshed += 1
            failed -= 1
        # Before the latest-session quote repair, so a repair frame missing
        # the as-of bar is quote-repaired with the rest.
        rs_anchor_repair = self._repair_rs_anchor_gaps(
            rs_anchor_coverage,
            symbols=rs_anchor_repair_symbols,
            batch_size=batch_size,
            market=market,
            as_of_date=as_of_date,
            missing_session_frames=missing_session_frames,
        )
        session_repair = self._repair_missing_sessions(
            market=market,
            as_of_date=as_of_date,
            frames=missing_session_frames,
        )

        return {
            "status": "completed",
            "market": market,
            "as_of_date": as_of_date.isoformat(),
            "total_active_symbols": len(active_symbols),
            "supported_symbols": len(supported_symbols),
            "key_market_symbols": len(key_market_symbols),
            "db_fresh_symbols": len(db_fresh_symbols),
            "stale_symbols": len(stale_symbols),
            "no_history_symbols": len(no_history_symbols),
            "history_incomplete_symbols": len(history_incomplete_symbols),
            "rrg_history_incomplete_symbols": len(
                rrg_history_incomplete_symbols
            ),
            "breadth_history_incomplete_symbols": len(
                breadth_history_incomplete_symbols
            ),
            "breadth_history_bootstrap_symbols": len(
                breadth_history_bootstrap_symbols
            ),
            "breadth_history_missing_through_date_symbols": len(
                breadth_history_missing_through_date_symbols
            ),
            "rrg_history_coverage_status": rrg_history_coverage.status,
            "rrg_history_coverage_error": rrg_history_coverage.error,
            "breadth_history_coverage_status": breadth_history_coverage.status,
            "breadth_history_coverage_error": breadth_history_coverage.error,
            "breadth_history_required_dates": (
                breadth_history_coverage.required_dates
            ),
            "skipped_unsupported_symbols": len(skipped_symbols),
            "rrg_history_tail_gap_symbols": len(rrg_history_tail_gap_symbols),
            "readjusted_symbols": len(readjusted_symbols),
            "yahoo_fetched_symbols": refreshed,
            "yahoo_failed_symbols": failed,
            "rate_limited_retry": retry_stats,
            "primary_benchmark_retry": primary_benchmark_retry,
            "latest_session_repair": session_repair,
            "rs_anchor_repair": rs_anchor_repair,
        }

    def _rrg_history_coverage(
        self,
        db,
        *,
        market: str | None,
        through_date: date,
        symbols: tuple[str, ...],
        enabled: bool,
    ) -> _StaticHistoryCoverageOutcome:
        if not enabled:
            return _StaticHistoryCoverageOutcome((), "not_requested")
        if market is None or not symbols:
            return _StaticHistoryCoverageOutcome((), "not_applicable")

        try:
            required_anchor_dates = (
                self._group_history_price_coverage.required_anchor_dates(
                    market=market,
                    through_date=through_date,
                )
            )
        except Exception as exc:
            print(
                "[static-daily prices] Could not resolve RRG history anchors "
                f"for market={market}: {exc}",
                flush=True,
            )
            return _StaticHistoryCoverageOutcome(
                (),
                "unverified",
                str(exc),
            )

        # Anchors include each target session itself (offset 0), so the newest
        # ones are always missing from a seed and would send every symbol to
        # the 2y bootstrap. Anchors after tail_start (the last 4 calendar days,
        # through_date-3..through_date) go to the 7d top-up instead, including
        # for already-fresh symbols with a hole there.
        tail_start = through_date - timedelta(days=STATIC_DAILY_PRICE_TOP_UP_TAIL_DAYS)
        history_anchor_dates = frozenset(
            anchor for anchor in required_anchor_dates if anchor <= tail_start
        )
        tail_anchor_dates = frozenset(required_anchor_dates) - history_anchor_dates
        coverage = self._group_history_price_coverage.classify(
            db,
            market=market,
            through_date=through_date,
            symbols=symbols,
            required_anchor_dates=history_anchor_dates,
        )
        tail_coverage = self._group_history_price_coverage.classify(
            db,
            market=market,
            through_date=through_date,
            symbols=symbols,
            required_anchor_dates=tail_anchor_dates,
        )
        return _StaticHistoryCoverageOutcome(
            tuple(coverage.incomplete_symbols),
            "verified",
            required_dates=len(history_anchor_dates),
            missing_through_date_symbols=tuple(tail_coverage.incomplete_symbols),
        )

    def _rs_anchor_coverage(
        self,
        db,
        *,
        market: str | None,
        through_date: date,
        symbols: tuple[str, ...],
        enabled: bool,
        lookahead_sessions: int,
    ) -> _RsAnchorCoverage:
        if not enabled:
            return _RsAnchorCoverage("not_requested")
        if market is None or not symbols:
            return _RsAnchorCoverage("not_applicable")
        try:
            required = self._rs_anchor_price_coverage.required_dates(
                market=market,
                through_date=through_date,
                lookahead_sessions=lookahead_sessions,
            )
            # As for RRG anchors: the 7d top-up owns gaps in the tail.
            tail_start = through_date - timedelta(days=STATIC_DAILY_PRICE_TOP_UP_TAIL_DAYS)
            history_dates = frozenset(day for day in required if day <= tail_start)
            history_gaps = self._rs_anchor_price_coverage.gaps(
                db, symbols=symbols, required_dates=history_dates
            )
            tail_gaps = self._rs_anchor_price_coverage.gaps(
                db, symbols=symbols, required_dates=required - history_dates
            )
        except Exception as exc:
            print(
                "[static-daily prices] Could not resolve RS anchor coverage "
                f"for market={market}: {exc}",
                flush=True,
            )
            return _RsAnchorCoverage("unverified", error=str(exc))
        if history_gaps.missing_dates_by_symbol:
            print(
                f"[static-daily prices:{market}] {len(history_gaps.missing_dates_by_symbol):,} "
                "symbols with older history are missing RS anchor sessions: "
                f"{history_gaps.count_by_date()}",
                flush=True,
            )
        return _RsAnchorCoverage(
            "verified",
            history_gaps=history_gaps,
            tail_gap_symbols=tuple(tail_gaps.missing_dates_by_symbol),
            history_dates=history_dates,
        )

    def _rs_anchor_repair_stats(
        self,
        coverage: _RsAnchorCoverage,
        *,
        attempted: int = 0,
        unresolved: RsAnchorGaps | None = None,
    ) -> dict[str, Any]:
        gaps = coverage.history_gaps.missing_dates_by_symbol
        unresolved = unresolved if unresolved is not None else coverage.history_gaps
        unresolved_symbols = sorted(unresolved.missing_dates_by_symbol)
        return {
            "status": coverage.status,
            "error": coverage.error,
            "required_dates": len(coverage.history_dates),
            "gap_symbols": len(gaps),
            "gap_count_by_date": coverage.history_gaps.count_by_date(),
            "tail_gap_symbols": len(coverage.tail_gap_symbols),
            "attempted_symbols": attempted,
            "repaired_symbols": len(gaps) - len(unresolved_symbols),
            "unresolved_symbols": len(unresolved_symbols),
            "unresolved_count_by_date": unresolved.count_by_date(),
            "unresolved_samples": unresolved_symbols[:RS_ANCHOR_UNRESOLVED_SAMPLE_LIMIT],
        }

    def _repair_rs_anchor_gaps(
        self,
        coverage: _RsAnchorCoverage,
        *,
        symbols: list[str],
        batch_size: int,
        market: str | None,
        as_of_date: date | None = None,
        missing_session_frames: dict[str, pd.DataFrame] | None = None,
    ) -> dict[str, Any]:
        """Refetch 2y for symbols with RS anchor holes and swap their history.

        Each symbol's stored rows are replaced by the refetch only if it covers
        the stored dates and the missing anchors, so a repair never splices
        two adjustment bases; anything else stays visibly unresolved. Coverage
        is then rechecked from committed rows, not inferred from the fetch.
        """
        missing = coverage.history_gaps.missing_dates_by_symbol
        if not missing:
            return self._rs_anchor_repair_stats(coverage)
        # Earlier fetches this run (2y bootstraps, drift replacements) may
        # already have filled some gaps; refetch only what is still missing,
        # judged by stored rows rather than by what was scheduled.
        with self._session_factory() as db:
            pending = self._rs_anchor_price_coverage.gaps(
                db, symbols=tuple(symbols), required_dates=coverage.history_dates
            ).missing_dates_by_symbol
        symbols = [symbol for symbol in symbols if symbol in pending]
        if symbols:
            print(
                f"[static-daily prices:{market}] Repairing RS anchor history for "
                f"{len(symbols):,} symbols.",
                flush=True,
            )
            required = {symbol: set(missing[symbol]) for symbol in symbols}
            _, _, failures = self._fetch_and_store(
                symbols,
                period=STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
                batch_size=batch_size,
                market=market,
                as_of_date=as_of_date,
                replacement_required_dates=required,
                missing_session_frames=missing_session_frames,
            )
            rate_limited = _rate_limited_symbols(failures)
            if rate_limited:
                # A large repair burst is the likeliest 429; replay it once.
                print(
                    f"[static-daily prices:{market}] {len(rate_limited):,} RS anchor "
                    f"repairs were rate limited; retrying after "
                    f"{STATIC_SESSION_REPAIR_WAIT_SECONDS}s.",
                    flush=True,
                )
                self._check_deadline(STATIC_SESSION_REPAIR_WAIT_SECONDS)
                self._sleep(STATIC_SESSION_REPAIR_WAIT_SECONDS)
                self._fetch_and_store(
                    rate_limited,
                    period=STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
                    batch_size=batch_size,
                    market=market,
                    as_of_date=as_of_date,
                    replacement_required_dates=required,
                    missing_session_frames=missing_session_frames,
                )
        with self._session_factory() as db:
            unresolved = self._rs_anchor_price_coverage.gaps(
                db,
                symbols=tuple(missing),
                required_dates=coverage.history_dates,
            )
        stats = self._rs_anchor_repair_stats(
            coverage, attempted=len(symbols), unresolved=unresolved
        )
        print(
            f"[static-daily prices:{market}] RS anchor repair: "
            f"{stats['repaired_symbols']:,}/{stats['gap_symbols']:,} repaired, "
            f"{stats['unresolved_symbols']:,} unresolved {stats['unresolved_count_by_date']}.",
            flush=True,
        )
        return stats

    def _breadth_history_coverage(
        self,
        db,
        *,
        market: str | None,
        through_date: date,
        symbols: tuple[str, ...],
        enabled: bool,
    ) -> _StaticHistoryCoverageOutcome:
        if not enabled:
            return _StaticHistoryCoverageOutcome((), "not_requested")
        if market is None or not symbols:
            return _StaticHistoryCoverageOutcome((), "not_applicable")

        try:
            coverage = self._breadth_history_price_coverage.classify(
                db,
                market=market,
                through_date=through_date,
                symbols=symbols,
            )
        except Exception as exc:
            print(
                "[static-daily prices] Could not resolve breadth history dates "
                f"for market={market}: {exc}",
                flush=True,
            )
            return _StaticHistoryCoverageOutcome(
                (),
                "unverified",
                str(exc),
            )

        return _StaticHistoryCoverageOutcome(
            tuple(coverage.incomplete_symbols),
            "verified",
            required_dates=coverage.required_price_date_count,
            bootstrap_symbols=tuple(
                getattr(
                    coverage,
                    "history_incomplete_symbols",
                    coverage.incomplete_symbols,
                )
            ),
            missing_through_date_symbols=tuple(
                getattr(coverage, "missing_through_date_symbols", ())
            ),
        )

    def _fetch_and_store(
        self,
        symbols: list[str],
        *,
        period: str,
        batch_size: int,
        market: str | None,
        as_of_date: date | None = None,
        readjusted_symbols: dict[str, set[date]] | None = None,
        replacement_required_dates: dict[str, set[date]] | None = None,
        missing_session_frames: dict[str, pd.DataFrame] | None = None,
        stored_symbols: set[str] | None = None,
    ) -> tuple[int, int, dict[str, str]]:
        """Fetch and store ``symbols``.

        Returns the stored count, the failed count and each provider failure
        by symbol (its error text). With ``stored_symbols``, each symbol
        stored is added there.

        With ``as_of_date``, each batch line also counts the frames a provider
        repair (e.g. Yahoo quotes) completed and those still stored without
        that session: the gap behind a Market RS coverage failure.

        With ``readjusted_symbols``, symbols whose history Yahoo back-adjusted
        are neither stored nor counted; they are appended there for a full
        refetch instead, with the discarded frame's dates. With
        ``replacement_required_dates``, each symbol's stored history is swapped
        for the fetched rows (see ``_replace_stored_history``): the store only
        updates a symbol's latest existing row, so old-scale history would
        otherwise survive. With ``missing_session_frames`` and ``as_of_date``,
        stored frames still lacking that session are kept there (and dropped
        once a later pass stores the symbol with it).
        """
        refreshed_count = 0
        failed_count = 0
        repaired_count = 0
        missing_session_count = 0
        failures: dict[str, str] = {}
        total_symbols = len(symbols)
        if not symbols:
            return 0, 0, {}
        total_group_batches = (total_symbols + batch_size - 1) // batch_size
        for batch_index, batch_symbols in enumerate(
            _iter_chunks(symbols, batch_size),
            start=1,
        ):
            self._check_deadline()
            processed_before = refreshed_count + failed_count
            print(
                f"[static-daily prices] Batch {batch_index}/{total_group_batches}: "
                f"{processed_before:,}/{total_symbols:,} processed, fetching "
                f"{len(batch_symbols):,} symbols from Yahoo ({period}).",
                flush=True,
            )
            batch_results = self._fetcher.fetch_prices_in_batches(
                batch_symbols,
                period=period,
                start_batch_size=batch_size,
                market=market,
            )
            batch_to_store: dict[str, Any] = {}
            for symbol, payload in batch_results.items():
                price_data = payload.get("price_data")
                if not payload.get("has_error") and price_data is not None and not price_data.empty:
                    batch_to_store[symbol] = price_data
                    refreshed_count += 1
                else:
                    failed_count += 1
                    failures[symbol] = str(payload.get("error") or "no_price_data")
            if readjusted_symbols is not None and batch_to_store:
                for symbol in sorted(self._adjustment_drift_symbols(batch_to_store)):
                    readjusted_symbols[symbol] = {
                        row["date"]
                        for row in _frame_price_rows(symbol, batch_to_store.pop(symbol))
                    }
                    refreshed_count -= 1
            if replacement_required_dates is not None and batch_to_store:
                replaced = self._replace_stored_history(
                    batch_to_store,
                    required_dates_by_symbol=replacement_required_dates,
                )
                for symbol in sorted(set(batch_to_store) - replaced):
                    del batch_to_store[symbol]
                    refreshed_count -= 1
                    failed_count += 1
            if batch_to_store:
                self._price_cache.store_batch_in_cache(
                    batch_to_store,
                    also_store_db=True,
                    market=market,
                )
                if stored_symbols is not None:
                    stored_symbols.update(batch_to_store)
            session_note = ""
            if as_of_date is not None:
                repaired_count += sum(
                    1 for symbol in batch_to_store if batch_results[symbol].get("repaired_by")
                )
                for symbol, frame in batch_to_store.items():
                    if not isinstance(frame, pd.DataFrame):
                        continue
                    if _has_session(frame, as_of_date):
                        if missing_session_frames is not None:
                            missing_session_frames.pop(symbol, None)
                        continue
                    missing_session_count += 1
                    if missing_session_frames is not None:
                        missing_session_frames[symbol] = frame
                session_note = (
                    f", {repaired_count:,} repaired, "
                    f"{missing_session_count:,} missing {as_of_date.isoformat()}"
                )
            print(
                f"[static-daily prices] Batch {batch_index}/{total_group_batches} complete: "
                f"{refreshed_count + failed_count:,}/{total_symbols:,} processed, "
                f"{refreshed_count:,} refreshed, {failed_count:,} failed{session_note}.",
                flush=True,
            )
        return refreshed_count, failed_count, failures

    def _repair_missing_sessions(
        self,
        *,
        market: str | None,
        as_of_date: date,
        frames: dict[str, pd.DataFrame],
    ) -> dict[str, Any]:
        """Quote-repair frames the refresh stored without ``as_of_date``, once more.

        Only for markets whose price plan uses Yahoo quote repair, after
        ``STATIC_SESSION_REPAIR_WAIT_SECONDS``. Repaired frames are stored.
        """
        stats: dict[str, Any] = {"attempted": 0, "repaired": 0, "wait_seconds": 0}
        if (
            not frames
            or market is None
            or not provider_data_plan_registry.plan_for(market, DATASET_PRICES).allows(
                PROVIDER_YAHOO_QUOTE
            )
        ):
            return stats
        self._check_deadline(STATIC_SESSION_REPAIR_WAIT_SECONDS)
        from app.services.yahoo_quote_price_repair import (
            YAHOO_QUOTE_BATCH_SIZE,
            fetch_yahoo_quotes,
            repair_from_yahoo_quotes,
        )

        print(
            f"[static-daily prices:{market}] {len(frames):,} symbols are still missing "
            f"{as_of_date.isoformat()}; waiting {STATIC_SESSION_REPAIR_WAIT_SECONDS}s, "
            "then repairing them from Yahoo quotes.",
            flush=True,
        )
        self._sleep(STATIC_SESSION_REPAIR_WAIT_SECONDS)
        self._check_deadline()  # the wait itself may have run past it
        rate_limiter = getattr(self._fetcher, "_rate_limiter", None)
        repaired: dict[str, pd.DataFrame] = {}
        # One quote batch per call, deadline-checked and stored before the
        # next, so a stop past the deadline keeps the batches already repaired.
        for symbols in _iter_chunks(list(frames), YAHOO_QUOTE_BATCH_SIZE):
            self._check_deadline()
            results = {symbol: {"price_data": frames[symbol]} for symbol in symbols}
            repair_from_yahoo_quotes(
                results,
                expected_session=as_of_date,
                market_tz=self._calendar_service.market_timezone(market),
                fetch_quotes=self._fetch_quotes or fetch_yahoo_quotes,
                wait=(
                    (lambda: rate_limiter.wait_for_market("yfinance:batch", market))
                    if rate_limiter is not None
                    else None
                ),
                sleep=self._sleep,
            )
            batch_repaired = {
                symbol: payload["price_data"]
                for symbol, payload in results.items()
                if payload.get("repaired_by")
            }
            if batch_repaired:
                self._price_cache.store_batch_in_cache(
                    batch_repaired, also_store_db=True, market=market
                )
                repaired.update(batch_repaired)
        stats.update(
            attempted=len(frames),
            repaired=len(repaired),
            wait_seconds=STATIC_SESSION_REPAIR_WAIT_SECONDS,
        )
        print(
            f"[static-daily prices:{market}] Latest-session repair complete: "
            f"{len(repaired):,}/{len(frames):,} repaired.",
            flush=True,
        )
        return stats

    def _adjustment_drift_symbols(self, frames: dict[str, Any]) -> set[str]:
        """Symbols whose fetched Adj Close disagrees with stored rows for the same dates."""
        fetched = {
            (symbol, pd.Timestamp(stamp).date()): float(value)
            for symbol, frame in frames.items()
            if isinstance(frame, pd.DataFrame) and "Adj Close" in frame
            for stamp, value in frame["Adj Close"].dropna().items()
        }
        if not fetched:
            return set()
        with self._session_factory() as db:
            stored_rows = (
                db.query(StockPrice.symbol, StockPrice.date, StockPrice.adj_close)
                .filter(
                    StockPrice.symbol.in_({symbol for symbol, _ in fetched}),
                    StockPrice.date >= min(row_date for _, row_date in fetched),
                )
                .all()
            )
        return {
            symbol
            for symbol, row_date, stored in stored_rows
            if stored
            and (symbol, row_date) in fetched
            and abs(fetched[(symbol, row_date)] / stored - 1)
            > STATIC_ADJUSTMENT_DRIFT_TOLERANCE
        }

    def _replace_stored_history(
        self,
        frames: dict[str, Any],
        *,
        required_dates_by_symbol: dict[str, set[date]],
    ) -> set[str]:
        """Swap each symbol's stored rows for its fetched rows in one transaction.

        A symbol is replaced only if its fetched rows cover every stored date
        from their first date onward plus its ``required_dates_by_symbol``
        (the discarded drift-triggering top-up, e.g. the new as-of bar); a
        sparse or truncated frame would leave a gap, a stale symbol, or an
        old-scale row, so it is skipped. Returns the symbols replaced; on any
        failure nothing is changed and the empty set is returned, so callers
        count those symbols as failed.
        """
        rows_by_symbol = {
            symbol: rows
            for symbol, frame in frames.items()
            if isinstance(frame, pd.DataFrame)
            and (rows := _frame_price_rows(symbol, frame))
        }
        if not rows_by_symbol:
            return set()
        with self._session_factory() as db:
            try:
                for symbol, rows in list(rows_by_symbol.items()):
                    dates = {row["date"] for row in rows}
                    stored_dates = {
                        stored_date
                        for (stored_date,) in db.query(StockPrice.date).filter(
                            StockPrice.symbol == symbol,
                            StockPrice.date >= min(dates),
                        )
                    }
                    uncovered = (
                        stored_dates | required_dates_by_symbol.get(symbol, set())
                    ) - dates
                    if uncovered:
                        print(
                            "[static-daily prices] Not replacing back-adjusted history "
                            f"for {symbol}: refetch lacks {len(uncovered)} required dates.",
                            flush=True,
                        )
                        del rows_by_symbol[symbol]
                        continue
                    db.query(StockPrice).filter(
                        StockPrice.symbol == symbol,
                        StockPrice.date.in_(dates),
                    ).delete(synchronize_session=False)
                if rows_by_symbol:
                    persist_stock_price_mappings(db, rows_by_symbol)
                db.commit()
            except Exception as exc:
                db.rollback()
                print(
                    "[static-daily prices] Could not replace back-adjusted history "
                    f"for {len(rows_by_symbol):,} symbols: {exc}",
                    flush=True,
                )
                return set()
        return set(rows_by_symbol)

    def _retry_rate_limited_failures(
        self,
        *,
        market: str | None,
        rate_limited_symbols_by_period: dict[str, list[str]],
        readjusted_symbols: dict[str, set[date]] | None = None,
        as_of_date: date | None = None,
        missing_session_frames: dict[str, pd.DataFrame] | None = None,
        stored_symbols: set[str] | None = None,
    ) -> dict[str, Any]:
        retry_groups = [
            (period, sorted(set(symbols)))
            for period, symbols in rate_limited_symbols_by_period.items()
            if symbols
        ]
        flagged = sum(len(symbols) for _period, symbols in retry_groups)
        stats: dict[str, Any] = {
            "attempted": 0,
            "recovered": 0,
            "still_failed": 0,
            "skipped": 0,
            "skip_reason": None,
            "wait_seconds": 0,
            "batch_size": STATIC_RATE_LIMITED_RETRY_BATCH_SIZE,
        }
        if not flagged:
            return stats
        normalized = (market or "").upper()
        if normalized not in STATIC_RATE_LIMITED_RETRY_MARKETS:
            print(
                f"[static-daily prices] Skipping rate-limited retry for market={normalized or 'shared'}: "
                f"{flagged} symbols looked throttled but market is outside the retry allowlist.",
                flush=True,
            )
            stats.update(skipped=flagged, skip_reason="market_not_in_retry_allowlist")
            return stats

        self._check_deadline(STATIC_RATE_LIMITED_RETRY_WAIT_SECONDS)
        print(
            f"[static-daily prices:{normalized}] Yahoo flagged {flagged} symbols as rate-limited; "
            f"waiting {STATIC_RATE_LIMITED_RETRY_WAIT_SECONDS}s then retrying with batch size "
            f"{STATIC_RATE_LIMITED_RETRY_BATCH_SIZE}.",
            flush=True,
        )
        self._sleep(STATIC_RATE_LIMITED_RETRY_WAIT_SECONDS)

        recovered = 0
        # One fetch per retry batch, each checked against the deadline and
        # stored before the next, so a stop keeps every recovered batch (#502).
        retry_batches = [
            (period, batch)
            for period, unique_symbols in retry_groups
            for batch in _iter_chunks(unique_symbols, STATIC_RATE_LIMITED_RETRY_BATCH_SIZE)
        ]
        for period, batch_symbols in retry_batches:
            self._check_deadline()
            retry_results = self._fetcher.fetch_prices_in_batches(
                batch_symbols,
                period=period,
                start_batch_size=STATIC_RATE_LIMITED_RETRY_BATCH_SIZE,
                market=market,
            )
            recovered_payload: dict[str, Any] = {}
            for symbol, payload in retry_results.items():
                price_data = payload.get("price_data")
                if not payload.get("has_error") and price_data is not None and not price_data.empty:
                    recovered_payload[symbol] = price_data
                    recovered += 1
            if (
                readjusted_symbols is not None
                and period == STATIC_DAILY_PRICE_REFRESH_PERIOD
                and recovered_payload
            ):
                for symbol in sorted(self._adjustment_drift_symbols(recovered_payload)):
                    readjusted_symbols[symbol] = {
                        row["date"]
                        for row in _frame_price_rows(symbol, recovered_payload.pop(symbol))
                    }
                    recovered -= 1
            if recovered_payload:
                self._price_cache.store_batch_in_cache(
                    recovered_payload,
                    also_store_db=True,
                    market=market,
                )
                if stored_symbols is not None:
                    stored_symbols.update(recovered_payload)
                if as_of_date is not None and missing_session_frames is not None:
                    for symbol, frame in recovered_payload.items():
                        if not isinstance(frame, pd.DataFrame):
                            continue
                        if _has_session(frame, as_of_date):
                            missing_session_frames.pop(symbol, None)
                        else:
                            missing_session_frames[symbol] = frame
        still_failed = flagged - recovered
        print(
            f"[static-daily prices:{normalized}] Rate-limited retry complete: "
            f"{recovered}/{flagged} recovered, {still_failed} still failed.",
            flush=True,
        )
        stats.update(
            attempted=flagged,
            recovered=recovered,
            still_failed=still_failed,
            wait_seconds=STATIC_RATE_LIMITED_RETRY_WAIT_SECONDS,
        )
        return stats

    def _primary_benchmark_retry_reserve_seconds(self, market: str | None) -> float:
        """Deadline headroom the late primary-benchmark refetch needs.

        Two throttled single-symbol fetches (the top-up, then a drift
        replacement), each with the fetcher's full inter-attempt backoff and,
        where the market's price plan repairs the latest session from Yahoo
        quotes, that repair's backoff too; plus the wait the latest-session
        repair then checks for.
        """
        fetch_seconds = sum(self._fetcher.price_batch_backoff_schedule(market))
        session_repair_wait = 0
        if market is not None and provider_data_plan_registry.plan_for(
            market, DATASET_PRICES
        ).allows(PROVIDER_YAHOO_QUOTE):
            fetch_seconds += sum(YAHOO_QUOTE_RETRY_BACKOFF_SECONDS)
            session_repair_wait = STATIC_SESSION_REPAIR_WAIT_SECONDS
        return 2 * fetch_seconds + session_repair_wait

    def _retry_primary_benchmark(
        self,
        *,
        symbol: str | None,
        fetch_candidate: bool,
        period: str | None,
        market: str | None,
        as_of_date: date,
        readjusted_symbols: dict[str, set[date]],
        stored_symbols: set[str],
        missing_session_frames: dict[str, pd.DataFrame],
    ) -> dict[str, Any]:
        """Refetch the primary benchmark once more if the refresh left it
        unstored or stored without ``as_of_date``.

        Without it a US build publishes nothing, and other markets fall back
        to their secondary benchmark (see ``critical_price_symbols``).
        ``fetch_candidate`` says it passed the provider symbol policy;
        ``period`` is the one its fetch group used (``None`` when the database
        already had it). The single-symbol call meets the fetcher's own
        backoff when throttled; drift found now gets the same replacement
        refetch as the main pass. The refetch is skipped, never raising, when
        the deadline leaves less than the reserve, and reported as skipped
        when its own call finds the Yahoo circuit open. A frame still lacking
        the session stays in ``missing_session_frames`` for the quote repair.
        """
        stats: dict[str, Any] = {
            "symbol": symbol,
            "attempted": False,
            "recovered": False,
            "skipped_reason": None,
        }

        def current() -> bool:
            return symbol in stored_symbols and symbol not in missing_session_frames

        if symbol is None:
            stats["skipped_reason"] = "no_primary_benchmark"
            return stats
        if not fetch_candidate:
            stats["skipped_reason"] = "not_a_fetch_candidate"
            return stats
        if period is None or current():
            stats["skipped_reason"] = "already_current"
            return stats
        label = f"[static-daily prices:{market}]"
        remaining = self._seconds_until_deadline()
        if remaining is not None:
            reserve = self._primary_benchmark_retry_reserve_seconds(market)
            if remaining < reserve:
                print(
                    f"{label} Not refetching {symbol}: {remaining:.0f}s left before the "
                    f"price-stage deadline, {reserve:.0f}s needed.",
                    flush=True,
                )
                stats["skipped_reason"] = "deadline"
                return stats

        fetch_kwargs: dict[str, Any] = {
            "batch_size": 1,
            "market": market,
            "as_of_date": as_of_date,
            "missing_session_frames": missing_session_frames,
            "stored_symbols": stored_symbols,
        }
        failures: dict[str, str] = {}
        if symbol not in readjusted_symbols:
            drift_found: dict[str, set[date]] = {}
            _, _, failures = self._fetch_and_store(
                [symbol],
                period=period,
                readjusted_symbols=(
                    drift_found if period == STATIC_DAILY_PRICE_REFRESH_PERIOD else None
                ),
                **fetch_kwargs,
            )
            readjusted_symbols.update(drift_found)
        if symbol in readjusted_symbols:
            _, _, failures = self._fetch_and_store(
                [symbol],
                period=STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
                replacement_required_dates=readjusted_symbols,
                **fetch_kwargs,
            )
        if failures.get(symbol) == "circuit_open":
            print(
                f"{label} Late refetch of {symbol} found the Yahoo circuit open for {market}; "
                "not retried.",
                flush=True,
            )
            stats["skipped_reason"] = "circuit_open"
            return stats
        stats["attempted"] = True
        stats["recovered"] = current()
        outcome = "recovered" if stats["recovered"] else "still missing"
        print(
            f"{label} Primary benchmark {symbol} {outcome} {as_of_date.isoformat()} "
            "after its late single-symbol refetch.",
            flush=True,
        )
        return stats
