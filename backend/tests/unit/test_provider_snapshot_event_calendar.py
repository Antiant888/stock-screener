"""Regression tests for event-calendar evidence in provider-snapshot hydration.

Guards the two producer-side gaps that left every persisted row without
event-calendar evidence (and therefore every Correction Survivors surface
empty):

- hydration completeness is keyed on the calendar *observation age*, not
  on ``next_earnings_date`` being non-null (gap 2), and
- a fresh Yahoo observation wins over a stale persisted one during the
  hydration merge (``_merge_fundamentals`` prefers non-null primary
  values, which would otherwise keep stale evidence forever).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

# Producer completeness compares against UTC "today"; construct payloads
# from the same clock so boundary tests are immune to local-timezone skew.
def _utc_today() -> date:
    return datetime.now(UTC).date()
from types import SimpleNamespace

import pandas as pd
import pytest

from app.services.provider_snapshot_service import ProviderSnapshotService
from app.services.yahoo_earnings_calendar import stamp_event_calendar_observation
from tests.unit.test_provider_snapshot_service import (
    _StubFundamentalsCache,
    _StubPriceCache,
    _StubTechnicalCalc,
    _make_provider_snapshot_service,
    _make_session,
)

_TEST_SYMBOL = "AAPL"
_OLD_REPORT = {"report_ts": "2026-04-22T16:00:00-04:00", "eps_estimate": 1.6, "reported_eps": 1.7, "surprise_pct": 6.25}
_NEW_REPORT = {"report_ts": "2026-07-22T16:00:00-04:00", "eps_estimate": 1.73, "reported_eps": 1.54, "surprise_pct": -10.88}


def _seed_published_run(db, *, payload: dict) -> None:
    from app.models.provider_snapshot import (
        ProviderSnapshotPointer,
        ProviderSnapshotRow,
        ProviderSnapshotRun,
    )
    from app.models.stock_universe import UNIVERSE_STATUS_ACTIVE, StockUniverse

    db.add(
        StockUniverse(
            symbol=_TEST_SYMBOL,
            exchange="NASDAQ",
            is_active=True,
            status=UNIVERSE_STATUS_ACTIVE,
            status_reason="active",
        )
    )
    run = ProviderSnapshotRun(
        snapshot_key=ProviderSnapshotService.SNAPSHOT_KEY_FUNDAMENTALS,
        run_mode="publish",
        status="published",
        source_revision="fundamentals_v1:20260919010101",
        created_at=datetime.now(UTC),
        published_at=datetime.now(UTC),
    )
    db.add(run)
    db.flush()
    db.add(
        ProviderSnapshotRow(
            run_id=run.id,
            symbol=_TEST_SYMBOL,
            exchange="NASDAQ",
            row_hash="row-hash",
            normalized_payload_json=json.dumps(payload, default=str),
            raw_payload_json=None,
        )
    )
    db.add(
        ProviderSnapshotPointer(
            snapshot_key=ProviderSnapshotService.SNAPSHOT_KEY_FUNDAMENTALS,
            run_id=run.id,
        )
    )
    db.commit()


def _hydrate_once(db, *, cached: dict | None, yahoo_payload: dict) -> dict:
    """Run one hydration with a stubbed Yahoo producer; return (stats, cache)."""
    cache = _StubFundamentalsCache(cached=cached)
    service = _make_provider_snapshot_service(fundamentals_cache=cache)
    service.price_cache = _StubPriceCache()
    service.technical_calc = _StubTechnicalCalc()
    service._fetch_yahoo_only_fields = lambda symbol: dict(yahoo_payload)
    stats = service.hydrate_published_snapshot(db)
    return stats, cache


def _complete_yahoo_payload(**calendar) -> dict:
    payload = {
        key: "present"
        for key in ProviderSnapshotService.YAHOO_ONLY_REQUIRED_KEYS
    }
    payload.update(calendar)
    return payload


class TestCalendarCompletenessIsAgeBased:
    def _payload(self, **calendar) -> dict:
        payload = {
            key: "present"
            for key in ProviderSnapshotService.YAHOO_ONLY_REQUIRED_KEYS
        }
        payload.update(calendar)
        return payload

    def test_missing_observation_needs_yahoo_hydration(self):
        service = _make_provider_snapshot_service()

        assert service._needs_yahoo_hydration(self._payload()) is True

    def test_fresh_observation_with_null_next_date_is_complete(self):
        """A successful lookup with no upcoming earnings is evidence-complete."""
        service = _make_provider_snapshot_service()

        payload = self._payload(
            event_calendar_as_of_date=_utc_today(),
            next_earnings_date=None,
        )

        assert service._needs_yahoo_hydration(payload) is False

    def test_observation_at_refresh_threshold_is_complete(self):
        """An observation at one cadence week still satisfies completeness."""
        from app.services.yahoo_earnings_calendar import (
            EVENT_CALENDAR_PRODUCER_REFRESH_AFTER_DAYS,
        )

        service = _make_provider_snapshot_service()

        payload = self._payload(
            event_calendar_as_of_date=(
                _utc_today()
                - timedelta(days=EVENT_CALENDAR_PRODUCER_REFRESH_AFTER_DAYS)
            ),
            next_earnings_date=None,
        )

        assert service._needs_yahoo_hydration(payload) is False

    def test_observation_one_day_past_refresh_threshold_needs_hydration(self):
        """Weekly runs must refresh before the consumer TTL can reject evidence."""
        from app.services.yahoo_earnings_calendar import (
            EVENT_CALENDAR_PRODUCER_REFRESH_AFTER_DAYS,
        )

        service = _make_provider_snapshot_service()

        # Off-boundary (+2) so a local/UTC clock read can't flip the verdict.
        payload = self._payload(
            event_calendar_as_of_date=(
                _utc_today()
                - timedelta(days=EVENT_CALENDAR_PRODUCER_REFRESH_AFTER_DAYS + 2)
            ),
            next_earnings_date=None,
        )

        assert service._needs_yahoo_hydration(payload) is True

    def test_observation_between_refresh_threshold_and_consumer_ttl_is_due(
        self,
    ):
        """Evidence the consumer still accepts may still require re-observation."""
        from app.services.yahoo_earnings_calendar import (
            EVENT_CALENDAR_MAX_AGE_DAYS,
            EVENT_CALENDAR_PRODUCER_REFRESH_AFTER_DAYS,
        )

        assert EVENT_CALENDAR_PRODUCER_REFRESH_AFTER_DAYS < EVENT_CALENDAR_MAX_AGE_DAYS

        service = _make_provider_snapshot_service()

        # Off-boundary: 2 days before the consumer TTL but past the weekly
        # refresh threshold, so a UTC-midnight straddle can't flip it.
        payload = self._payload(
            event_calendar_as_of_date=(
                _utc_today() - timedelta(days=EVENT_CALENDAR_MAX_AGE_DAYS - 2)
            ),
            next_earnings_date=None,
        )

        assert service._needs_yahoo_hydration(payload) is True

    def test_stale_observation_beyond_consumer_ttl_needs_yahoo_hydration(self):
        from app.services.yahoo_earnings_calendar import EVENT_CALENDAR_MAX_AGE_DAYS

        service = _make_provider_snapshot_service()

        payload = self._payload(
            event_calendar_as_of_date=(
                _utc_today() - timedelta(days=EVENT_CALENDAR_MAX_AGE_DAYS + 1)
            ),
            next_earnings_date=date(2026, 1, 5),
        )

        assert service._needs_yahoo_hydration(payload) is True


class TestHydrationReconcilesCalendarEvidence:
    def test_fresh_yahoo_observation_overrides_stale_persisted_evidence(self):
        observation = _utc_today()
        stale_date = observation - timedelta(days=30)
        fresh_next = observation + timedelta(days=9)
        db = _make_session()()
        _seed_published_run(
            db,
            payload={
                "symbol": _TEST_SYMBOL,
                "event_calendar_as_of_date": stale_date,
                "next_earnings_date": date(2026, 1, 5),
            },
        )

        stats, cache = _hydrate_once(
            db,
            cached={
                _TEST_SYMBOL: {
                    "event_calendar_as_of_date": stale_date,
                    "next_earnings_date": date(2026, 1, 5),
                    "recent_earnings": [_OLD_REPORT],
                },
            },
            yahoo_payload=_complete_yahoo_payload(
                event_calendar_as_of_date=observation,
                next_earnings_date=fresh_next,
                recent_earnings=[_NEW_REPORT, _OLD_REPORT],
            ),
        )

        stored = cache.stored[_TEST_SYMBOL]
        assert stats["yahoo_hydrated"] == 1
        assert stats["missing_yahoo"] == 0
        assert stored["event_calendar_as_of_date"] == observation
        assert stored["next_earnings_date"] == fresh_next
        assert stored["recent_earnings"] == [_NEW_REPORT, _OLD_REPORT]

    def test_fresh_observation_without_reports_keeps_cached_history(self):
        observation = _utc_today()
        stale_date = observation - timedelta(days=30)
        db = _make_session()()
        _seed_published_run(
            db,
            payload={"symbol": _TEST_SYMBOL, "event_calendar_as_of_date": stale_date},
        )

        _stats, cache = _hydrate_once(
            db,
            cached={
                _TEST_SYMBOL: {
                    "event_calendar_as_of_date": stale_date,
                    "recent_earnings": [_OLD_REPORT],
                },
            },
            yahoo_payload=_complete_yahoo_payload(
                **stamp_event_calendar_observation([], [], True)
            ),
        )

        stored = cache.stored[_TEST_SYMBOL]
        assert stored["event_calendar_as_of_date"] == observation
        assert stored["recent_earnings"] == [_OLD_REPORT]

    def test_fresh_cached_evidence_survives_failed_yahoo_lookup(self):
        """A stale snapshot must not clobber fresher cached evidence.

        ``_merge_fundamentals`` prefers non-null primary (snapshot) values;
        when the Yahoo refresh fails, the newest available observation —
        here the fresh cached one — must win before the completeness check,
        both for the missing_yahoo accounting and for what ``store()``
        persists.
        """
        observation = _utc_today()
        stale_date = observation - timedelta(days=30)
        fresh_next = observation + timedelta(days=9)
        db = _make_session()()
        _seed_published_run(
            db,
            payload={
                "symbol": _TEST_SYMBOL,
                "event_calendar_as_of_date": stale_date,
                "next_earnings_date": date(2026, 1, 5),
            },
        )

        stats, cache = _hydrate_once(
            db,
            cached={
                _TEST_SYMBOL: {
                    "event_calendar_as_of_date": observation,
                    "next_earnings_date": fresh_next,
                    "recent_earnings": [_NEW_REPORT],
                },
            },
            # Yahoo calendar lookup failed this run: no observation stamp.
            yahoo_payload=_complete_yahoo_payload(),
        )

        stored = cache.stored[_TEST_SYMBOL]
        # The stale snapshot evidence must not overwrite the fresh cache.
        assert stored["event_calendar_as_of_date"] == observation
        assert stored["next_earnings_date"] == fresh_next
        assert stored["recent_earnings"] == [_NEW_REPORT]
        # Completeness accounting sees the reconciled fresh evidence.
        assert stats["missing_yahoo"] == 0

    def test_failed_yahoo_calendar_lookup_keeps_row_unavailable(self):
        db = _make_session()()
        _seed_published_run(
            db,
            payload={"symbol": _TEST_SYMBOL, "market_cap": 1000},
        )

        stats, cache = _hydrate_once(db, cached={}, yahoo_payload={})

        stored = cache.stored[_TEST_SYMBOL]
        assert "event_calendar_as_of_date" not in stored
        assert stats["missing_yahoo"] == 1

    def test_hydration_disabled_keeps_stale_evidence_reported_missing(self):
        stale_date = date.today() - timedelta(days=30)
        db = _make_session()()
        _seed_published_run(
            db,
            payload={
                "symbol": _TEST_SYMBOL,
                "event_calendar_as_of_date": stale_date,
                "next_earnings_date": date(2026, 1, 5),
            },
        )

        cache = _StubFundamentalsCache()
        service = _make_provider_snapshot_service(fundamentals_cache=cache)
        service.price_cache = _StubPriceCache()
        service.technical_calc = _StubTechnicalCalc()
        yahoo_calls: list[str] = []
        service._fetch_yahoo_only_fields = (
            lambda symbol: yahoo_calls.append(symbol) or {}
        )

        stats = service.hydrate_published_snapshot(
            db,
            allow_yahoo_hydration=False,
        )

        assert yahoo_calls == []
        assert stats["missing_yahoo"] == 1
        stored = cache.stored[_TEST_SYMBOL]
        # The stale snapshot evidence flows through untouched (ISO payload
        # form); only the completeness accounting reflects the staleness.
        assert stored["event_calendar_as_of_date"] in (
            stale_date,
            stale_date.isoformat(),
        )


class TestYahooOnlyFieldsStampsCalendar:
    def test_fetch_yahoo_only_fields_includes_fresh_observation(self, monkeypatch):
        before = datetime.now(UTC).date()
        earnings_at = pd.Timestamp(before + timedelta(days=7))
        ticker = SimpleNamespace(
            info={"symbol": _TEST_SYMBOL},
            quarterly_income_stmt=None,
            annual_income_stmt=None,
            quarterly_balance_sheet=None,
            earnings_dates=pd.DataFrame(
                {"Earnings Date": [earnings_at], "EPS Estimate": [1.25]}
            ).set_index("Earnings Date"),
        )

        monkeypatch.setattr(
            "yfinance.Ticker",
            lambda _symbol: ticker,
        )

        service = _make_provider_snapshot_service()
        result = service._fetch_yahoo_only_fields(_TEST_SYMBOL)
        after = datetime.now(UTC).date()

        assert result["next_earnings_date"] == earnings_at.date()
        assert result["event_calendar_as_of_date"] in {before, after}

    def test_fetch_yahoo_only_fields_omits_marker_on_provider_failure(self, monkeypatch):
        class FailingCalendarTicker:
            def __init__(self):
                self.info = {"symbol": _TEST_SYMBOL}

            @property
            def earnings_dates(self):
                raise RuntimeError("calendar unavailable")

        monkeypatch.setattr(
            "yfinance.Ticker",
            lambda _symbol: FailingCalendarTicker(),
        )

        service = _make_provider_snapshot_service()
        result = service._fetch_yahoo_only_fields(_TEST_SYMBOL)

        assert "event_calendar_as_of_date" not in result
        assert "next_earnings_date" not in result
