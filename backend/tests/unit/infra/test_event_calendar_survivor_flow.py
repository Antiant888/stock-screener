"""End-to-end regression: persisted event-calendar evidence → survivors.

Chains the fixed producer evidence through the persisted-only bulk
preparation path into the fail-closed survivor policy, so the regression
that left every Correction Survivors surface empty (fresh calendar
observations never persisted by producers) cannot silently return.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest
from app.analysis.patterns.config import SetupEngineParameters
from app.scanners.base_screener import DataRequirements
from app.scanners.data_preparation import DataPreparationLayer
from app.services.opportunity_state_service import build_opportunity_projection
from app.services.yahoo_earnings_calendar import EVENT_CALENDAR_MAX_AGE_DAYS


def _make_price_df(days: int = 252, price: float = 100.0) -> pd.DataFrame:
    dates = pd.date_range(end=pd.Timestamp.today(), periods=days, freq="B")
    return pd.DataFrame(
        {
            "Open": price * 0.995,
            "High": price * 1.01,
            "Low": price * 0.99,
            "Close": price,
            "Volume": 1_000_000,
        },
        index=dates,
    )


@pytest.fixture
def price_data():
    return _make_price_df()


def _make_layer(price_data, fundamentals: dict) -> tuple[DataPreparationLayer, MagicMock]:
    price_cache = MagicMock()
    price_cache.get_many.return_value = {"AAPL": price_data}
    fundamentals_cache = MagicMock()
    fundamentals_cache.get_many.return_value = {"AAPL": fundamentals}
    event_context = MagicMock()
    event_context.get_next_earnings_summary_with_status.return_value = (
        None,
        None,
        False,
    )
    layer = DataPreparationLayer.__new__(DataPreparationLayer)
    layer._max_retries = 2
    layer._retry_base_delay = 0.01
    layer._yfinance_service = MagicMock()
    layer._rate_limiter = MagicMock()
    layer.price_cache = price_cache
    layer.benchmark_cache = MagicMock()
    layer.benchmark_cache.get_benchmark_data.return_value = _make_price_df(
        price=450.0
    )
    layer.benchmark_cache.get_benchmark_symbol.return_value = "SPY"
    layer.fundamentals_cache = fundamentals_cache
    layer.event_context_service = event_context
    return layer, event_context


def _strong_screener_result() -> dict[str, object]:
    """A row that passes every survivor gate when calendar evidence exists."""
    return {
        "avg_dollar_volume": 150_000_000,
        "data_status": "complete",
        "is_scannable": True,
        "rs_rating_1m": 90.0,
        "rs_rating_3m": 80.0,
        "stage": 2,
        "ma_alignment": True,
        "setup_engine": {
            "pattern_primary": "vcp",
            "bb_squeeze": True,
            "tight_closes_count": 3,
            "quiet_days_10d": 3,
            "volume_vs_50d": 0.7,
            "rs_vs_spy_65d": 0.08,
            "rs_line_new_high": True,
            "rs_line_blue_dot": False,
            "setup_ready": True,
            "in_early_zone": True,
            "extended_from_pivot": False,
            "explain": {"invalidation_flags": []},
        },
    }


REQUIREMENTS = DataRequirements(needs_benchmark=True, needs_event_calendar=True)


class TestPersistedEvidenceToSurvivorClassification:
    def test_fresh_observation_classifies_strong_row_as_survivor(
        self, price_data
    ):
        as_of_date = price_data.index[-1].date()
        layer, _event_context = _make_layer(
            price_data,
            fundamentals={
                "event_calendar_as_of_date": as_of_date,
                "next_earnings_date": as_of_date + timedelta(days=60),
            },
        )

        stock_data = layer.prepare_data_bulk(
            ["AAPL"], REQUIREMENTS
        )["AAPL"]

        assert stock_data.event_calendar_available is True
        projection = build_opportunity_projection(
            _strong_screener_result(),
            stock_data,
            SetupEngineParameters(),
        )

        assert projection["correction_survivor"] is True
        assert projection["action_state"] == "setup_ready"

    def test_successful_empty_observation_keeps_row_survivor_eligible(
        self, price_data
    ):
        """Known calendar with no upcoming earnings must not block survivors."""
        as_of_date = price_data.index[-1].date()
        layer, _event_context = _make_layer(
            price_data,
            fundamentals={
                "event_calendar_as_of_date": as_of_date,
                "next_earnings_date": None,
            },
        )

        stock_data = layer.prepare_data_bulk(["AAPL"], REQUIREMENTS)["AAPL"]

        assert stock_data.event_calendar_available is True
        projection = build_opportunity_projection(
            _strong_screener_result(),
            stock_data,
            SetupEngineParameters(),
        )

        assert projection["correction_survivor"] is True

    @pytest.mark.parametrize(
        "observation_age_days",
        [EVENT_CALENDAR_MAX_AGE_DAYS + 1, 30],
        ids=["one-day-past-window", "monthly-stale"],
    )
    def test_stale_observation_fails_closed_to_data_limited(
        self, price_data, observation_age_days
    ):
        as_of_date = price_data.index[-1].date()
        layer, _event_context = _make_layer(
            price_data,
            fundamentals={
                "event_calendar_as_of_date": (
                    as_of_date - timedelta(days=observation_age_days)
                ),
                "next_earnings_date": as_of_date + timedelta(days=11),
            },
        )

        stock_data = layer.prepare_data_bulk(["AAPL"], REQUIREMENTS)["AAPL"]

        assert stock_data.event_calendar_available is False
        projection = build_opportunity_projection(
            _strong_screener_result(),
            stock_data,
            SetupEngineParameters(),
        )

        assert projection["correction_survivor"] is False
        assert projection["action_state"] == "data_limited"
        # The score is computable from non-calendar inputs, but the row is
        # explicitly NOT a survivor: fail-closed on missing evidence.
        assert "required_evidence" in projection["opportunity_state"][
            "failed_checks"
        ]

    def test_producer_observation_round_trips_through_persisted_gate(
        self, price_data
    ):
        """The exact payload a fixed producer stamps must satisfy the gate."""
        from app.services.yahoo_earnings_calendar import (
            normalize_yahoo_earnings_dates,
            stamp_event_calendar_observation,
        )

        expected_next_date = datetime.now(UTC).date() + timedelta(days=9)
        frame = pd.DataFrame(
            {
                "Earnings Date": [
                    pd.Timestamp(expected_next_date)
                ],
                "EPS Estimate": [1.25],
            }
        ).set_index("Earnings Date")
        stamped = {}
        stamped.update(
            stamp_event_calendar_observation(
                *normalize_yahoo_earnings_dates(frame, symbol="AAPL")
            )
        )
        assert stamped, "producer must stamp an observation on success"

        layer, _event_context = _make_layer(price_data, fundamentals=stamped)

        stock_data = layer.prepare_data_bulk(["AAPL"], REQUIREMENTS)["AAPL"]

        assert stock_data.event_calendar_available is True
        # The persisted gate must accept exactly what the producer stamped;
        # derive the expectation from the stamp, not a second clock read.
        assert stock_data.next_earnings_date == stamped["next_earnings_date"]
        assert stock_data.next_earnings_date == expected_next_date
