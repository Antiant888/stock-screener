"""Scheduled-task run history is recorded by the worker, not by browser polling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from celery.exceptions import TimeLimitExceeded
from celery.signals import task_postrun, task_prerun

import app.celery_app  # noqa: F401  (connects the signal handlers)
from app.models.task_execution import TaskExecutionHistory
from app.schemas.task import TaskStatusResponse
from app.services.task_registry_service import TaskRegistryService, record_task_run

PIPELINE = "app.tasks.daily_market_pipeline_tasks.queue_daily_market_pipeline"
CLEANUP = "app.tasks.cache_tasks.cleanup_orphaned_scans"
SOCIAL = "app.interfaces.tasks.social_signal_tasks.refresh_social_signals"


class _Task:
    """Stands in for a Celery task as a signal sender (senders must be hashable)."""

    def __init__(self, name: str) -> None:
        self.name = name


def _rows(db, task_id):
    db.expire_all()
    return db.query(TaskExecutionHistory).filter_by(task_id=task_id).all()


def _start(name, task_id, kwargs):
    sender = _Task(name)
    task_prerun.send(sender=sender, task_id=task_id, task=sender, args=(), kwargs=kwargs)


def _finish(name, task_id, kwargs, *, state, retval=None):
    sender = _Task(name)
    task_postrun.send(
        sender=sender, task_id=task_id, task=sender, args=(), kwargs=kwargs, retval=retval, state=state,
    )


def _last_run(db, name):
    tasks = TaskRegistryService().get_all_scheduled_tasks(db)
    return next(t for t in tasks if t["name"] == name)["last_run"]


def test_worker_run_is_recorded_start_to_finish(db_session):
    task_id = str(uuid4())

    _start(PIPELINE, task_id, {"market": "HK"})
    [row] = _rows(db_session, task_id)
    assert (row.task_name, row.status, row.triggered_by) == ("daily-market-pipeline-hk", "running", "auto")

    _finish(PIPELINE, task_id, {"market": "HK"}, state="SUCCESS", retval={"status": "queued"})
    [row] = _rows(db_session, task_id)
    assert row.status == "completed"
    assert row.completed_at is not None
    assert row.duration_seconds is not None
    assert row.result_summary == {"status": "queued"}


def test_failed_run_records_error_message(db_session):
    task_id = str(uuid4())

    _start(CLEANUP, task_id, {})
    _finish(CLEANUP, task_id, {}, state="FAILURE", retval=RuntimeError("boom"))

    [row] = _rows(db_session, task_id)
    assert (row.task_name, row.status, row.error_message) == ("weekly-orphaned-scan-cleanup", "failed", "boom")


def test_retry_does_not_rewind_a_running_row(db_session):
    task_id = str(uuid4())

    _start(CLEANUP, task_id, {})
    _finish(CLEANUP, task_id, {}, state="RETRY")

    [row] = _rows(db_session, task_id)
    assert row.status == "running"


def test_unserializable_result_still_records_the_outcome(db_session):
    task_id = str(uuid4())

    _start(CLEANUP, task_id, {})
    _finish(CLEANUP, task_id, {}, state="SUCCESS", retval={"ratio": float("nan")})

    [row] = _rows(db_session, task_id)
    assert row.status == "completed"
    assert row.result_summary is None


def test_tasks_outside_the_registry_are_not_recorded(db_session):
    task_id = str(uuid4())

    _start("app.tasks.theme_intelligence_tasks.refresh_groups", task_id, {})

    assert _rows(db_session, task_id) == []


def test_ad_hoc_social_refreshes_are_not_recorded_but_scheduled_ones_are(db_session):
    # prepare_live_attachments can dispatch the same task every minute; those
    # runs are not the six-hourly scheduled job and would flood the history.
    ad_hoc, scheduled = str(uuid4()), str(uuid4())

    _start(SOCIAL, ad_hoc, {"origin": "attachment-evidence"})
    _start(SOCIAL, scheduled, {"origin": "scheduled"})

    assert _rows(db_session, ad_hoc) == []
    [row] = _rows(db_session, scheduled)
    assert (row.task_name, row.triggered_by) == ("social-signal-refresh", "scheduled")


def test_recording_failure_is_logged_not_raised(monkeypatch, caplog):
    from app.services import task_registry_service

    def broken_session():
        raise RuntimeError("db down")

    monkeypatch.setattr(task_registry_service, "SessionLocal", broken_session)

    # Called directly (Celery's Signal.send would swallow errors on its own):
    # the Operations cancel path relies on this never raising.
    record_task_run("x", CLEANUP, {}, "cancelled")

    assert "Failed to record" in caplog.text


@pytest.mark.parametrize(
    ("celery_state", "result", "expected_status", "expected_error"),
    [
        ("FAILURE", TimeLimitExceeded(2100), "failed", "TimeLimitExceeded(2100,)"),
        ("REVOKED", None, "cancelled", None),
    ],
)
def test_open_run_is_closed_from_celery_result_when_no_signal_fired(
    db_session, celery_state, result, expected_status, expected_error,
):
    # A hard time limit or a lost worker kills the child before task_postrun,
    # and revokes are announced in the parent; Celery still stores the outcome.
    task_id = str(uuid4())
    _start("app.tasks.cache_tasks.weekly_full_refresh", task_id, {"market": "US"})

    with patch("app.services.task_registry_service.AsyncResult") as async_result:
        async_result.return_value = SimpleNamespace(state=celery_state, result=result)
        last_run = _last_run(db_session, "weekly-full-refresh")

    assert last_run["status"] == expected_status
    [row] = _rows(db_session, task_id)
    assert row.status == expected_status
    assert row.error_message == expected_error


def test_open_run_stays_open_while_celery_reports_it_running(db_session):
    task_id = str(uuid4())
    _start("app.tasks.cache_tasks.weekly_full_refresh", task_id, {"market": "US"})

    with patch("app.services.task_registry_service.AsyncResult") as async_result:
        async_result.return_value = SimpleNamespace(state="STARTED", result=None)
        assert _last_run(db_session, "weekly-full-refresh")["status"] == "running"


def test_manual_trigger_writes_its_row_before_dispatch_and_worker_updates_it(db_session):
    seen_at_dispatch = []

    def apply_async(**kwargs):
        # A fast worker may start the task before trigger_task returns, so
        # the row must already be committed when the message is published.
        seen_at_dispatch.extend(_rows(db_session, kwargs["task_id"]))
        return SimpleNamespace(id=kwargs["task_id"])

    service = TaskRegistryService()
    service._task_imports["weekly-orphaned-scan-cleanup"] = SimpleNamespace(apply_async=apply_async)
    task_id = service.trigger_task("weekly-orphaned-scan-cleanup", db_session)["task_id"]

    assert [r.status for r in seen_at_dispatch] == ["queued"]

    _start(CLEANUP, task_id, {})
    _finish(CLEANUP, task_id, {}, state="SUCCESS")

    [row] = _rows(db_session, task_id)
    assert (row.status, row.triggered_by) == ("completed", "manual")


def _manual_dispatch_that_raises(db_session, *, worker_started: bool):
    def apply_async(**kwargs):
        if worker_started:
            # The publish landed and a worker picked it up before the
            # producer saw an error (e.g. a lost confirm during retry).
            _start(CLEANUP, kwargs["task_id"], {})
        raise RuntimeError("broker unavailable")

    service = TaskRegistryService()
    service._task_imports["weekly-orphaned-scan-cleanup"] = SimpleNamespace(apply_async=apply_async)
    with pytest.raises(RuntimeError, match="broker unavailable"):
        service.trigger_task("weekly-orphaned-scan-cleanup", db_session)

    db_session.expire_all()
    return db_session.query(TaskExecutionHistory).filter_by(task_name="weekly-orphaned-scan-cleanup").one()


def test_manual_trigger_marks_row_failed_when_dispatch_fails(db_session):
    row = _manual_dispatch_that_raises(db_session, worker_started=False)

    assert row.status == "failed"
    assert "broker unavailable" in row.error_message


def test_dispatch_error_does_not_overwrite_a_run_the_worker_already_started(db_session):
    row = _manual_dispatch_that_raises(db_session, worker_started=True)

    assert row.status == "running"


def test_run_open_longer_than_celery_hard_time_limit_reads_as_lost(db_session):
    db_session.add(TaskExecutionHistory(
        task_name="monthly-price-data-cleanup",
        task_function="app.tasks.cache_tasks.cleanup_old_price_data",
        task_id=str(uuid4()),
        status="running",
        started_at=datetime.now(timezone.utc) - timedelta(hours=25),
        triggered_by="auto",
    ))
    db_session.commit()

    with patch("app.services.task_registry_service.AsyncResult") as async_result:
        assert _last_run(db_session, "monthly-price-data-cleanup")["status"] == "lost"
    # Past Celery's result expiry there is nothing left to ask.
    async_result.assert_not_called()


def test_status_endpoint_reads_finished_runs_from_the_ledger(db_session):
    task_id = str(uuid4())
    _start(CLEANUP, task_id, {})
    record_task_run(task_id, CLEANUP, {}, "cancelled")

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
    raw = _queued_message(task_id=task_id, task_name=CLEANUP)
    broker = _FakeBroker({"celery": [raw]})
    ops._broker = lambda: broker
    ops._record_cancel_action = lambda *args, **kwargs: None
    ops._find_scan_record = lambda _db, _task_id: _JobRecord(
        task_id=task_id,
        task_name=CLEANUP,
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
