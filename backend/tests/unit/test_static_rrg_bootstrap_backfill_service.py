from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.domain.relative_strength import BALANCED_RS_FORMULA_VERSION
from app.models.industry import IBDGroupRank
from app.models.stock_universe import StockUniverse
from app.services.group_rank_snapshot_coordinator import (
    GroupBackfillReport,
    GroupSnapshotResult,
    GroupSnapshotStatus,
)
from app.services.point_in_time_universe_service import (
    hash_point_in_time_universe_symbols,
)
from app.services.rrg_service import MIN_TAIL_WEEKS
from app.services.static_rrg_bootstrap_backfill_service import (
    DEFAULT_STATIC_RRG_BOOTSTRAP_LOOKBACK_DAYS,
    STATIC_RRG_BOOTSTRAP_UNIVERSE_POLICY,
    StaticRRGBootstrapBackfillService,
    StaticRRGBootstrapBackfillStatus,
)
from app.services.static_rrg_bootstrap_universe import StaticRRGBootstrapUniverse


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[StockUniverse.__table__, IBDGroupRank.__table__],
    )
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def test_bootstrap_universe_uses_current_active_market_rows_for_historical_date():
    engine, factory = _session_factory()
    try:
        with factory() as db:
            db.add_all(
                [
                    StockUniverse(symbol="AAPL", market="US", is_active=True),
                    StockUniverse(symbol="MSFT", market="US", is_active=True),
                    StockUniverse(symbol="ZZZ", market="US", is_active=False),
                    StockUniverse(symbol="0700.HK", market="HK", is_active=True),
                ]
            )
            db.commit()

            universe = StaticRRGBootstrapUniverse().resolve(
                db,
                market="US",
                as_of_date=date(2026, 4, 17),
            )

        assert universe.market == "US"
        assert universe.as_of_date == date(2026, 4, 17)
        assert universe.symbols == ("AAPL", "MSFT")
        assert universe.universe_hash == hash_point_in_time_universe_symbols(
            universe.symbols
        )
    finally:
        engine.dispose()


def test_bootstrap_backfill_materializes_min_tail_weekly_targets():
    engine, factory = _session_factory()
    through_date = date(2026, 7, 24)
    start = through_date - timedelta(days=DEFAULT_STATIC_RRG_BOOTSTRAP_LOOKBACK_DAYS)
    trading_days = [
        start + timedelta(days=offset)
        for offset in range((through_date - start).days + 1)
        if (start + timedelta(days=offset)).weekday() < 5
    ]
    captured_dates: list[date] = []

    class FakeCalendar:
        def trading_days(self, market, range_start, range_end):
            assert market == "US"
            assert range_start == start
            assert range_end == through_date
            return trading_days

    class FakeCoordinator:
        def backfill(self, db, *, identities, continue_on_error):
            assert continue_on_error is True
            captured = tuple(identities)
            captured_dates.extend(identity.as_of_date for identity in captured)
            return GroupBackfillReport(
                results=tuple(
                    GroupSnapshotResult(
                        identity=identity,
                        status=GroupSnapshotStatus.PROCESSED,
                        row_count=1,
                        market_rs_run_id=index,
                    )
                    for index, identity in enumerate(captured, start=1)
                )
            )

    try:
        with factory() as db:
            result = StaticRRGBootstrapBackfillService(
                calendar_service=FakeCalendar(),
                group_snapshot_coordinator=FakeCoordinator(),
            ).backfill(
                db,
                market="US",
                through_date=through_date,
                formula_version=BALANCED_RS_FORMULA_VERSION,
            )

        expected_weekly_targets = (
            date(2026, 5, 8),
            date(2026, 5, 15),
            date(2026, 5, 22),
            date(2026, 5, 29),
            date(2026, 6, 5),
            date(2026, 6, 12),
            date(2026, 6, 19),
            date(2026, 6, 26),
            date(2026, 7, 3),
            date(2026, 7, 10),
            date(2026, 7, 17),
            date(2026, 7, 24),
        )
        assert tuple(captured_dates) == expected_weekly_targets
        assert result.as_dict() == {
            "status": "completed",
            "market": "US",
            "as_of_date": "2026-07-24",
            "formula_version": BALANCED_RS_FORMULA_VERSION,
            "policy": STATIC_RRG_BOOTSTRAP_UNIVERSE_POLICY,
            "lookback_start_date": start.isoformat(),
            "target_dates": [day.isoformat() for day in expected_weekly_targets],
            "existing": 0,
            "processed": MIN_TAIL_WEEKS,
            "errors": 0,
            "total_dates": MIN_TAIL_WEEKS,
        }
    finally:
        engine.dispose()


def test_default_bootstrap_lookback_is_scoped_to_minimum_rrg_tail():
    engine, factory = _session_factory()
    through_date = date(2026, 7, 24)
    expected_start = through_date - timedelta(weeks=MIN_TAIL_WEEKS + 2)
    trading_days = [
        expected_start + timedelta(days=offset)
        for offset in range((through_date - expected_start).days + 1)
        if (expected_start + timedelta(days=offset)).weekday() < 5
    ]
    range_calls: list[tuple[date, date]] = []

    class FakeCalendar:
        def trading_days(self, market, range_start, range_end):
            assert market == "US"
            range_calls.append((range_start, range_end))
            return trading_days

    class FakeCoordinator:
        def backfill(self, db, *, identities, continue_on_error):
            return GroupBackfillReport(
                results=tuple(
                    GroupSnapshotResult(
                        identity=identity,
                        status=GroupSnapshotStatus.PROCESSED,
                        row_count=1,
                        market_rs_run_id=index,
                    )
                    for index, identity in enumerate(tuple(identities), start=1)
                )
            )

    try:
        with factory() as db:
            result = StaticRRGBootstrapBackfillService(
                calendar_service=FakeCalendar(),
                group_snapshot_coordinator=FakeCoordinator(),
            ).backfill(
                db,
                market="US",
                through_date=through_date,
                formula_version=BALANCED_RS_FORMULA_VERSION,
            )

        assert range_calls == [(expected_start, through_date)]
        assert result.status is StaticRRGBootstrapBackfillStatus.COMPLETED
    finally:
        engine.dispose()


def test_weekly_targets_use_latest_day_per_week_without_sorted_input():
    targets = StaticRRGBootstrapBackfillService._weekly_targets(
        [
            date(2026, 1, 9),
            date(2026, 1, 5),
            date(2026, 1, 7),
            date(2026, 1, 16),
            date(2026, 1, 12),
        ]
    )

    assert targets == (date(2026, 1, 9), date(2026, 1, 16))


def test_bootstrap_backfill_rejects_legacy_formula_before_snapshot_dispatch():
    engine, factory = _session_factory()
    through_date = date(2026, 7, 24)
    start = through_date - timedelta(days=DEFAULT_STATIC_RRG_BOOTSTRAP_LOOKBACK_DAYS)

    class FakeCalendar:
        def trading_days(self, *_args, **_kwargs):
            raise AssertionError("unsupported formula must not enumerate dates")

    class FakeCoordinator:
        def backfill(self, *_args, **_kwargs):
            raise AssertionError("unsupported formula must not dispatch snapshots")

    try:
        with factory() as db:
            result = StaticRRGBootstrapBackfillService(
                calendar_service=FakeCalendar(),
                group_snapshot_coordinator=FakeCoordinator(),
            ).backfill(
                db,
                market="US",
                through_date=through_date,
                formula_version="legacy-linear-v1",
            )

        assert result.status is StaticRRGBootstrapBackfillStatus.ERRORED
        assert result.as_dict() == {
            "status": "errored",
            "market": "US",
            "as_of_date": "2026-07-24",
            "formula_version": "legacy-linear-v1",
            "policy": STATIC_RRG_BOOTSTRAP_UNIVERSE_POLICY,
            "lookback_start_date": start.isoformat(),
            "target_dates": [],
            "existing": 0,
            "processed": 0,
            "errors": 1,
            "total_dates": 0,
            "reason": "unsupported_formula",
            "error": "Static RRG bootstrap only supports canonical Market RS",
        }
    finally:
        engine.dispose()
