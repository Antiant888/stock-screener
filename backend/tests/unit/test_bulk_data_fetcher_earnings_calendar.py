"""Regression tests for event-calendar evidence in bulk fundamentals ingestion.

The non-US weekly-reference producer fetches fundamentals through
``BulkDataFetcher._extract_fundamentals``. These tests guard that the
shared Yahoo calendar normalizer's observation lands in the persisted
payload (success-empty stays available, provider failure stays retryable)
so ``prepare_data_bulk``'s persisted-only event gate can pass.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.stock import StockFundamental
from app.services.bulk_data_fetcher import BulkDataFetcher
from app.services.fundamentals_cache_service import FundamentalsCacheService


def _ticker_with_calendar(earnings_dates) -> SimpleNamespace:
    return SimpleNamespace(
        fast_info=SimpleNamespace(
            market_cap=None, shares=None, last_price=None,
        ),
        earnings_dates=earnings_dates,
    )


class TestExtractFundamentalsCalendarEvidence:
    def test_future_earnings_rows_produce_observation_and_next_date(self):
        before = datetime.now(UTC).date()
        future = datetime.now(UTC) + pd.Timedelta(days=9)
        ticker = _ticker_with_calendar(
            pd.DataFrame({"Earnings Date": [future], "EPS Estimate": [1.25]}).set_index(
                "Earnings Date"
            )
        )

        result = BulkDataFetcher()._extract_fundamentals(ticker, {})
        after = datetime.now(UTC).date()

        assert result["next_earnings_date"] == future.date()
        assert result["event_calendar_as_of_date"] in {before, after}
        assert "recent_earnings" not in result

    def test_successful_empty_calendar_stays_observed_without_next_date(self):
        """Success with no upcoming earnings keeps availability via the stamp."""
        before = datetime.now(UTC).date()
        ticker = _ticker_with_calendar(pd.DataFrame())

        result = BulkDataFetcher()._extract_fundamentals(ticker, {})
        after = datetime.now(UTC).date()

        assert "event_calendar_as_of_date" in result
        assert result["event_calendar_as_of_date"] in {before, after}
        # A null next date is dropped by the None-filter, which the persisted
        # gate treats identically to an explicit None: still available.
        assert "next_earnings_date" not in result

    def test_provider_failure_stamps_no_calendar_marker(self):
        class FailingCalendarTicker(SimpleNamespace):
            @property
            def earnings_dates(self):
                raise RuntimeError("calendar unavailable")

        result = BulkDataFetcher()._extract_fundamentals(
            FailingCalendarTicker(), {}
        )

        assert "event_calendar_as_of_date" not in result
        assert "next_earnings_date" not in result

    def test_none_ticker_still_extracts_without_calendar(self):
        result = BulkDataFetcher()._extract_fundamentals(None, {"marketCap": 42})

        assert result["market_cap"] == 42
        assert "event_calendar_as_of_date" not in result

    def test_extract_earnings_calendar_is_usable_in_isolation(self):
        future = datetime.now(UTC) + pd.Timedelta(days=11)
        ticker = _ticker_with_calendar(
            pd.DataFrame({"Earnings Date": [future]}).set_index("Earnings Date")
        )

        stamped = BulkDataFetcher()._extract_earnings_calendar(ticker, symbol="AAPL")

        assert stamped["next_earnings_date"] == future.date()


STORED_HISTORY = [
    {"report_ts": "2026-07-22T16:00:00-04:00", "eps_estimate": 1.73, "reported_eps": 1.54, "surprise_pct": -10.88},
]


class FailingCalendarTicker(SimpleNamespace):
    @property
    def earnings_dates(self):
        raise RuntimeError("calendar unavailable")


class TestStoredEarningsHistory:
    def _store_with_history(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine)
        service = FundamentalsCacheService(redis_client=None, session_factory=factory)
        seeded = {"event_calendar_as_of_date": date(2026, 10, 1), "recent_earnings": STORED_HISTORY}
        assert service._store_in_database("AAPL", seeded, data_source="yfinance", market="US")
        return service, factory

    def _stored_history(self, factory):
        db = factory()
        try:
            return db.query(StockFundamental).filter_by(symbol="AAPL").one().recent_earnings
        finally:
            db.close()

    def test_failed_lookup_leaves_stored_history_untouched(self):
        service, factory = self._store_with_history()
        payload = BulkDataFetcher()._extract_fundamentals(FailingCalendarTicker(), {"marketCap": 42})

        assert service._store_in_database("AAPL", payload, data_source="yfinance", market="US")

        assert self._stored_history(factory) == STORED_HISTORY

    @pytest.mark.parametrize(
        "earnings_dates",
        [
            None,
            pd.DataFrame(),
            # Upcoming rows only: Yahoo left out the Reported EPS column.
            pd.DataFrame(
                {"Earnings Date": [datetime.now(UTC) + timedelta(days=9)], "EPS Estimate": [1.25]}
            ).set_index("Earnings Date"),
        ],
        ids=["none", "empty", "no_reported_eps_column"],
    )
    def test_successful_lookup_without_reports_keeps_stored_history(self, earnings_dates):
        service, factory = self._store_with_history()
        payload = BulkDataFetcher()._extract_fundamentals(
            _ticker_with_calendar(earnings_dates), {"marketCap": 42}
        )

        assert service._store_in_database("AAPL", payload, data_source="yfinance", market="US")

        assert self._stored_history(factory) == STORED_HISTORY
