"""Scheduled-task run history is recorded by the worker, not by browser polling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from celery.signals import task_postrun, task_prerun, task_revoked

import app.celery_app  # noqa: F401  (connects the signal handlers)
from app.models.task_execution import TaskExecutionHistory
from app.schemas.task import TaskStatusResponse
from app.services.task_registry_service import TaskRegistryService

PIPELINE = "app.tasks.daily_market_pipeline_tasks.queue_daily_market_pipeline"


class _Task:
    """Stands in for a Celery task as a signal sender (senders must be hashable)."""

    def __init__(self, name: str) -> None:
        self.name = name


_task = _Task


def _rows(db, task_id):
    db.expire_all()
    return db.query(TaskExecutionHistory).filter_by(task_id=task_id).all()


def test_scheduled_run_is_recorded_start_to_finish(db_session):
    task_id = str(uuid4())
    sender = _task(PIPELINE)

    task_prerun.send(sender=sender, task_id=task_id, task=sender, args=(), kwargs={"market": "HK"})
    [row] = _rows(db_session, task_id)
    assert (row.task_name, row.status, row.triggered_by) == ("daily-market-pipeline-hk", "running", "schedule")

    task_postrun.send(
        sender=sender, task_id=task_id, task=sender, args=(), kwargs={"market": "HK"},
        retval={"status": "queued"}, state="SUCCESS",
    )
    [row] = _rows(db_session, task_id)
    assert row.status == "completed"
    assert row.completed_at is not None
    assert row.duration_seconds is not None
    assert row.result_summary == {"status": "queued"}


def test_failed_run_records_error_message(db_session):
    task_id = str(uuid4())
    sender = _task("app.tasks.cache_tasks.cleanup_orphaned_scans")

    task_prerun.send(sender=sender, task_id=task_id, task=sender, args=(), kwargs={})
    task_postrun.send(
        sender=sender, task_id=task_id, task=sender, args=(), kwargs={},
        retval=RuntimeError("boom"), state="FAILURE",
    )

    [row] = _rows(db_session, task_id)
    assert (row.task_name, row.status, row.error_message) == ("weekly-orphaned-scan-cleanup", "failed", "boom")


def test_revoked_run_is_cancelled_not_failed(db_session):
    task_id = str(uuid4())
    sender = _task("app.tasks.cache_tasks.cleanup_orphaned_scans")

    task_revoked.send(
        sender=sender, request=SimpleNamespace(id=task_id, kwargs={}),
        terminated=False, signum=None, expired=False,
    )

    [row] = _rows(db_session, task_id)
    assert row.status == "cancelled"


def test_tasks_outside_the_registry_are_not_recorded(db_session):
    task_id = str(uuid4())
    sender = _task("app.tasks.theme_intelligence_tasks.refresh_groups")

    task_prerun.send(sender=sender, task_id=task_id, task=sender, args=(), kwargs={})

    assert _rows(db_session, task_id) == []


def test_recording_failure_never_breaks_the_task(monkeypatch):
    from app.services import task_registry_service

    def broken_session():
        raise RuntimeError("db down")

    monkeypatch.setattr(task_registry_service, "SessionLocal", broken_session)
    sender = _task(PIPELINE)

    # Must not raise: bookkeeping is best-effort and only logs a warning.
    task_prerun.send(sender=sender, task_id="x", task=sender, args=(), kwargs={"market": "US"})


def test_manual_trigger_writes_its_row_before_dispatch_and_worker_updates_it(db_session):
    seen_at_dispatch = []

    def apply_async(**kwargs):
        # A fast worker may start the task before trigger_task returns, so
        # the row must already be committed when the message is published.
        seen_at_dispatch.extend(_rows(db_session, kwargs["task_id"]))
        return SimpleNamespace(id=kwargs["task_id"])

    service = TaskRegistryService()
    service._task_imports["weekly-orphaned-scan-cleanup"] = SimpleNamespace(apply_async=apply_async)
    result = service.trigger_task("weekly-orphaned-scan-cleanup", db_session)
    task_id = result["task_id"]

    assert [r.status for r in seen_at_dispatch] == ["queued"]

    sender = _task("app.tasks.cache_tasks.cleanup_orphaned_scans")
    task_prerun.send(sender=sender, task_id=task_id, task=sender, args=(), kwargs={})
    task_postrun.send(sender=sender, task_id=task_id, task=sender, args=(), kwargs={}, retval=None, state="SUCCESS")

    [row] = _rows(db_session, task_id)
    assert (row.status, row.triggered_by) == ("completed", "manual")


def test_manual_trigger_marks_row_failed_when_dispatch_fails(db_session):
    def apply_async(**_kwargs):
        raise RuntimeError("broker unavailable")

    service = TaskRegistryService()
    service._task_imports["weekly-orphaned-scan-cleanup"] = SimpleNamespace(apply_async=apply_async)
    try:
        service.trigger_task("weekly-orphaned-scan-cleanup", db_session)
    except RuntimeError:
        pass

    db_session.expire_all()
    row = (
        db_session.query(TaskExecutionHistory)
        .filter_by(task_name="weekly-orphaned-scan-cleanup")
        .order_by(TaskExecutionHistory.id.desc())
        .first()
    )
    assert row.status == "failed"
    assert "broker unavailable" in row.error_message


def test_run_older_than_celery_hard_time_limit_reads_as_lost(db_session):
    db_session.add(TaskExecutionHistory(
        task_name="monthly-price-data-cleanup",
        task_function="app.tasks.cache_tasks.cleanup_old_price_data",
        task_id=str(uuid4()),
        status="running",
        started_at=datetime.now(timezone.utc) - timedelta(hours=25),
        triggered_by="schedule",
    ))
    db_session.commit()

    tasks = TaskRegistryService().get_all_scheduled_tasks(db_session)
    task = next(t for t in tasks if t["name"] == "monthly-price-data-cleanup")

    assert task["last_run"]["status"] == "lost"


def test_status_endpoint_reads_finished_runs_from_the_ledger(db_session):
    task_id = str(uuid4())
    sender = _task("app.tasks.cache_tasks.cleanup_orphaned_scans")
    task_revoked.send(
        sender=sender, request=SimpleNamespace(id=task_id, kwargs={}),
        terminated=True, signum=15, expired=False,
    )

    with patch("app.services.task_registry_service.AsyncResult") as async_result:
        status = TaskRegistryService().get_task_status("weekly-orphaned-scan-cleanup", task_id, db_session)

    assert status["status"] == "cancelled"
    async_result.assert_not_called()
    # The API serializes through this schema; a missing field would be a 500.
    assert TaskStatusResponse(**status).celery_state == "REVOKED"


def test_operations_queue_removal_marks_the_run_cancelled(db_session):
    from app.services.operations_job_service import OperationsJobService, _JobRecord
    from tests.unit.test_operations_job_service import _FakeBroker, _queued_message

    service = TaskRegistryService()
    service._task_imports["weekly-orphaned-scan-cleanup"] = SimpleNamespace(
        apply_async=lambda **kwargs: SimpleNamespace(id=kwargs["task_id"])
    )
    task_id = service.trigger_task("weekly-orphaned-scan-cleanup", db_session)["task_id"]

    ops = OperationsJobService()
    raw = _queued_message(task_id=task_id, task_name="app.tasks.cache_tasks.cleanup_orphaned_scans")
    broker = _FakeBroker({"celery": [raw]})
    ops._broker = lambda: broker
    ops._record_cancel_action = lambda *args, **kwargs: None
    ops._find_scan_record = lambda _db, _task_id: _JobRecord(
        task_id=task_id,
        task_name="app.tasks.cache_tasks.cleanup_orphaned_scans",
        queue="celery", market=None, state="queued", worker=None, age_seconds=None,
        wait_reason=None, heartbeat_lag_seconds=None,
        cancel_strategy="revoke_and_remove_from_queue", kwargs={},
    )
    with patch.object(ops, "broker_queue_names", return_value=["celery"]), patch(
        "app.services.operations_job_service.celery_app.control.revoke"
    ):
        assert ops.cancel_job(MagicMock(), task_id)["status"] == "accepted"

    [row] = _rows(db_session, task_id)
    assert row.status == "cancelled"
