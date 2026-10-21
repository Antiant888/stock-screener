from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

import app.services.hk_sina_price_repair as repair_module
from app.services.hk_sina_price_repair import repair_missing_latest_sessions


@pytest.fixture(autouse=True)
def _reset_breaker():
    repair_module._breaker["consecutive_failures"] = 0
    yield
    repair_module._breaker["consecutive_failures"] = 0


def _yahoo(rows):
    frame = pd.DataFrame(
        rows,
        columns=["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends"],
    )
    return frame.set_index(pd.DatetimeIndex(frame.pop("Date")))


def _sina(rows):
    frame = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    return frame.set_index(pd.DatetimeIndex(frame.pop("Date")))


def _ok(frame):
    return {"price_data": frame, "has_error": False}


def test_appends_missing_latest_session_and_drops_yahoo_nan_bar():
    # Mirrors ^HSI on 2026-09-28: Yahoo returns the session row with NaN OHLC.
    results = {
        "^HSI": _ok(
            _yahoo(
                [
                    ("2026-09-25", 24523.5, 24537.1, 24275.6, 24510.0, 24510.0, 1, 0.0),
                    ("2026-09-28", np.nan, np.nan, np.nan, np.nan, np.nan, 0, 0.0),
                ]
            )
        )
    }
    sina = _sina(
        [
            ("2026-09-25", 24523.5, 24537.1, 24275.6, 24510.0, 1),
            ("2026-09-28", 24554.6, 24767.2, 24554.6, 24642.5, 2),
        ]
    )

    stats = repair_missing_latest_sessions(
        results, expected_session=date(2026, 9, 28), fetch_recent=lambda _s: sina
    )

    frame = results["^HSI"]["price_data"]
    assert stats == {"stale": 1, "repaired": 1, "failed": 0, "skipped": 0}
    assert [ts.date().isoformat() for ts in frame.index] == ["2026-09-25", "2026-09-28"]
    assert frame.loc["2026-09-28", "Close"] == 24642.5
    assert frame.loc["2026-09-28", "Dividends"] == 0.0
    assert results["^HSI"]["repaired_sessions"] == ["2026-09-28"]


def test_fills_interior_hole_when_yahoo_already_has_next_intraday_bar():
    # Mirrors 2800.HK on 2026-09-29: Yahoo has 09-25 and today's intraday 09-29
    # but never published the completed 09-28 session.
    results = {
        "2800.HK": _ok(
            _yahoo(
                [
                    ("2026-09-25", 25.2, 25.3, 25.0, 25.12, 25.12, 1, 0.0),
                    ("2026-09-29", 25.2, 25.3, 25.1, 25.18, 25.18, 1, 0.0),
                ]
            )
        )
    }
    sina = _sina(
        [
            ("2026-09-25", 25.2, 25.3, 25.0, 25.12, 1),
            ("2026-09-28", 25.1, 25.4, 25.1, 25.30, 2),
            ("2026-09-29", 25.2, 25.3, 25.1, 25.18, 1),
        ]
    )

    repair_missing_latest_sessions(
        results, expected_session=date(2026, 9, 28), fetch_recent=lambda _s: sina
    )

    frame = results["2800.HK"]["price_data"]
    assert [ts.date().isoformat() for ts in frame.index] == [
        "2026-09-25",
        "2026-09-28",
        "2026-09-29",
    ]
    assert results["2800.HK"]["repaired_sessions"] == ["2026-09-28"]


def test_appended_adj_close_keeps_last_yahoo_adjustment_ratio():
    results = {
        "0700.HK": _ok(_yahoo([("2026-09-25", 433.8, 437.2, 431.2, 436.6, 218.3, 9, 0.0)]))
    }
    sina = _sina([("2026-09-28", 441.4, 447.0, 438.6, 439.8, 15)])

    repair_missing_latest_sessions(
        results, expected_session=date(2026, 9, 28), fetch_recent=lambda _s: sina
    )

    assert results["0700.HK"]["price_data"].loc["2026-09-28", "Adj Close"] == pytest.approx(219.9)


@pytest.mark.parametrize(
    "anchor_adj_close", [None, 0.0, float("nan")], ids=["null", "zero", "nan"]
)
def test_appended_adj_close_falls_back_to_close_without_a_usable_anchor(anchor_adj_close):
    results = {
        "0700.HK": _ok(
            _yahoo([("2026-09-25", 433.8, 437.2, 431.2, 436.6, anchor_adj_close, 9, 0.0)])
        )
    }
    sina = _sina([("2026-09-28", 441.4, 447.0, 438.6, 439.8, 15)])

    repair_missing_latest_sessions(
        results, expected_session=date(2026, 9, 28), fetch_recent=lambda _s: sina
    )

    assert results["0700.HK"]["price_data"].loc["2026-09-28", "Adj Close"] == pytest.approx(439.8)
    assert results["0700.HK"]["repaired_sessions"] == ["2026-09-28"]


def test_current_frames_and_yahoo_failures_do_not_call_sina():
    calls = []
    results = {
        "0005.HK": _ok(_yahoo([("2026-09-28", 1, 1, 1, 1, 1, 1, 0.0)])),
        "9999.HK": {"price_data": None, "has_error": True},
    }

    stats = repair_missing_latest_sessions(
        results,
        expected_session=date(2026, 9, 28),
        fetch_recent=lambda symbol: calls.append(symbol),
    )

    assert calls == []
    assert stats["stale"] == 0


def test_breaker_stops_calling_sina_after_consecutive_failures():
    calls = []

    def failing(symbol):
        calls.append(symbol)
        return None

    stale = _yahoo([("2026-09-25", 1, 1, 1, 1, 1, 1, 0.0)])
    results = {f"{code:04d}.HK": _ok(stale) for code in range(15)}

    stats = repair_missing_latest_sessions(
        results, expected_session=date(2026, 9, 28), fetch_recent=failing
    )

    assert len(calls) == repair_module.SINA_HK_MAX_CONSECUTIVE_FAILURES
    assert stats["skipped"] == 15 - repair_module.SINA_HK_MAX_CONSECUTIVE_FAILURES
