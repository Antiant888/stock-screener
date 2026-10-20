from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

from celery.exceptions import SoftTimeLimitExceeded
import pytest
from sqlalchemy.orm import sessionmaker
from app.domain.relative_strength import BALANCED_RS_FORMULA_VERSION


def _target():
    from app.services.group_history_reconciliation import GroupHistoryTarget

    return GroupHistoryTarget(
        market="US",
        through_date=date(2026, 6, 30),
    )


def test_reconciliation_marker_reservation_is_idempotent_and_resumable(db_session):
    from app.services.group_history_reconciliation import (
        GroupHistoryReconciliationRepository,
        GroupHistoryReconciliationStatus,
    )

    repository = GroupHistoryReconciliationRepository()
    target = _target()

    reservation = repository.reserve(db_session, target=target)
    assert reservation is not None
    assert repository.reserve(db_session, target=target) is None

    assert repository.transition(
        db_session,
        reservation=reservation,
        expected_statuses={GroupHistoryReconciliationStatus.DISPATCHING},
        status=GroupHistoryReconciliationStatus.QUEUED,
    )
    assert (
        repository.transition(
            db_session,
            reservation=reservation,
            expected_statuses={GroupHistoryReconciliationStatus.QUEUED},
            status=GroupHistoryReconciliationStatus.INCOMPLETE,
            error="worker interrupted",
        )
        is True
    )

    assert repository.reserve(db_session, target=target) is not None
    marker = repository.load(db_session, market="US")
    assert marker.status is GroupHistoryReconciliationStatus.DISPATCHING
    assert marker.error is None


def test_fresh_finalization_adopts_same_target_queued_reservation(db_session):
    from app.services.group_history_reconciliation import (
        GroupHistoryReconciliationRepository,
        GroupHistoryReconciliationStatus,
    )

    repository = GroupHistoryReconciliationRepository()
    target = _target()
    queued = repository.reserve(db_session, target=target)
    assert queued is not None
    assert repository.transition(
        db_session,
        reservation=queued,
        expected_statuses={GroupHistoryReconciliationStatus.DISPATCHING},
        status=GroupHistoryReconciliationStatus.QUEUED,
    )

    finalization = repository.reserve_finalization(db_session, target=target)

    assert finalization is not None
    assert finalization.target == target
    marker = repository.load(db_session, market="US")
    assert marker is not None
    assert marker.status is GroupHistoryReconciliationStatus.FINALIZING
    assert marker.reservation_id == finalization.reservation_id
    assert marker.reservation_id != queued.reservation_id
    assert (
        repository.transition(
            db_session,
            reservation=queued,
            expected_statuses={GroupHistoryReconciliationStatus.QUEUED},
            status=GroupHistoryReconciliationStatus.REPAIRING,
        )
        is False
    )


def test_existing_marker_compare_and_swap_allows_only_one_stale_reservation(
    db_session,
):
    from app.models.app_settings import AppSetting
    from app.services.group_history_reconciliation import (
        GroupHistoryReconciliationRepository,
        GroupHistoryReconciliationStatus,
        GroupHistoryTarget,
    )

    repository = GroupHistoryReconciliationRepository()
    target = GroupHistoryTarget(
        market="US",
        through_date=date(2026, 6, 30),
    )
    reservation = repository.reserve(db_session, target=target)
    assert reservation is not None
    assert repository.transition(
        db_session,
        reservation=reservation,
        expected_statuses={GroupHistoryReconciliationStatus.DISPATCHING},
        status=GroupHistoryReconciliationStatus.INCOMPLETE,
    )
    setting = (
        db_session.query(AppSetting)
        .filter(AppSetting.key == repository.key("US"))
        .one()
    )
    observed_value = setting.value
    observed_marker = repository.load(db_session, market="US")

    class _StaleObservationRepository(GroupHistoryReconciliationRepository):
        def load(self, _db, *, market):
            assert market == "US"
            return observed_marker

        def _load_record(self, _db, *, market):
            assert market == "US"
            return observed_marker, observed_value

    Session = sessionmaker(bind=db_session.get_bind())
    first = Session()
    second = Session()
    try:
        contenders = _StaleObservationRepository()
        assert contenders.reserve(first, target=target) is not None
        assert contenders.reserve(second, target=target) is None
    finally:
        first.close()
        second.close()


def test_stale_dispatching_marker_can_be_reserved_after_interrupted_dispatch(
    db_session,
):
    from app.models.app_settings import AppSetting
    from app.services.group_history_reconciliation import (
        GroupHistoryReconciliationRepository,
    )

    repository = GroupHistoryReconciliationRepository()
    target = _target()
    assert repository.reserve(db_session, target=target)
    marker = repository.load(db_session, market="US")
    assert marker is not None
    assert marker.status.value == "dispatching"
    setting = (
        db_session.query(AppSetting)
        .filter(AppSetting.key == repository.key("US"))
        .one()
    )
    payload = json.loads(setting.value)
    payload["updated_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=7)
    ).isoformat()
    setting.value = json.dumps(payload)
    db_session.commit()

    assert repository.reserve(db_session, target=target) is not None


def test_queued_marker_remains_owned_during_extended_broker_backlog(db_session):
    from app.models.app_settings import AppSetting
    from app.services.group_history_reconciliation import (
        GroupHistoryReconciliationRepository,
    )

    repository = GroupHistoryReconciliationRepository()
    target = _target()
    reservation = repository.reserve(db_session, target=target)
    assert reservation is not None
    setting = (
        db_session.query(AppSetting)
        .filter(AppSetting.key == repository.key("US"))
        .one()
    )
    payload = json.loads(setting.value)
    payload["status"] = "queued"
    payload["updated_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=7)
    ).isoformat()
    setting.value = json.dumps(payload)
    db_session.commit()

    assert repository.reserve(db_session, target=target) is None
    marker = repository.load(db_session, market="US")
    assert marker is not None
    assert marker.reservation_id == reservation.reservation_id
    assert marker.status.value == "queued"


def test_abandoned_queued_marker_can_be_reserved_after_bounded_lease(db_session):
    from app.models.app_settings import AppSetting
    from app.services.group_history_reconciliation import (
        GroupHistoryReconciliationRepository,
    )

    repository = GroupHistoryReconciliationRepository()
    target = _target()
    reservation = repository.reserve(db_session, target=target)
    assert reservation is not None
    setting = (
        db_session.query(AppSetting)
        .filter(AppSetting.key == repository.key("US"))
        .one()
    )
    payload = json.loads(setting.value)
    payload["status"] = "queued"
    payload["updated_at"] = (
        datetime.now(timezone.utc) - timedelta(days=2)
    ).isoformat()
    setting.value = json.dumps(payload)
    db_session.commit()

    replacement = repository.reserve(db_session, target=target)

    assert replacement is not None
    assert replacement.reservation_id != reservation.reservation_id
    marker = repository.load(db_session, market="US")
    assert marker is not None
    assert marker.reservation_id == replacement.reservation_id
    assert marker.status.value == "dispatching"


def test_stale_reservation_cannot_overwrite_a_new_target(db_session):
    from app.services.group_history_reconciliation import (
        GroupHistoryReconciliationRepository,
        GroupHistoryReconciliationStatus,
        GroupHistoryTarget,
    )

    repository = GroupHistoryReconciliationRepository()
    stale = repository.reserve(db_session, target=_target())
    assert stale is not None
    current_target = GroupHistoryTarget(
        market="US",
        through_date=date(2026, 7, 1),
    )
    current = repository.reserve(db_session, target=current_target)
    assert current is not None

    assert (
        repository.transition(
            db_session,
            reservation=stale,
            expected_statuses={GroupHistoryReconciliationStatus.DISPATCHING},
            status=GroupHistoryReconciliationStatus.FAILED,
            error="late errback",
        )
        is False
    )
    marker = repository.load(db_session, market="US")
    assert marker is not None
    assert marker.target == current_target
    assert marker.reservation_id == current.reservation_id
    assert marker.status is GroupHistoryReconciliationStatus.DISPATCHING


def test_finalizing_reservation_cannot_be_superseded(db_session):
    from app.services.group_history_reconciliation import (
        GroupHistoryReconciliationRepository,
        GroupHistoryReconciliationStatus,
        GroupHistoryTarget,
    )

    repository = GroupHistoryReconciliationRepository()
    reservation = repository.reserve(db_session, target=_target())
    assert reservation is not None
    assert repository.transition(
        db_session,
        reservation=reservation,
        expected_statuses={GroupHistoryReconciliationStatus.DISPATCHING},
        status=GroupHistoryReconciliationStatus.REPAIRING,
    )
    assert repository.transition(
        db_session,
        reservation=reservation,
        expected_statuses={GroupHistoryReconciliationStatus.REPAIRING},
        status=GroupHistoryReconciliationStatus.FINALIZING,
    )

    replacement = repository.reserve(
        db_session,
        target=GroupHistoryTarget("US", date(2026, 7, 1)),
    )

    assert replacement is None
    marker = repository.load(db_session, market="US")
    assert marker is not None
    assert marker.reservation_id == reservation.reservation_id
    assert marker.status is GroupHistoryReconciliationStatus.FINALIZING


def test_non_object_marker_json_is_replaced_by_a_new_reservation(db_session):
    from app.models.app_settings import AppSetting
    from app.services.group_history_reconciliation import (
        GroupHistoryReconciliationRepository,
    )

    repository = GroupHistoryReconciliationRepository()
    db_session.add(
        AppSetting(
            key=repository.key("US"),
            value="null",
            category="group_history_reconciliation",
        )
    )
    db_session.commit()

    reservation = repository.reserve(db_session, target=_target())

    assert reservation is not None
    marker = repository.load(db_session, market="US")
    assert marker is not None
    assert marker.reservation_id == reservation.reservation_id


def _readiness(*, ready: bool, formula: str = "balanced-v1"):
    return SimpleNamespace(
        ready=ready,
        supported=True,
        formula_version=formula,
        as_dict=lambda: {
            "ready": ready,
            "formula_version": formula,
            "missing_dates": [] if ready else ["2026-05-01"],
        },
    )


def test_celery_discovery_queues_incomplete_enabled_group_market(monkeypatch):
    from app.tasks import group_history_tasks as module
    from app.services.group_history_reconciliation import (
        GroupHistoryReservation,
        GroupHistoryTarget,
    )

    db = Mock()
    db.close = Mock()
    repository = Mock()
    reservation = GroupHistoryReservation(_target(), "lease-1")
    repository.reserve.return_value = reservation
    dispatched = []
    monkeypatch.setattr(module, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        module,
        "get_runtime_preferences",
        lambda _db: SimpleNamespace(
            enabled_markets=["US", "SG"],
            bootstrap_state="ready",
        ),
    )
    monkeypatch.setattr(
        module,
        "_resolve_current_group_history_target",
        lambda _db, *, market: GroupHistoryTarget(
            market=market,
            through_date=date(2026, 6, 30),
        ),
    )
    monkeypatch.setattr(
        module,
        "_evaluate_group_history_readiness",
        lambda _db, *, target: _readiness(
            ready=False,
            formula=BALANCED_RS_FORMULA_VERSION,
        ),
    )
    monkeypatch.setattr(
        module,
        "get_market_calendar_service",
        lambda: SimpleNamespace(
            last_completed_trading_day=lambda _market: date(2026, 6, 30)
        ),
    )
    monkeypatch.setattr(
        module,
        "GroupHistoryReconciliationRepository",
        lambda: repository,
    )
    monkeypatch.setattr(
        module,
        "_dispatch_group_history_reconciliation",
        lambda **kwargs: dispatched.append(kwargs) or "repair-us-1",
    )

    result = module.discover_group_history_reconciliation.run()

    assert result == {"US": "queued", "SG": "skipped"}
    assert dispatched == [
        {
            "market": "US",
            "formula_version": BALANCED_RS_FORMULA_VERSION,
            "through_date": date(2026, 6, 30),
            "reservation_id": "lease-1",
        }
    ]
    queued_transition = repository.transition.call_args.kwargs
    assert {
        status.value for status in queued_transition["expected_statuses"]
    } == {"dispatching"}
    assert queued_transition["status"].value == "queued"
    db.close.assert_called_once()


def test_startup_reconciliation_skips_while_runtime_bootstrap_is_running(
    monkeypatch,
):
    from app.tasks import group_history_tasks as module

    db = Mock()
    monkeypatch.setattr(module, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        module,
        "get_runtime_preferences",
        lambda _db: SimpleNamespace(
            enabled_markets=["US"],
            bootstrap_state="running",
        ),
    )
    dispatch = Mock()
    monkeypatch.setattr(module, "_dispatch_group_history_reconciliation", dispatch)

    assert module.discover_group_history_reconciliation.run() == {
        "US": "bootstrap_running"
    }
    dispatch.assert_not_called()


def test_startup_reconciliation_database_ready_queues_finalization(monkeypatch):
    from app.tasks import group_history_tasks as module
    from app.services.group_history_reconciliation import (
        GroupHistoryReservation,
        GroupHistoryReconciliationStatus,
        GroupHistoryTarget,
    )

    db = Mock()
    repository = Mock()
    repository.load.return_value = None
    repository.reserve.return_value = GroupHistoryReservation(_target(), "lease-1")
    monkeypatch.setattr(module, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        module,
        "get_runtime_preferences",
        lambda _db: SimpleNamespace(
            enabled_markets=["US"],
            bootstrap_state="ready",
        ),
    )
    monkeypatch.setattr(
        module,
        "get_market_calendar_service",
        lambda: SimpleNamespace(
            last_completed_trading_day=lambda _market: date(2026, 6, 30)
        ),
    )
    monkeypatch.setattr(
        module,
        "_resolve_current_group_history_target",
        lambda _db, *, market: GroupHistoryTarget(
            market=market,
            through_date=date(2026, 6, 30),
        ),
    )
    monkeypatch.setattr(
        module,
        "_evaluate_group_history_readiness",
        lambda _db, *, target: _readiness(
            ready=True,
            formula=BALANCED_RS_FORMULA_VERSION,
        ),
    )
    monkeypatch.setattr(
        module,
        "GroupHistoryReconciliationRepository",
        lambda: repository,
    )
    repair_dispatch = Mock()
    finalization_dispatch = Mock(return_value="finalize-us-1")
    monkeypatch.setattr(
        module,
        "_dispatch_group_history_reconciliation",
        repair_dispatch,
    )
    monkeypatch.setattr(
        module,
        "_dispatch_group_history_finalization",
        finalization_dispatch,
    )

    assert module.discover_group_history_reconciliation.run() == {
        "US": "finalization_queued"
    }
    repair_dispatch.assert_not_called()
    finalization_dispatch.assert_called_once_with(
        market="US",
        formula_version=BALANCED_RS_FORMULA_VERSION,
        through_date=date(2026, 6, 30),
        reservation_id="lease-1",
    )
    queued_transition = repository.transition.call_args.kwargs
    assert queued_transition["reservation"].reservation_id == "lease-1"
    assert queued_transition["expected_statuses"] == {
        GroupHistoryReconciliationStatus.DISPATCHING
    }
    assert queued_transition["status"] is GroupHistoryReconciliationStatus.QUEUED


def test_startup_reconciliation_ready_marker_is_verified_noop(monkeypatch):
    from app.tasks import group_history_tasks as module
    from app.services.group_history_reconciliation import (
        GroupHistoryReconciliationStatus,
        GroupHistoryTarget,
    )

    db = Mock()
    target = GroupHistoryTarget(
        market="US",
        through_date=date(2026, 6, 30),
    )
    repository = Mock()
    repository.load.return_value = SimpleNamespace(
        target=target,
        status=GroupHistoryReconciliationStatus.READY,
    )
    monkeypatch.setattr(module, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        module,
        "get_runtime_preferences",
        lambda _db: SimpleNamespace(
            enabled_markets=["US"],
            bootstrap_state="ready",
        ),
    )
    monkeypatch.setattr(
        module,
        "_resolve_current_group_history_target",
        lambda _db, *, market: target,
    )
    evaluate = Mock(return_value=_readiness(ready=True, formula="balanced-v1"))
    monkeypatch.setattr(module, "_evaluate_group_history_readiness", evaluate)
    monkeypatch.setattr(
        module,
        "GroupHistoryReconciliationRepository",
        lambda: repository,
    )
    repair_dispatch = Mock()
    finalization_dispatch = Mock()
    monkeypatch.setattr(
        module,
        "_dispatch_group_history_reconciliation",
        repair_dispatch,
    )
    monkeypatch.setattr(
        module,
        "_dispatch_group_history_finalization",
        finalization_dispatch,
    )

    assert module.discover_group_history_reconciliation.run() == {"US": "ready"}
    evaluate.assert_called_once_with(db, target=target)
    repository.reserve.assert_not_called()
    repair_dispatch.assert_not_called()
    finalization_dispatch.assert_not_called()


def test_startup_reconciliation_reserve_failure_isolated_per_market(monkeypatch):
    from app.tasks import group_history_tasks as module
    from app.services.group_history_reconciliation import GroupHistoryTarget

    db = Mock()
    repository = Mock()
    repository.load.return_value = None
    repository.reserve.side_effect = [RuntimeError("database unavailable"), None]
    monkeypatch.setattr(module, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        module,
        "get_runtime_preferences",
        lambda _db: SimpleNamespace(
            enabled_markets=["US", "HK"],
            bootstrap_state="ready",
        ),
    )
    monkeypatch.setattr(
        module,
        "_resolve_current_group_history_target",
        lambda _db, *, market: GroupHistoryTarget(
            market=market,
            through_date=date(2026, 6, 30),
        ),
    )
    monkeypatch.setattr(
        module,
        "GroupHistoryReconciliationRepository",
        lambda: repository,
    )

    assert module.discover_group_history_reconciliation.run() == {
        "US": "reserve_failed:RuntimeError",
        "HK": "already_queued",
    }
    db.rollback.assert_called_once()
    db.close.assert_called_once()


def test_startup_reconciliation_reraises_soft_timeout(monkeypatch):
    from app.tasks import group_history_tasks as module
    from app.services.group_history_reconciliation import GroupHistoryTarget

    db = Mock()
    repository = Mock()
    repository.load.return_value = None
    repository.reserve.side_effect = SoftTimeLimitExceeded()
    monkeypatch.setattr(module, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        module,
        "get_runtime_preferences",
        lambda _db: SimpleNamespace(
            enabled_markets=["US"],
            bootstrap_state="ready",
        ),
    )
    monkeypatch.setattr(
        module,
        "_resolve_current_group_history_target",
        lambda _db, *, market: GroupHistoryTarget(
            market=market,
            through_date=date(2026, 6, 30),
        ),
    )
    monkeypatch.setattr(
        module,
        "GroupHistoryReconciliationRepository",
        lambda: repository,
    )

    with pytest.raises(SoftTimeLimitExceeded):
        module.discover_group_history_reconciliation.run()

    db.rollback.assert_called_once()
    db.close.assert_called_once()


def test_dispatch_failure_returns_marker_to_incomplete(monkeypatch):
    from app.tasks import group_history_tasks as module
    from app.services.group_history_reconciliation import (
        GroupHistoryReservation,
        GroupHistoryTarget,
    )

    db = Mock()
    repository = Mock()
    repository.reserve.return_value = GroupHistoryReservation(_target(), "lease-1")
    monkeypatch.setattr(module, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        module,
        "get_runtime_preferences",
        lambda _db: SimpleNamespace(
            enabled_markets=["US"],
            bootstrap_state="ready",
        ),
    )
    monkeypatch.setattr(
        module,
        "get_market_calendar_service",
        lambda: SimpleNamespace(
            last_completed_trading_day=lambda _market: date(2026, 6, 30)
        ),
    )
    monkeypatch.setattr(
        module,
        "_resolve_current_group_history_target",
        lambda _db, *, market: GroupHistoryTarget(
            market=market,
            through_date=date(2026, 6, 30),
        ),
    )
    monkeypatch.setattr(
        module,
        "_evaluate_group_history_readiness",
        lambda _db, *, target: _readiness(
            ready=False,
            formula=BALANCED_RS_FORMULA_VERSION,
        ),
    )
    monkeypatch.setattr(
        module,
        "GroupHistoryReconciliationRepository",
        lambda: repository,
    )
    monkeypatch.setattr(
        module,
        "_dispatch_group_history_reconciliation",
        Mock(side_effect=RuntimeError("broker unavailable")),
    )

    assert module.discover_group_history_reconciliation.run() == {
        "US": "dispatch_failed"
    }
    assert {
        status.value
        for status in repository.transition.call_args.kwargs["expected_statuses"]
    } == {"dispatching"}
    assert repository.transition.call_args.kwargs["status"].value == "incomplete"
    assert repository.transition.call_args.kwargs["error"] == "broker unavailable"


def test_reconciliation_dispatches_price_repair_and_verification_to_expected_queues(
    monkeypatch,
):
    from app.tasks import group_history_tasks as module
    from app.tasks.market_queues import (
        data_fetch_queue_for_market,
        market_jobs_queue_for_market,
    )

    captured = {}

    class _Workflow:
        def apply_async(self, **kwargs):
            captured["apply_kwargs"] = kwargs
            return SimpleNamespace(id="repair-us-1")

    def _chain(*signatures):
        captured["signatures"] = signatures
        return _Workflow()

    monkeypatch.setattr(module, "chain", _chain)

    task_id = module._dispatch_group_history_reconciliation(
        market="US",
        formula_version="balanced-v1",
        through_date=date(2026, 6, 30),
        reservation_id="lease-1",
    )

    assert task_id == "repair-us-1"
    signatures = captured["signatures"]
    assert [signature.task for signature in signatures] == [
        "app.tasks.cache_tasks.smart_refresh_cache",
        "app.tasks.group_history_tasks.repair_group_history_reconciliation",
    ]
    assert [signature.options["queue"] for signature in signatures] == [
        data_fetch_queue_for_market("US"),
        market_jobs_queue_for_market("US"),
    ]
    assert signatures[0].kwargs["ensure_group_history"] is True
    assert signatures[1].kwargs == {
        "market": "US",
        "formula_version": "balanced-v1",
        "through_date": "2026-06-30",
        "reservation_id": "lease-1",
    }
    errback = captured["apply_kwargs"]["link_error"]
    assert errback.task == (
        "app.tasks.group_history_tasks.fail_group_history_reconciliation"
    )
    assert errback.options["queue"] == "celery"
    assert errback.kwargs["reservation_id"] == "lease-1"


def test_ready_reconciliation_dispatches_only_fenced_finalization(monkeypatch):
    from app.tasks import group_history_tasks as module

    repair_signature = Mock()
    repair_signature.apply_async.return_value = SimpleNamespace(id="finalize-us-1")
    failure_signature = Mock()
    repair_builder = Mock(return_value=repair_signature)
    failure_builder = Mock(return_value=failure_signature)
    monkeypatch.setattr(module, "_group_history_repair_signature", repair_builder)
    monkeypatch.setattr(module, "_group_history_failure_signature", failure_builder)

    task_id = module._dispatch_group_history_finalization(
        market="US",
        formula_version="balanced-v1",
        through_date=date(2026, 6, 30),
        reservation_id="lease-1",
    )

    assert task_id == "finalize-us-1"
    repair_builder.assert_called_once_with(
        market="US",
        formula_version="balanced-v1",
        through_date=date(2026, 6, 30),
        reservation_id="lease-1",
    )
    repair_signature.apply_async.assert_called_once_with(link_error=failure_signature)


def test_reconciliation_repair_executes_the_captured_target(monkeypatch):
    from app.services.group_history_reconciliation import GroupHistoryTarget
    from app.tasks import group_history_tasks as module

    db = Mock()
    executor = Mock()
    executor.execute_reconciliation.return_value = {"status": "ready"}
    monkeypatch.setattr(module, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        module, "_build_group_history_execution_service", lambda: executor
    )

    result = module.repair_group_history_reconciliation.run.__wrapped__(
        module.repair_group_history_reconciliation,
        market="us",
        formula_version="captured-v1",
        through_date="2026-06-30",
        reservation_id="lease-1",
    )

    assert result == {"status": "ready"}
    reservation = executor.execute_reconciliation.call_args.kwargs["reservation"]
    assert reservation.target == GroupHistoryTarget(
        market="US",
        through_date=date(2026, 6, 30),
    )
    assert reservation.reservation_id == "lease-1"
    db.close.assert_called_once_with()
