from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.domain.relative_strength import (
    BALANCED_RS_FORMULA_VERSION,
    BALANCED_RS_PRICE_BASIS,
    BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
)
from app.models.app_settings import AppSetting
from app.services.bootstrap_readiness_service import (
    PRE_BOOTSTRAP_SEED_IMPORT_KEY,
)
from app.services.group_history_bootstrap_service import GroupHistoryBootstrapStatus


def _result(*, ready: bool):
    status = (
        GroupHistoryBootstrapStatus.READY
        if ready
        else GroupHistoryBootstrapStatus.INCOMPLETE
    )
    return SimpleNamespace(
        status=status,
        after=SimpleNamespace(ready=ready),
        as_dict=lambda: {"status": status.value, "after": {"ready": ready}},
    )


def test_startup_group_history_reconciliation_marks_pristine_seed_import(
    db_session,
    monkeypatch,
) -> None:
    from app.domain.group_history import GroupHistoryTarget
    from app.tasks import group_history_tasks as module

    target = GroupHistoryTarget(
        market="US",
        formula_version=BALANCED_RS_FORMULA_VERSION,
        through_date=date(2026, 7, 31),
    )
    dispatched = []
    monkeypatch.setattr(module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(
        module,
        "_resolve_current_group_history_target",
        lambda _db, *, market: target,
    )
    monkeypatch.setattr(
        module,
        "_evaluate_group_history_readiness",
        lambda _db, *, target: SimpleNamespace(
            ready=False,
            as_dict=lambda: {"ready": False},
        ),
    )
    monkeypatch.setattr(
        module,
        "_dispatch_group_history_reconciliation",
        lambda **kwargs: dispatched.append(kwargs) or "repair-task",
    )

    result = module.discover_group_history_reconciliation()

    assert result == {"US": "queued"}
    assert dispatched == [
        {
            "market": "US",
            "formula_version": BALANCED_RS_FORMULA_VERSION,
            "through_date": date(2026, 7, 31),
            "reservation_id": dispatched[0]["reservation_id"],
        }
    ]
    setting = (
        db_session.query(AppSetting)
        .filter(AppSetting.key == PRE_BOOTSTRAP_SEED_IMPORT_KEY)
        .one()
    )
    assert "group_history_reconciliation" in setting.value


def test_startup_group_history_reconciliation_clears_owned_marker_on_dispatch_failure(
    db_session,
    monkeypatch,
) -> None:
    from app.domain.group_history import GroupHistoryTarget
    from app.tasks import group_history_tasks as module

    target = GroupHistoryTarget(
        market="US",
        formula_version=BALANCED_RS_FORMULA_VERSION,
        through_date=date(2026, 7, 31),
    )
    monkeypatch.setattr(module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(
        module,
        "_resolve_current_group_history_target",
        lambda _db, *, market: target,
    )
    monkeypatch.setattr(
        module,
        "_evaluate_group_history_readiness",
        lambda _db, *, target: SimpleNamespace(
            ready=False,
            as_dict=lambda: {"ready": False},
        ),
    )

    def _dispatch_failure(**_kwargs):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(
        module,
        "_dispatch_group_history_reconciliation",
        _dispatch_failure,
    )

    result = module.discover_group_history_reconciliation()

    assert result == {"US": "dispatch_failed"}
    setting = (
        db_session.query(AppSetting)
        .filter(AppSetting.key == PRE_BOOTSTRAP_SEED_IMPORT_KEY)
        .one_or_none()
    )
    assert setting is None


def test_ensure_group_history_invalidates_cache_and_publishes_us_snapshot(
    monkeypatch,
):
    from app.services.group_history_reconciliation import (
        GroupHistoryReservation,
        GroupHistoryTarget,
    )
    from app.tasks import group_history_tasks as module

    db = Mock()
    db.close = Mock()
    service = Mock()
    service.ensure.return_value = _result(ready=True)
    bumped = []
    monkeypatch.setattr(module, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        module, "_build_group_history_bootstrap_service", lambda: service
    )
    target = GroupHistoryTarget(
        market="US",
        formula_version="balanced-v1",
        through_date=date(2026, 6, 30),
    )
    monkeypatch.setattr(
        module,
        "_resolve_current_group_history_target",
        lambda _db, *, market: target,
    )
    repository = Mock()
    repository.reserve_finalization.return_value = GroupHistoryReservation(
        target, "fresh-lease"
    )
    repository.transition.return_value = True
    repository.owns.return_value = True
    monkeypatch.setattr(
        module,
        "GroupHistoryReconciliationRepository",
        lambda: repository,
    )
    monkeypatch.setattr(module, "bump_group_rankings_epoch", bumped.append)
    monkeypatch.setattr(
        module,
        "safe_publish_groups_bootstrap",
        lambda **_kwargs: {"snapshot_revision": "42"},
    )
    monkeypatch.setattr(module, "mark_market_activity_started", Mock())
    monkeypatch.setattr(module, "mark_market_activity_completed", Mock())
    monkeypatch.setattr(module, "mark_market_activity_failed", Mock())

    result = module.ensure_group_history.run.__wrapped__(
        module.ensure_group_history,
        market="US",
        strict=True,
    )

    assert result["status"] == "ready"
    assert result["cache_invalidated"] is True
    assert result["ui_snapshot_published"] is True
    assert bumped == ["US"]
    service.ensure.assert_called_once_with(db, target=target)
    module.mark_market_activity_completed.assert_called_once()
    db.close.assert_called_once()




def test_group_history_target_uses_balanced_publication_date(monkeypatch):
    from app.infra.db.repositories import market_rs_repo
    from app.tasks import group_history_tasks as module

    repository = Mock()
    repository.list_completed_runs.return_value = (
        SimpleNamespace(
            id=42,
            as_of_date=date(2026, 6, 29),
            diagnostics_json={
                "price_basis": BALANCED_RS_PRICE_BASIS,
                "rs_snapshot_schema_version": BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
            },
        ),
    )
    monkeypatch.setattr(
        market_rs_repo,
        "MarketRsRunRepository",
        lambda: repository,
    )
    calendar = Mock()
    calendar.last_completed_trading_day.return_value = date(2026, 6, 30)
    monkeypatch.setattr(module, "get_market_calendar_service", lambda: calendar)
    db = Mock()

    target = module._resolve_current_group_history_target(db, market="us")

    assert target.market == "US"
    assert target.formula_version == BALANCED_RS_FORMULA_VERSION
    assert target.through_date == date(2026, 6, 29)
    repository.list_completed_runs.assert_called_once_with(
        db,
        market="US",
        formula_version=BALANCED_RS_FORMULA_VERSION,
        start_date=date(2026, 6, 25),
        through_date=date(2026, 6, 30),
    )
    repository.get_latest_completed.assert_not_called()


def test_strict_group_history_task_raises_when_readiness_remains_incomplete(
    monkeypatch,
):
    from app.services.group_history_reconciliation import GroupHistoryTarget
    from app.tasks import group_history_tasks as module

    db = Mock()
    service = Mock()
    service.ensure.return_value = _result(ready=False)
    monkeypatch.setattr(module, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        module, "_build_group_history_bootstrap_service", lambda: service
    )
    monkeypatch.setattr(
        module,
        "_resolve_current_group_history_target",
        lambda _db, *, market: GroupHistoryTarget(
            market=market,
            formula_version="balanced-v1",
            through_date=date(2026, 6, 30),
        ),
    )
    monkeypatch.setattr(module, "mark_market_activity_started", Mock())
    monkeypatch.setattr(module, "mark_market_activity_completed", Mock())
    monkeypatch.setattr(module, "mark_market_activity_failed", Mock())

    with pytest.raises(RuntimeError, match="Group history remains incomplete"):
        module.ensure_group_history.run.__wrapped__(
            module.ensure_group_history,
            market="US",
            strict=True,
        )

    module.mark_market_activity_failed.assert_called_once()
    db.close.assert_called_once()


def test_strict_group_history_task_records_snapshot_publication_failure_once(
    monkeypatch,
):
    from app.services.group_history_reconciliation import (
        GroupHistoryReservation,
        GroupHistoryTarget,
    )
    from app.tasks import group_history_tasks as module

    db = Mock()
    service = Mock()
    service.ensure.return_value = _result(ready=True)
    monkeypatch.setattr(module, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        module, "_build_group_history_bootstrap_service", lambda: service
    )
    target = GroupHistoryTarget(
        market="US",
        formula_version="balanced-v1",
        through_date=date(2026, 6, 30),
    )
    monkeypatch.setattr(
        module,
        "_resolve_current_group_history_target",
        lambda _db, *, market: target,
    )
    repository = Mock()
    repository.reserve_finalization.return_value = GroupHistoryReservation(
        target, "fresh-lease"
    )
    repository.transition.return_value = True
    repository.owns.return_value = True
    monkeypatch.setattr(
        module,
        "GroupHistoryReconciliationRepository",
        lambda: repository,
    )
    monkeypatch.setattr(module, "bump_group_rankings_epoch", Mock())
    monkeypatch.setattr(
        module,
        "safe_publish_groups_bootstrap",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(module, "mark_market_activity_started", Mock())
    monkeypatch.setattr(module, "mark_market_activity_completed", Mock())
    monkeypatch.setattr(module, "mark_market_activity_failed", Mock())

    with pytest.raises(RuntimeError, match="snapshot publication failed"):
        module.ensure_group_history.run.__wrapped__(
            module.ensure_group_history,
            market="US",
            strict=True,
        )

    module.mark_market_activity_failed.assert_called_once()


def test_execution_service_finalizes_successful_us_reconciliation_once():
    from app.services.group_history_execution_service import (
        GroupHistoryExecutionService,
    )
    from app.services.group_history_reconciliation import (
        GroupHistoryReservation,
        GroupHistoryTarget,
    )

    db = Mock()
    bootstrap = Mock()
    bootstrap.ensure.return_value = _result(ready=True)
    repository = Mock()
    repository.transition.return_value = True
    repository.owns.return_value = True
    bump = Mock()
    publish = Mock(return_value={"snapshot_revision": "42"})
    completed = Mock()
    service = GroupHistoryExecutionService(
        bootstrap_service=bootstrap,
        reconciliation_repository=repository,
        bump_epoch=bump,
        publish_snapshot=publish,
        mark_started=Mock(),
        mark_completed=completed,
        mark_failed=Mock(),
    )
    target = GroupHistoryTarget(
        market="US",
        formula_version="captured-v1",
        through_date=date(2026, 6, 30),
    )

    reservation = GroupHistoryReservation(target=target, reservation_id="lease-1")
    result = service.execute_reconciliation(
        db,
        reservation=reservation,
        task_name="repair_group_history_reconciliation",
        task_id="task-1",
    )

    assert result["status"] == "ready"
    bootstrap.ensure.assert_called_once_with(db, target=target)
    bump.assert_called_once_with("US")
    publish.assert_called_once_with(target)
    claim_transition = repository.transition.call_args_list[0].kwargs
    assert {
        status.value for status in claim_transition["expected_statuses"]
    } == {"dispatching", "queued"}
    assert claim_transition["status"].value == "repairing"
    assert repository.transition.call_args.kwargs["reservation"] == reservation
    assert repository.transition.call_args.kwargs["status"].value == "ready"
    completed.assert_called_once()


def test_execution_service_revalidates_fresh_bootstrap_target_before_finalization():
    from app.services.group_history_execution_service import (
        GroupHistoryExecutionService,
    )
    from app.services.group_history_reconciliation import GroupHistoryTarget

    target = GroupHistoryTarget("US", "captured-v1", date(2026, 6, 30))
    replacement = GroupHistoryTarget("US", "captured-v1", date(2026, 7, 1))
    repository = Mock()
    bump = Mock()
    publish = Mock()
    failed = Mock()
    service = GroupHistoryExecutionService(
        bootstrap_service=Mock(ensure=Mock(return_value=_result(ready=True))),
        reconciliation_repository=repository,
        bump_epoch=bump,
        publish_snapshot=publish,
        mark_started=Mock(),
        mark_completed=Mock(),
        mark_failed=failed,
        resolve_current_target=lambda _db, *, market: replacement,
    )

    with pytest.raises(RuntimeError, match="target changed before finalization"):
        service.execute_bootstrap(
            Mock(),
            target=target,
            task_name="ensure_group_history",
            task_id="task-1",
        )

    repository.reserve_finalization.assert_not_called()
    bump.assert_not_called()
    publish.assert_not_called()
    failed.assert_called_once()


def test_execution_service_revalidates_target_after_snapshot_publication():
    from app.services.group_history_execution_service import (
        GroupHistoryExecutionService,
    )
    from app.services.group_history_reconciliation import (
        GroupHistoryReservation,
        GroupHistoryTarget,
    )

    target = GroupHistoryTarget("US", "captured-v1", date(2026, 6, 30))
    replacement = GroupHistoryTarget("US", "balanced-v1", date(2026, 6, 30))
    repository = Mock()
    repository.reserve_finalization.return_value = GroupHistoryReservation(
        target, "fresh-lease"
    )
    repository.owns.return_value = True
    repository.transition.return_value = True
    publish = Mock(return_value={"snapshot_revision": "42"})
    resolved_targets = iter((target, target, replacement))
    service = GroupHistoryExecutionService(
        bootstrap_service=Mock(ensure=Mock(return_value=_result(ready=True))),
        reconciliation_repository=repository,
        bump_epoch=Mock(),
        publish_snapshot=publish,
        mark_started=Mock(),
        mark_completed=Mock(),
        mark_failed=Mock(),
        resolve_current_target=lambda _db, *, market: next(resolved_targets),
    )

    with pytest.raises(RuntimeError, match="target changed after publication"):
        service.execute_bootstrap(
            Mock(),
            target=target,
            task_name="ensure_group_history",
            task_id="task-1",
        )

    publish.assert_called_once_with(target)
    transition_statuses = [
        call.kwargs["status"].value for call in repository.transition.call_args_list
    ]
    assert "ready" not in transition_statuses
    assert "incomplete" in transition_statuses


def test_execution_service_records_successful_fresh_bootstrap_as_ready(db_session):
    from app.services.group_history_execution_service import (
        GroupHistoryExecutionService,
    )
    from app.services.group_history_reconciliation import (
        GroupHistoryReconciliationRepository,
        GroupHistoryReconciliationStatus,
        GroupHistoryTarget,
    )

    target = GroupHistoryTarget("US", "captured-v1", date(2026, 6, 30))
    repository = GroupHistoryReconciliationRepository()
    service = GroupHistoryExecutionService(
        bootstrap_service=Mock(ensure=Mock(return_value=_result(ready=True))),
        reconciliation_repository=repository,
        bump_epoch=Mock(),
        publish_snapshot=Mock(return_value={"snapshot_revision": "42"}),
        mark_started=Mock(),
        mark_completed=Mock(),
        mark_failed=Mock(),
        resolve_current_target=lambda _db, *, market: target,
    )

    result = service.execute_bootstrap(
        db_session,
        target=target,
        task_name="ensure_group_history",
        task_id="task-1",
    )

    assert result["status"] == "ready"
    marker = repository.load(db_session, market="US")
    assert marker is not None
    assert marker.target == target
    assert marker.status is GroupHistoryReconciliationStatus.READY
    assert marker.counts == {"ready": True}


def test_fresh_bootstrap_adopts_pending_reconciliation_for_same_target(db_session):
    from app.services.group_history_execution_service import (
        GroupHistoryExecutionService,
    )
    from app.services.group_history_reconciliation import (
        GroupHistoryReconciliationRepository,
        GroupHistoryReconciliationStatus,
        GroupHistoryTarget,
    )

    target = GroupHistoryTarget("US", "captured-v1", date(2026, 6, 30))
    repository = GroupHistoryReconciliationRepository()
    pending = repository.reserve(db_session, target=target)
    assert pending is not None
    bump = Mock()
    publish = Mock(return_value={"snapshot_revision": "42"})
    service = GroupHistoryExecutionService(
        bootstrap_service=Mock(ensure=Mock(return_value=_result(ready=True))),
        reconciliation_repository=repository,
        bump_epoch=bump,
        publish_snapshot=publish,
        mark_started=Mock(),
        mark_completed=Mock(),
        mark_failed=Mock(),
        resolve_current_target=lambda _db, *, market: target,
    )

    result = service.execute_bootstrap(
        db_session,
        target=target,
        task_name="ensure_group_history",
        task_id="task-1",
    )

    assert result["status"] == "ready"
    marker = repository.load(db_session, market="US")
    assert marker is not None
    assert marker.status is GroupHistoryReconciliationStatus.READY
    bump.assert_called_once_with("US")
    publish.assert_called_once_with(target)


def test_activity_completion_failure_does_not_reclassify_success():
    from app.services.group_history_execution_service import (
        GroupHistoryExecutionService,
    )
    from app.services.group_history_reconciliation import (
        GroupHistoryReservation,
        GroupHistoryTarget,
    )

    db = Mock()
    bootstrap = Mock()
    bootstrap.ensure.return_value = _result(ready=True)
    repository = Mock()
    repository.transition.return_value = True
    repository.owns.return_value = True
    service = GroupHistoryExecutionService(
        bootstrap_service=bootstrap,
        reconciliation_repository=repository,
        bump_epoch=Mock(),
        publish_snapshot=Mock(return_value={"snapshot_revision": "42"}),
        mark_started=Mock(),
        mark_completed=Mock(side_effect=RuntimeError("telemetry unavailable")),
        mark_failed=Mock(),
    )

    target = GroupHistoryTarget(
        market="US",
        formula_version="captured-v1",
        through_date=date(2026, 6, 30),
    )
    result = service.execute_reconciliation(
        db,
        reservation=GroupHistoryReservation(target=target, reservation_id="lease-1"),
        task_name="repair_group_history_reconciliation",
        task_id="task-1",
    )

    assert result["status"] == "ready"
    assert [
        call.kwargs["status"].value for call in repository.transition.call_args_list
    ] == [
        "repairing",
        "finalizing",
        "ready",
    ]


def test_skipped_reconciliation_reaches_terminal_ready_without_finalization():
    from app.services.group_history_execution_service import (
        GroupHistoryExecutionService,
    )
    from app.services.group_history_reconciliation import (
        GroupHistoryReconciliationStatus,
        GroupHistoryReservation,
        GroupHistoryTarget,
    )

    repository = Mock()
    repository.transition.return_value = True
    repository.owns.return_value = True
    bump = Mock()
    publish = Mock()
    completed = Mock()
    bootstrap_result = SimpleNamespace(
        status=GroupHistoryBootstrapStatus.SKIPPED,
        as_dict=lambda: {"status": "skipped", "after": {"ready": False}},
    )
    service = GroupHistoryExecutionService(
        bootstrap_service=Mock(ensure=Mock(return_value=bootstrap_result)),
        reconciliation_repository=repository,
        bump_epoch=bump,
        publish_snapshot=publish,
        mark_started=Mock(),
        mark_completed=completed,
        mark_failed=Mock(),
    )
    reservation = GroupHistoryReservation(
        GroupHistoryTarget("US", "captured-v1", date(2026, 6, 30)),
        "lease-1",
    )

    result = service.execute_reconciliation(
        Mock(),
        reservation=reservation,
        task_name="repair_group_history_reconciliation",
        task_id="task-1",
    )

    assert result["status"] == "skipped"
    assert [
        call.kwargs["status"].value for call in repository.transition.call_args_list
    ] == ["repairing", "ready"]
    assert repository.transition.call_args.kwargs["expected_statuses"] == {
        GroupHistoryReconciliationStatus.REPAIRING
    }
    bump.assert_not_called()
    publish.assert_not_called()
    completed.assert_called_once()


def test_superseded_reconciliation_does_not_repair_or_publish():
    from app.services.group_history_execution_service import (
        GroupHistoryExecutionService,
    )
    from app.services.group_history_reconciliation import (
        GroupHistoryReservation,
        GroupHistoryTarget,
    )

    repository = Mock()
    repository.transition.return_value = False
    bootstrap = Mock()
    bump = Mock()
    publish = Mock()
    service = GroupHistoryExecutionService(
        bootstrap_service=bootstrap,
        reconciliation_repository=repository,
        bump_epoch=bump,
        publish_snapshot=publish,
        mark_started=Mock(),
        mark_completed=Mock(),
        mark_failed=Mock(),
    )
    reservation = GroupHistoryReservation(
        target=GroupHistoryTarget(
            market="US",
            formula_version="old-v1",
            through_date=date(2026, 6, 30),
        ),
        reservation_id="stale-lease",
    )

    result = service.execute_reconciliation(
        Mock(),
        reservation=reservation,
        task_name="repair_group_history_reconciliation",
        task_id="task-old",
    )

    assert result["status"] == "superseded"
    bootstrap.ensure.assert_not_called()
    bump.assert_not_called()
    publish.assert_not_called()


def test_target_drift_before_finalization_suppresses_cache_and_publication():
    from app.services.group_history_execution_service import (
        GroupHistoryExecutionService,
    )
    from app.services.group_history_reconciliation import (
        GroupHistoryReservation,
        GroupHistoryTarget,
    )

    target = GroupHistoryTarget("US", "captured-v1", date(2026, 6, 30))
    replacement = GroupHistoryTarget("US", "captured-v2", date(2026, 6, 30))
    repository = Mock()
    repository.transition.return_value = True
    repository.owns.return_value = True
    current_targets = iter((target, replacement))
    bump = Mock()
    publish = Mock()
    service = GroupHistoryExecutionService(
        bootstrap_service=Mock(ensure=Mock(return_value=_result(ready=True))),
        reconciliation_repository=repository,
        bump_epoch=bump,
        publish_snapshot=publish,
        mark_started=Mock(),
        mark_completed=Mock(),
        mark_failed=Mock(),
        resolve_current_target=lambda _db, *, market: next(current_targets),
    )

    result = service.execute_reconciliation(
        Mock(),
        reservation=GroupHistoryReservation(target, "lease-1"),
        task_name="repair_group_history_reconciliation",
        task_id="task-1",
    )

    assert result["status"] == "superseded"
    bump.assert_not_called()
    publish.assert_not_called()
    assert repository.transition.call_args.kwargs["status"].value == "incomplete"
