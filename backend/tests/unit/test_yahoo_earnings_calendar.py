"""Contract tests for the shared Yahoo earnings-calendar normalizer.

Guards the success-empty vs provider-failure distinction and the shared
freshness window consumed by the fail-closed survivor gate.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from app.services.yahoo_earnings_calendar import (
    EVENT_CALENDAR_FUTURE_TOLERANCE_DAYS,
    EVENT_CALENDAR_MAX_AGE_DAYS,
    EVENT_CALENDAR_PRODUCER_REFRESH_AFTER_DAYS,
    is_event_calendar_observation_due_for_refresh,
    is_event_calendar_observation_fresh,
    normalize_yahoo_earnings_dates,
    stamp_event_calendar_observation,
)


def _frame(values):
    return pd.DataFrame({"Earnings Date": values, "EPS Estimate": [1.0] * len(values)}).set_index(
        "Earnings Date"
    )


class TestNormalizeYahooEarningsDates:
    def test_successful_lookup_with_rows_returns_normalized_dates(self):
        first = datetime(2026, 9, 25, 20, 0)
        second = datetime(2026, 12, 18, 20, 0)
        frame = _frame([second, first])

        dates, _recent, available = normalize_yahoo_earnings_dates(frame, symbol="AAPL")

        assert dates == [date(2026, 9, 25), date(2026, 12, 18)]
        assert available is True

    def test_successful_empty_response_is_available_not_failure(self):
        dates, _recent, available = normalize_yahoo_earnings_dates(pd.DataFrame())

        assert dates == []
        assert available is True

    def test_none_frame_is_available_not_failure(self):
        dates, _recent, available = normalize_yahoo_earnings_dates(None)

        assert dates == []
        assert available is True

    def test_provider_failure_is_reported_as_unavailable(self):
        class ExplodingFrame:
            @property
            def empty(self):
                raise RuntimeError("provider outage")

        assert normalize_yahoo_earnings_dates(ExplodingFrame(), symbol="AAPL") == ([], [], False)

    def test_unparsable_rows_are_skipped_not_failures(self):
        frame = _frame(["not-a-date", datetime(2026, 10, 2, 16, 0)])

        dates, _recent, available = normalize_yahoo_earnings_dates(frame, symbol="AAPL")

        assert dates == [date(2026, 10, 2)]
        assert available is True

    def test_na_and_nat_cells_do_not_fail_the_lookup(self):
        """Missing cells must not bool()-evaluate or abort a successful lookup."""
        frame = pd.DataFrame(
            {
                "Earnings Date": [pd.NA, pd.NaT, datetime(2026, 10, 2, 16, 0)],
                "EPS Estimate": [1.0, 1.0, 1.0],
            }
        )

        dates, _recent, available = normalize_yahoo_earnings_dates(frame, symbol="AAPL")

        assert dates == [date(2026, 10, 2)]
        assert available is True

    def test_list_like_cell_is_skipped_without_aborting_the_lookup(self):
        """A non-scalar cell must not poison isna or abort later rows."""
        frame = pd.DataFrame(
            {
                "Earnings Date": [
                    ["bad", "2026-09-01"],
                    datetime(2026, 10, 2, 16, 0),
                ],
                "EPS Estimate": [1.0, 1.0],
            }
        )

        dates, _recent, available = normalize_yahoo_earnings_dates(frame, symbol="AAPL")

        assert dates == [date(2026, 10, 2)]
        assert available is True

    def test_wholly_unparseable_rows_are_provider_failure_not_empty_success(self):
        """Garbage input must not stamp a known no-upcoming-earnings observation."""
        frame = _frame(["not-a-date", {"unexpected": "shape"}])

        dates, _recent, available = normalize_yahoo_earnings_dates(frame, symbol="AAPL")

        assert dates == []
        assert available is False

    def test_nonempty_frame_with_wrong_field_names_is_unavailable(self):
        frame = pd.DataFrame({"Some Other Column": [datetime(2026, 10, 2)]})

        dates, _recent, available = normalize_yahoo_earnings_dates(frame, symbol="AAPL")

        assert dates == []
        assert available is False

    def test_limit_caps_normalized_rows(self):
        values = [datetime(2026, 10, 1 + offset) for offset in range(6)]
        dates, _recent, _available = normalize_yahoo_earnings_dates(_frame(values), limit=3)

        assert len(dates) == 3

    def test_yahoo_frame_keeps_the_newest_reported_quarters(self):
        nan = float("nan")
        index = pd.DatetimeIndex(
            [
                pd.Timestamp("2026-11-19 16:00", tz="America/New_York"),
                pd.Timestamp("2026-08-27 16:00", tz="America/New_York"),
                pd.Timestamp("2026-05-28 16:00", tz="America/New_York"),
                pd.Timestamp("2026-02-25 07:00", tz="America/New_York"),
                pd.Timestamp("2025-11-19 16:00", tz="America/New_York"),
                pd.Timestamp("2025-08-27 16:00", tz="America/New_York"),
                pd.Timestamp("2025-05-28 16:00", tz="America/New_York"),
            ],
            name="Earnings Date",
        )
        frame = pd.DataFrame(
            {
                "EPS Estimate": [1.25, 1.01, 0.93, nan, 0.75, 0.64, 0.73],
                "Reported EPS": [nan, 1.05, 0.83, 0.89, nan, 0.68, 0.76],
                "Surprise(%)": [nan, 3.96, -10.88, nan, nan, 6.25, 4.11],
            },
            index=index,
        )

        dates, recent, available = normalize_yahoo_earnings_dates(frame, symbol="NVDA")

        assert dates == [
            date(2026, 2, 25),
            date(2026, 5, 28),
            date(2026, 8, 27),
            date(2026, 11, 19),
        ]
        assert recent == [
            {
                "report_ts": "2026-08-27T16:00:00-04:00",
                "eps_estimate": 1.01,
                "reported_eps": 1.05,
                "surprise_pct": 3.96,
            },
            {
                "report_ts": "2026-05-28T16:00:00-04:00",
                "eps_estimate": 0.93,
                "reported_eps": 0.83,
                "surprise_pct": -10.88,
            },
            {
                "report_ts": "2026-02-25T07:00:00-05:00",
                "eps_estimate": None,
                "reported_eps": 0.89,
                "surprise_pct": None,
            },
            {
                "report_ts": "2025-08-27T16:00:00-04:00",
                "eps_estimate": 0.64,
                "reported_eps": 0.68,
                "surprise_pct": 6.25,
            },
        ]
        assert available is True

    def test_report_without_a_timezone_is_not_kept(self):
        frame = pd.DataFrame(
            {"Earnings Date": [datetime(2026, 8, 27, 16, 0)], "Reported EPS": [1.05]}
        ).set_index("Earnings Date")

        assert normalize_yahoo_earnings_dates(frame, symbol="AAPL") == (
            [date(2026, 8, 27)],
            [],
            True,
        )


REPORTED = {
    "report_ts": "2026-07-22T16:00:00-04:00",
    "eps_estimate": 1.73,
    "reported_eps": 1.54,
    "surprise_pct": -10.88,
}


class TestStampEventCalendarObservation:
    def test_success_stamps_observation_next_date_and_recent_earnings(self):
        stamped = stamp_event_calendar_observation(
            [date(2026, 9, 25), date(2026, 12, 18)],
            [REPORTED],
            True,
            observed_at=date(2026, 9, 20),
        )

        assert stamped == {
            "event_calendar_as_of_date": date(2026, 9, 20),
            "next_earnings_date": date(2026, 9, 25),
            "recent_earnings": [REPORTED],
        }

    def test_success_without_future_date_or_reports_leaves_history_unstamped(self):
        """No reported rows is not "no history": a partial response must not
        overwrite the stored quarters."""
        stamped = stamp_event_calendar_observation(
            [date(2026, 1, 5)],
            [],
            True,
            observed_at=date(2026, 9, 20),
        )

        assert stamped == {
            "event_calendar_as_of_date": date(2026, 9, 20),
            "next_earnings_date": None,
        }

    def test_failure_stamps_nothing(self):
        assert stamp_event_calendar_observation([], [], False) == {}

    def test_failure_with_dates_and_reports_still_stamps_nothing(self):
        assert stamp_event_calendar_observation([date(2026, 9, 25)], [REPORTED], False) == {}

    def test_default_observation_is_today_utc(self):
        before = datetime.now(UTC).date()
        stamped = stamp_event_calendar_observation([], [], True)
        after = datetime.now(UTC).date()

        assert stamped["event_calendar_as_of_date"] in {before, after}
        assert stamped["next_earnings_date"] is None


class TestObservationFreshness:
    def test_shared_window_is_wider_than_weekly_cadence(self):
        assert EVENT_CALENDAR_MAX_AGE_DAYS > 7
        assert EVENT_CALENDAR_FUTURE_TOLERANCE_DAYS >= 0

    def test_same_day_observation_is_fresh(self):
        today = date(2026, 9, 20)

        assert is_event_calendar_observation_fresh(today, reference_date=today) is True

    def test_observation_within_max_age_is_fresh(self):
        today = date(2026, 9, 20)

        assert (
            is_event_calendar_observation_fresh(
                today - timedelta(days=EVENT_CALENDAR_MAX_AGE_DAYS),
                reference_date=today,
            )
            is True
        )

    def test_observation_one_day_past_max_age_is_stale(self):
        today = date(2026, 9, 20)

        assert (
            is_event_calendar_observation_fresh(
                today - timedelta(days=EVENT_CALENDAR_MAX_AGE_DAYS + 1),
                reference_date=today,
            )
            is False
        )

    def test_observation_beyond_future_tolerance_is_not_fresh(self):
        today = date(2026, 9, 20)

        assert (
            is_event_calendar_observation_fresh(
                today + timedelta(days=EVENT_CALENDAR_FUTURE_TOLERANCE_DAYS + 1),
                reference_date=today,
            )
            is False
        )

    def test_future_observation_within_tolerance_is_fresh(self):
        today = date(2026, 9, 20)

        assert (
            is_event_calendar_observation_fresh(
                today + timedelta(days=EVENT_CALENDAR_FUTURE_TOLERANCE_DAYS),
                reference_date=today,
            )
            is True
        )

    @pytest.mark.parametrize(
        "raw",
        [None, "", "not-a-date", float("nan")],
        ids=["none", "empty", "garbage", "nan"],
    )
    def test_unusable_observation_is_stale(self, raw):
        assert is_event_calendar_observation_fresh(raw) is False

    def test_accepts_iso_string_observation(self):
        today = date(2026, 9, 20)

        assert (
            is_event_calendar_observation_fresh("2026-09-18", reference_date=today)
            is True
        )


class TestProducerRefreshThreshold:
    """Producer completeness must refresh strictly before the consumer TTL.

    With a weekly cadence, an observation older than one week must be
    re-observed on the next scheduled run so the consumer gate (14 days)
    never rejects evidence that a weekly producer could have refreshed.
    """

    def test_producer_threshold_is_shorter_than_consumer_ttl(self):
        assert (
            EVENT_CALENDAR_PRODUCER_REFRESH_AFTER_DAYS
            < EVENT_CALENDAR_MAX_AGE_DAYS
        )
        # Weekly cadence: refresh threshold must not exceed one cadence week.
        assert EVENT_CALENDAR_PRODUCER_REFRESH_AFTER_DAYS <= 7

    def test_observation_within_refresh_threshold_is_not_due(self):
        today = date(2026, 9, 20)

        assert (
            is_event_calendar_observation_due_for_refresh(
                today - timedelta(days=EVENT_CALENDAR_PRODUCER_REFRESH_AFTER_DAYS),
                reference_date=today,
            )
            is False
        )

    def test_observation_one_day_past_refresh_threshold_is_due(self):
        today = date(2026, 9, 20)

        assert (
            is_event_calendar_observation_due_for_refresh(
                today - timedelta(days=EVENT_CALENDAR_PRODUCER_REFRESH_AFTER_DAYS + 1),
                reference_date=today,
            )
            is True
        )

    def test_missing_or_unusable_observation_is_due(self):
        assert is_event_calendar_observation_due_for_refresh(None) is True
        assert is_event_calendar_observation_due_for_refresh("garbage") is True
