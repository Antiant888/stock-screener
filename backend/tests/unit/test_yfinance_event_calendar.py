from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

import pandas as pd

from app.services.data_source_service import DataSourceService
from app.services.stock_event_context_service import StockEventContextService
from app.services.yfinance_service import YFinanceService


def _service() -> YFinanceService:
    service = YFinanceService.__new__(YFinanceService)
    service._wait_for_yfinance_rate_limit = lambda: None
    return service


def test_calendar_status_distinguishes_successful_empty_from_provider_failure(
    monkeypatch,
):
    service = _service()
    monkeypatch.setattr(
        "app.services.yfinance_service.yf.Ticker",
        lambda _symbol: SimpleNamespace(earnings_dates=pd.DataFrame()),
    )

    assert service.get_upcoming_earnings_dates_with_status("AAPL") == ([], True)

    def fail(_symbol):
        raise RuntimeError("provider outage")

    monkeypatch.setattr("app.services.yfinance_service.yf.Ticker", fail)

    assert service.get_upcoming_earnings_dates_with_status("AAPL") == ([], False)


def test_stock_event_summary_propagates_calendar_fetch_status():
    event_service = StockEventContextService.__new__(StockEventContextService)
    event_service._yfinance_service = SimpleNamespace(
        get_upcoming_earnings_dates_with_status=lambda _symbol: ([], False)
    )

    assert event_service.get_next_earnings_summary_with_status(
        "AAPL", as_of_date=date(2026, 8, 23)
    ) == (None, None, False)


def test_fundamentals_snapshot_reuses_ticker_for_authoritative_calendar_observation(
    monkeypatch,
):
    service = _service()
    service._extract_eps_rating_data = lambda _ticker: {}
    earnings_date = datetime.now(UTC).date() + timedelta(days=7)
    earnings_at = datetime.combine(earnings_date, time(20), tzinfo=UTC)
    ticker = SimpleNamespace(
        info={"symbol": "AAPL"},
        earnings_dates=pd.DataFrame(
            {
                "Earnings Date": [earnings_at],
                "EPS Estimate": [1.25],
            }
        ).set_index("Earnings Date"),
    )
    monkeypatch.setattr(
        "app.services.yfinance_service.yf.Ticker", lambda _symbol: ticker
    )

    observed_before = datetime.now(UTC).date()
    result = service.get_fundamentals("AAPL")
    observed_after = datetime.now(UTC).date()

    assert result is not None
    assert result["next_earnings_date"] == earnings_date
    assert result["event_calendar_as_of_date"] in {
        observed_before,
        observed_after,
    }


def test_fundamentals_snapshot_omits_calendar_marker_when_lookup_fails(monkeypatch):
    service = _service()
    service._extract_eps_rating_data = lambda _ticker: {}

    class FailingCalendarTicker:
        def __init__(self):
            self.info = {"symbol": "AAPL"}

        @property
        def earnings_dates(self):
            raise RuntimeError("calendar unavailable")

    monkeypatch.setattr(
        "app.services.yfinance_service.yf.Ticker",
        lambda _symbol: FailingCalendarTicker(),
    )

    result = service.get_fundamentals("AAPL")

    assert result is not None
    assert "event_calendar_as_of_date" not in result
    assert "next_earnings_date" not in result


def test_fundamentals_snapshot_without_reports_leaves_history_unstamped(monkeypatch):
    service = _service()
    service._extract_eps_rating_data = lambda _ticker: {}
    monkeypatch.setattr(
        "app.services.yfinance_service.yf.Ticker",
        lambda _symbol: SimpleNamespace(info={"symbol": "AAPL"}, earnings_dates=None),
    )

    observed_before = datetime.now(UTC).date()
    result = service.get_fundamentals("AAPL")
    observed_after = datetime.now(UTC).date()

    assert result["event_calendar_as_of_date"] in {observed_before, observed_after}
    assert "recent_earnings" not in result


def test_finviz_supplement_carries_reported_quarters(monkeypatch):
    service = _service()
    service._extract_eps_rating_data = lambda _ticker: {}
    ticker = SimpleNamespace(
        info={"symbol": "AAPL"},
        earnings_dates=pd.DataFrame(
            {
                "Earnings Date": [pd.Timestamp("2026-07-22 16:00", tz="America/New_York")],
                "EPS Estimate": [1.73],
                "Reported EPS": [1.54],
                "Surprise(%)": [-10.88],
            }
        ).set_index("Earnings Date"),
    )
    monkeypatch.setattr(
        "app.services.yfinance_service.yf.Ticker", lambda _symbol: ticker
    )
    data_source = DataSourceService.__new__(DataSourceService)
    data_source.yfinance_service = service

    eps_data = data_source.get_eps_rating_data("AAPL")

    assert eps_data["recent_earnings"] == [
        {
            "report_ts": "2026-07-22T16:00:00-04:00",
            "eps_estimate": 1.73,
            "reported_eps": 1.54,
            "surprise_pct": -10.88,
        }
    ]
