"""Operations job actions are audited intent-first (#613).

Each action commits a ``requested`` row before it changes Redis, the broker or
a scan, and no action runs if that commit fails. The outcome row follows; if it
cannot be written, the action's real result is still returned, and the intent
row already shows the attempt.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models.app_settings import AppSetting
from app.services.operations_job_service import (
    OPERATIONS_JOB_ACTION_CATEGORY,
    OperationsJobService,
    _JobRecord,
)


def _record(strategy: str, **overrides) -> _JobRecord:
    fields = dict(
        task_id="task-1",
        task_name="app.tasks.cache_tasks.smart_refresh_cache",
        queue="data_fetch_us",
        market="US",
        state="reserved",
        worker="datafetch@host",
        age_seconds=None,
        wait_reason=None,
        heartbeat_lag_seconds=None,
        cancel_strategy=strategy,
    )
    fields.update(overrides)
    return _JobRecord(**fields)


class _Events:
    """One ordered log of audit writes and side effects."""

    def __init__(self, *, fail_outcomes=(), fail_intent=False):
        self.log: list[str] = []
        self.fail_outcomes = set(fail_outcomes)
        self.fail_intent = fail_intent

    def audit(self, _db, *, task_id, strategy, outcome, message):
        if outcome == "requested" and self.fail_intent:
            raise ConnectionError("database unavailable")
        if outcome in self.fail_outcomes:
            raise ConnectionError("database unavailable")
        self.log.append(f"audit:{outcome}")

    def effect(self, name):
        def record(*_args, **_kwargs):
            self.log.append(name)
            return True

        return record


def _service(events: _Events, record: _JobRecord) -> OperationsJobService:
    service = OperationsJobService()
    service._record_cancel_action = events.audit
    service._find_job_record = lambda _db, _task_id: record
    return service


# ── Intent before the action ─────────────────────────────────────────────


def test_revoke_commits_its_intent_before_revoking():
    events = _Events()
    service = _service(events, _record("revoke"))

    with patch(
        "app.services.operations_job_service.celery_app.control.revoke",
        side_effect=events.effect("revoke"),
    ):
        result = service.cancel_job(MagicMock(), "task-1", expected_strategy="revoke")

    assert result["status"] == "accepted"
    assert events.log == ["audit:requested", "revoke", "audit:accepted"]


def test_queue_removal_commits_its_intent_before_touching_the_broker():
    events = _Events()
    service = _service(events, _record("revoke_and_remove_from_queue", state="queued"))

    def remove(*, task_id):
        events.log.append("remove")
        return True, "data_fetch_us"

    service._remove_queued_task = remove
    with patch("app.services.operations_job_service.record_task_run"):
        result = service.cancel_job(MagicMock(), "task-1")

    assert result["status"] == "accepted"
    assert events.log == ["audit:requested", "remove", "audit:accepted"]


def test_market_lease_release_commits_its_intent_first():
    events = _Events()
    service = _service(events, _record("force_release_market_lease", state="stuck"))

    with patch("app.services.operations_job_service.get_workload_coordination") as coordination:
        coordination.return_value.release_market_workload.side_effect = events.effect("release")
        result = service.cancel_job(MagicMock(), "task-1")

    assert result["status"] == "accepted"
    assert events.log == ["audit:requested", "release", "audit:accepted"]


def test_chain_claim_release_commits_its_intent_first():
    events = _Events()
    service = OperationsJobService()
    service._record_cancel_action = events.audit

    with patch("app.services.operations_job_service.get_workload_coordination") as coordination:
        coordination.return_value.release_pipeline_chain.side_effect = events.effect("release")
        result = service.release_pipeline_chain(MagicMock(), "US", "tok-1")

    assert result["status"] == "accepted"
    assert events.log == ["audit:requested", "release", "audit:accepted"]


# ── No action without an intent row ──────────────────────────────────────


def test_no_revoke_when_the_intent_cannot_be_recorded(caplog):
    events = _Events(fail_intent=True)
    service = _service(events, _record("revoke"))
    db = MagicMock()

    with patch("app.services.operations_job_service.celery_app.control.revoke") as revoke:
        result = service.cancel_job(db, "task-1", expected_strategy="revoke")

    revoke.assert_not_called()
    db.rollback.assert_called()
    assert result["status"] == "blocked"
    assert "nothing was done" in result["message"]
    assert "Could not record" in caplog.text


def test_no_chain_release_when_the_intent_cannot_be_recorded():
    events = _Events(fail_intent=True)
    service = OperationsJobService()
    service._record_cancel_action = events.audit

    with patch("app.services.operations_job_service.get_workload_coordination") as coordination:
        result = service.release_pipeline_chain(MagicMock(), "US", "tok-1")

    coordination.return_value.release_pipeline_chain.assert_not_called()
    assert result["status"] == "blocked"


# ── An outcome write failure keeps the real result ───────────────────────


def test_a_failed_outcome_write_still_returns_the_actions_result(caplog):
    events = _Events(fail_outcomes={"accepted"})
    service = _service(events, _record("revoke"))
    db = MagicMock()

    with patch(
        "app.services.operations_job_service.celery_app.control.revoke",
        side_effect=events.effect("revoke"),
    ):
        result = service.cancel_job(db, "task-1", expected_strategy="revoke")

    # The revoke happened: report it, not an error.
    assert result["status"] == "accepted"
    assert events.log == ["audit:requested", "revoke"]
    db.rollback.assert_called()
    assert "Could not record the outcome" in caplog.text


def test_a_failed_outcome_write_after_a_chain_release_still_reports_it():
    events = _Events(fail_outcomes={"accepted"})
    service = OperationsJobService()
    service._record_cancel_action = events.audit

    with patch("app.services.operations_job_service.get_workload_coordination") as coordination:
        coordination.return_value.release_pipeline_chain.return_value = True
        result = service.release_pipeline_chain(MagicMock(), "US", "tok-1")

    assert result["status"] == "accepted"


# ── The audit trail itself ───────────────────────────────────────────────


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[AppSetting.__table__])
    with Session(engine) as session:
        yield session


def test_intent_and_outcome_rows_never_collide_on_one_clock_tick(db):
    # A coarse clock (Windows ticks ~15 ms) gives both rows one timestamp;
    # the key is unique, so the outcome row would be lost.
    service = OperationsJobService()
    tick = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)
    with patch("app.services.operations_job_service._utcnow", return_value=tick):
        for outcome in ("requested", "accepted"):
            service._record_cancel_action(
                db, task_id="task-1", strategy="revoke", outcome=outcome, message="m"
            )

    outcomes = [
        json.loads(row.value)["outcome"]
        for row in db.query(AppSetting).filter(
            AppSetting.category == OPERATIONS_JOB_ACTION_CATEGORY
        )
    ]
    assert sorted(outcomes) == ["accepted", "requested"]


def _outcomes(db) -> list[str]:
    return [
        json.loads(row.value)["outcome"]
        for row in db.query(AppSetting)
        .filter(AppSetting.category == OPERATIONS_JOB_ACTION_CATEGORY)
        .order_by(AppSetting.id)
    ]


def test_an_action_that_raises_is_recorded_as_an_error_and_still_raises(db):
    # A queue removal can drop the message and then fail to revoke: the log
    # must say the action broke, not leave a bare request.
    service = OperationsJobService()

    def act():
        raise ConnectionError("broker went away mid-action")

    with pytest.raises(ConnectionError):
        service._audited(db, task_id="task-1", strategy="revoke", act=act)

    assert _outcomes(db) == ["requested", "error"]
    row = json.loads(db.query(AppSetting).order_by(AppSetting.id.desc()).first().value)
    assert "broker went away mid-action" in row["message"]


def test_scan_cancel_commits_its_intent_before_cancelling(db):
    events = _Events()
    service = _service(
        events,
        _record("scan_cancel", task_name="app.tasks.scan_tasks.run_bulk_scan",
                kwargs={"scan_id": "scan-1"}, state="running"),
    )

    def cancel(_db, scan_id):
        events.log.append(f"cancel:{scan_id}")
        return "accepted", "cancelled"

    service._cancel_scan = cancel
    result = service.cancel_job(db, "task-1")

    assert result["status"] == "accepted"
    assert events.log == ["audit:requested", "cancel:scan-1", "audit:accepted"]


def test_a_real_failed_intent_commit_leaves_the_session_usable(db):
    # The rollback after a failed intent must leave the request's session
    # working: nothing acts, and the next action records normally.
    service = OperationsJobService()
    real_record = service._record_cancel_action
    fail = {"next": True}

    def flaky(session, **kwargs):
        if fail.pop("next", False):
            session.add(AppSetting(key="dup", value="{}", category="x"))
            session.add(AppSetting(key="dup", value="{}", category="x"))
            session.commit()  # IntegrityError, like a real failed commit
        return real_record(session, **kwargs)

    service._record_cancel_action = flaky
    acted = []

    first = service._audited(db, task_id="task-1", strategy="revoke",
                             act=lambda: acted.append(1) or ("accepted", "m"))
    second = service._audited(db, task_id="task-1", strategy="revoke",
                              act=lambda: acted.append(2) or ("accepted", "m"))

    assert (first["status"], second["status"]) == ("blocked", "accepted")
    assert acted == [2]
    assert _outcomes(db) == ["requested", "accepted"]


def test_an_intent_row_alone_does_not_read_cancel_requested(db):
    service = OperationsJobService()
    service._record_cancel_action(
        db, task_id="task-1", strategy="revoke", outcome="requested", message="m"
    )

    assert service._cancel_requests(db) == {}

    service._record_cancel_action(
        db, task_id="task-1", strategy="revoke", outcome="accepted", message="m"
    )
    assert service._cancel_requests(db)["task-1"]["outcome"] == "accepted"
