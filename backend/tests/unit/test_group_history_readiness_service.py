from __future__ import annotations

from datetime import date, timedelta

from app.domain.relative_strength import GroupSnapshotIdentity
from app.services.group_history_reconciliation import GroupHistoryTarget
from app.services.group_rank_snapshot_reader import GroupSnapshotIntegrityError


def _target(market: str, through_date: date) -> GroupHistoryTarget:
    return GroupHistoryTarget(
        market=market,
        formula_version="balanced-v1",
        through_date=through_date,
    )


class _Calendar:
    def __init__(self, desired_dates):
        self.desired_dates = list(desired_dates)

    def trading_days(self, market, start, end):
        assert market in {"US", "CA"}
        assert start < end
        return self.desired_dates

    @staticmethod
    def last_completed_trading_day(_market):
        return date(2026, 6, 30)


class _Reader:
    def __init__(self, *, missing=(), invalid=()):
        self.missing = set(missing)
        self.invalid = set(invalid)

    def load_exact(self, _db, *, identity: GroupSnapshotIdentity, **_kwargs):
        if identity.as_of_date in self.invalid:
            raise GroupSnapshotIntegrityError("mixed run IDs")
        if identity.as_of_date in self.missing:
            return []
        return [
            {
                "industry_group": "Software",
                "rank": 1,
                "avg_rs_rating": 80.0,
                "num_stocks": 10,
            }
        ]

    def inspect_window(
        self,
        db,
        *,
        market,
        formula_version,
        dates,
        **kwargs,
    ):
        from app.services.group_rank_snapshot_reader import GroupSnapshotWindowResult

        snapshots = {}
        errors = {}
        for target_date in dates:
            try:
                rows = self.load_exact(
                    db,
                    identity=GroupSnapshotIdentity(
                        market,
                        target_date,
                        formula_version,
                    ),
                    **kwargs,
                )
            except GroupSnapshotIntegrityError as exc:
                errors[target_date] = str(exc)
                continue
            if rows:
                snapshots[target_date] = rows
        return GroupSnapshotWindowResult(snapshots=snapshots, errors=errors)


class _RRGProvider:
    def __init__(self, weekly_points=12):
        self.weekly_points = weekly_points
        self.calls = 0

    def get_all_groups_history(
        self,
        _db,
        *,
        market,
        days,
        as_of_date,
    ):
        self.calls += 1
        assert market == "US"
        points = [
            (as_of_date - timedelta(days=7 * offset), 50.0 + offset, 10)
            for offset in reversed(range(self.weekly_points))
        ]
        return as_of_date.isoformat(), {"Software": {"rank": 1}}, {"Software": points}


def _reference_days(current: date) -> tuple[date, ...]:
    return (
        current - timedelta(days=180),
        current - timedelta(days=90),
        current - timedelta(days=30),
        current - timedelta(days=7),
        current,
    )


def test_readiness_classifies_missing_invalid_rank_windows_and_rrg() -> None:
    from app.services.group_history_readiness_service import (
        GroupHistoryReadinessService,
    )

    current = date(2026, 6, 30)
    missing = current - timedelta(days=45)
    invalid = current - timedelta(days=60)
    desired = (*_reference_days(current), invalid, missing)
    provider = _RRGProvider(weekly_points=12)
    service = GroupHistoryReadinessService(
        calendar_service=_Calendar(desired),
        snapshot_reader=_Reader(missing=(missing,), invalid=(invalid,)),
        rrg_history_provider=provider,
    )

    report = service.evaluate(object(), target=_target("US", current))

    assert report.formula_version == "balanced-v1"
    assert report.valid_dates == _reference_days(current)
    assert report.missing_dates == (missing,)
    assert report.invalid_dates == (invalid,)
    assert report.rank_change_ready == {
        "1w": True,
        "1m": True,
        "3m": True,
        "6m": True,
    }
    assert report.rrg_required is True
    assert report.rrg_usable_weeks == 12
    assert report.rrg_plottable_series == 1
    assert report.ready is False


def test_non_rrg_group_market_can_be_ready_without_rrg_provider_call() -> None:
    from app.services.group_history_readiness_service import (
        GroupHistoryReadinessService,
    )

    current = date(2026, 6, 30)
    provider = _RRGProvider(weekly_points=0)
    service = GroupHistoryReadinessService(
        calendar_service=_Calendar(_reference_days(current)),
        snapshot_reader=_Reader(),
        rrg_history_provider=provider,
    )

    report = service.evaluate(object(), target=_target("CA", current))

    assert report.rrg_required is False
    assert report.rrg_usable_weeks == 0
    assert report.rrg_plottable_series == 0
    assert report.ready is True
    assert provider.calls == 0


def test_rrg_market_requires_twelve_provider_usable_weeks() -> None:
    from app.services.group_history_readiness_service import (
        GroupHistoryReadinessService,
    )

    current = date(2026, 6, 30)
    service = GroupHistoryReadinessService(
        calendar_service=_Calendar(_reference_days(current)),
        snapshot_reader=_Reader(),
        rrg_history_provider=_RRGProvider(weekly_points=11),
    )

    report = service.evaluate(object(), target=_target("US", current))

    assert report.rrg_usable_weeks == 11
    assert report.rrg_plottable_series == 0
    assert report.ready is False


def test_rrg_integrity_failure_is_reported_as_not_ready() -> None:
    from app.services.group_history_readiness_service import (
        GroupHistoryReadinessService,
    )

    current = date(2026, 6, 30)

    class _InvalidRRGProvider:
        @staticmethod
        def get_all_groups_history(*_args, **_kwargs):
            raise GroupSnapshotIntegrityError("mixed run IDs")

    service = GroupHistoryReadinessService(
        calendar_service=_Calendar(_reference_days(current)),
        snapshot_reader=_Reader(),
        rrg_history_provider=_InvalidRRGProvider(),
    )

    report = service.evaluate(object(), target=_target("US", current))

    assert report.rrg_usable_weeks == 0
    assert report.rrg_plottable_series == 0
    assert report.ready is False


def test_readiness_preserves_supplied_target_identity() -> None:
    from app.services.group_history_readiness_service import (
        GroupHistoryReadinessService,
    )

    current = date(2026, 6, 30)

    service = GroupHistoryReadinessService(
        calendar_service=_Calendar(_reference_days(current)),
        snapshot_reader=_Reader(),
        rrg_history_provider=_RRGProvider(weekly_points=12),
    )
    target = GroupHistoryTarget(
        market="US",
        formula_version="captured-formula-v1",
        through_date=current,
    )

    report = service.evaluate(object(), target=target)

    assert report.formula_version == "captured-formula-v1"
    assert report.market == "US"
    assert report.through_date == current
