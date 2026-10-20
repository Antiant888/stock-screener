from __future__ import annotations

from datetime import date

from celery.exceptions import SoftTimeLimitExceeded
import pytest

from app.services.group_history_readiness_service import GroupHistoryReadinessReport
from app.services.group_history_reconciliation import GroupHistoryTarget
from app.domain.relative_strength import BALANCED_RS_FORMULA_VERSION


def _target(market="US", formula_version="balanced-v1"):
    return GroupHistoryTarget(
        market=market,
        through_date=date(2026, 6, 30),
    )


def _report(
    *,
    ready: bool,
    missing=(),
    invalid=(),
    supported: bool = True,
    formula_version: str = "balanced-v1",
) -> GroupHistoryReadinessReport:
    through = date(2026, 6, 30)
    valid = tuple(
        day
        for day in (date(2026, 1, 2), date(2026, 6, 30))
        if day not in set(missing) | set(invalid)
    )
    return GroupHistoryReadinessReport(
        market="US",
        through_date=through,
        formula_version=formula_version if supported else None,
        supported=supported,
        desired_dates=tuple(sorted((*valid, *missing, *invalid))),
        valid_dates=valid,
        missing_dates=tuple(missing),
        invalid_dates=tuple(invalid),
        rank_change_ready={"1w": ready, "1m": ready, "3m": ready, "6m": ready},
        ready=ready,
        reason=None if supported else "group_rankings_not_supported",
    )


class _Readiness:
    def __init__(self, *reports):
        self.reports = list(reports)

    def evaluate(self, _db, *, target):
        assert target.market in {"US", "SG"}
        assert target.through_date == date(2026, 6, 30)
        return self.reports.pop(0)


class _Coordinator:
    def __init__(self, fail_date=None):
        self.ensure_calls = []
        self.repair_calls = []
        self.fail_date = fail_date

    def ensure_snapshot(self, _db, *, identity):
        self.ensure_calls.append(identity)
        if identity.as_of_date == self.fail_date:
            raise RuntimeError("price anchor unavailable")

    def repair_snapshot(self, _db, *, identity):
        self.repair_calls.append(identity)
        if identity.as_of_date == self.fail_date:
            raise RuntimeError("repair failed")


class _Policies:
    @staticmethod
    def policy_for(_market, day):
        return "point_in_time" if day.day == 1 else "current_active_fallback_v1"


class _DB:
    def __init__(self):
        self.rollbacks = 0
        self.commits = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _CommitFailsOnceDB(_DB):
    def commit(self):
        super().commit()
        if self.commits == 1:
            raise RuntimeError("commit failed")


def test_group_history_bootstrap_processes_only_missing_and_invalid_oldest_first():
    from app.services.group_history_bootstrap_service import (
        GroupHistoryBootstrapService,
        GroupHistoryBootstrapStatus,
    )

    missing = date(2026, 5, 2)
    invalid = date(2026, 4, 1)
    coordinator = _Coordinator()
    service = GroupHistoryBootstrapService(
        readiness_service=_Readiness(
            _report(ready=False, missing=(missing,), invalid=(invalid,)),
            _report(ready=True),
        ),
        snapshot_coordinator=coordinator,
        universe_resolver=_Policies(),
    )

    db = _DB()
    result = service.ensure(db, target=_target())

    assert result.status is GroupHistoryBootstrapStatus.READY
    assert [item.as_of_date for item in coordinator.repair_calls] == [invalid]
    assert [item.as_of_date for item in coordinator.ensure_calls] == [missing]
    assert result.processed_dates == (invalid, missing)
    assert result.failed_dates == ()
    assert result.skipped_valid == 2
    assert result.policy_counts == {
        "point_in_time": 1,
        "current_active_fallback_v1": 1,
    }
    assert result.after.ready is True
    assert db.commits == 2


def test_group_history_bootstrap_rolls_back_failed_date_and_trusts_after_report():
    from app.services.group_history_bootstrap_service import (
        GroupHistoryBootstrapService,
        GroupHistoryBootstrapStatus,
    )

    failed = date(2026, 5, 2)
    db = _DB()
    service = GroupHistoryBootstrapService(
        readiness_service=_Readiness(
            _report(ready=False, missing=(failed,)),
            _report(ready=False, missing=(failed,)),
        ),
        snapshot_coordinator=_Coordinator(fail_date=failed),
        universe_resolver=_Policies(),
    )

    result = service.ensure(db, target=_target())

    assert result.status is GroupHistoryBootstrapStatus.INCOMPLETE
    assert result.failed_dates == (failed,)
    assert result.errors == ((failed, "price anchor unavailable"),)
    assert db.rollbacks == 1
    assert result.after.ready is False


def test_group_history_bootstrap_continues_after_per_date_commit_failure():
    from app.services.group_history_bootstrap_service import (
        GroupHistoryBootstrapService,
    )

    first = date(2026, 4, 1)
    second = date(2026, 5, 2)
    db = _CommitFailsOnceDB()
    service = GroupHistoryBootstrapService(
        readiness_service=_Readiness(
            _report(ready=False, missing=(first, second)),
            _report(ready=False, missing=(first,)),
        ),
        snapshot_coordinator=_Coordinator(),
        universe_resolver=_Policies(),
    )

    result = service.ensure(db, target=_target())

    assert result.processed_dates == (second,)
    assert result.failed_dates == (first,)
    assert result.errors == ((first, "commit failed"),)
    assert db.commits == 2
    assert db.rollbacks == 1


def test_group_history_bootstrap_reraises_soft_time_limit():
    from app.services.group_history_bootstrap_service import (
        GroupHistoryBootstrapService,
    )

    target_date = date(2026, 5, 2)
    coordinator = _Coordinator()
    coordinator.ensure_snapshot = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        SoftTimeLimitExceeded()
    )
    db = _DB()
    service = GroupHistoryBootstrapService(
        readiness_service=_Readiness(
            _report(ready=False, missing=(target_date,)),
        ),
        snapshot_coordinator=coordinator,
        universe_resolver=_Policies(),
    )

    with pytest.raises(SoftTimeLimitExceeded):
        service.ensure(db, target=_target())

    assert db.rollbacks == 1


def test_group_history_bootstrap_skips_unsupported_market():
    from app.services.group_history_bootstrap_service import (
        GroupHistoryBootstrapService,
        GroupHistoryBootstrapStatus,
    )

    coordinator = _Coordinator()
    result = GroupHistoryBootstrapService(
        readiness_service=_Readiness(_report(ready=True, supported=False)),
        snapshot_coordinator=coordinator,
        universe_resolver=_Policies(),
    ).ensure(_DB(), target=_target("SG"))

    assert result.status is GroupHistoryBootstrapStatus.SKIPPED
    assert coordinator.ensure_calls == []
    assert coordinator.repair_calls == []


def test_group_history_bootstrap_preserves_supplied_target_identity():
    from app.services.group_history_bootstrap_service import (
        GroupHistoryBootstrapService,
    )

    target = GroupHistoryTarget(
        market="US",
        through_date=date(2026, 6, 30),
    )

    class _TargetReadiness:
        def __init__(self):
            self.targets = []

        def evaluate(self, _db, *, target):
            self.targets.append(target)
            return _report(ready=True)

    readiness = _TargetReadiness()
    result = GroupHistoryBootstrapService(
        readiness_service=readiness,
        snapshot_coordinator=_Coordinator(),
        universe_resolver=_Policies(),
    ).ensure(_DB(), target=target)

    assert readiness.targets == [target]
    assert result.market == target.market
    assert result.formula_version == BALANCED_RS_FORMULA_VERSION
    assert result.through_date == target.through_date
