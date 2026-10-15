"""What the Operations page tells someone before they press Run."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from app.models.task_execution import TaskExecutionHistory
from app.services.operations_job_service import _JobRecord
from app.services.task_registry_service import SCHEDULED_TASKS, TaskRegistryService


def _run(db, name, status, duration, *, days_ago=1.0):
    db.add(TaskExecutionHistory(
        task_name=name,
        task_id=str(uuid4()),
        status=status,
        started_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        duration_seconds=duration,
        triggered_by="auto",
    ))


def _tasks(db, **kwargs):
    return {task["name"]: task for task in TaskRegistryService().get_all_scheduled_tasks(db, **kwargs)}


def test_every_job_says_when_to_run_it_and_what_it_affects():
    missing = [
        name for name, info in SCHEDULED_TASKS.items()
        if not info.get("when_to_run") or not info.get("impact")
    ]
    assert missing == []


def test_typical_duration_is_the_median_of_the_last_ten_completed_runs(db_session):
    name = "weekly-fundamental-refresh-hk"
    # Newest ten, one per day: 100..1000s, median 550s.
    for day, duration in enumerate(range(100, 1001, 100), start=1):
        _run(db_session, name, "completed", duration, days_ago=day)
    # An eleventh, older run past the newest ten does not count.
    _run(db_session, name, "completed", 99_999, days_ago=20)
    # Skipped and failed runs end early and would understate the work.
    _run(db_session, name, "skipped", 0.1, days_ago=0.5)
    _run(db_session, name, "failed", 3.0, days_ago=0.5)
    _run(db_session, "weekly-orphaned-scan-cleanup", "completed", 40, days_ago=200)
    db_session.commit()

    tasks = _tasks(db_session)

    assert tasks[name]["typical_duration_seconds"] == 550
    # Only runs inside the window count.
    assert tasks["weekly-orphaned-scan-cleanup"]["typical_duration_seconds"] is None
    assert tasks["weekly-fundamental-refresh-us"]["typical_duration_seconds"] is None


class _Calendar:
    def __init__(self, states):
        self.states = states

    def is_market_open(self, market):
        state = self.states.get(market, False)
        if isinstance(state, Exception):
            raise state
        return state


def test_price_jobs_name_the_markets_mid_session(db_session):
    states = {"HK": True, "JP": RuntimeError("no verified calendar coverage")}
    with patch(
        "app.tasks.daily_market_pipeline_tasks._preflight_calendar",
        return_value=_Calendar(states),
    ):
        tasks = _tasks(db_session, include_run_blockers=True)

    assert tasks["daily-market-pipeline-hk"]["open_markets"] == ["HK"]
    assert tasks["weekly-full-refresh-hk"]["open_markets"] == ["HK"]
    assert tasks["daily-market-pipeline-us"]["open_markets"] == []
    # Unknown, not "closed": the calendar could not say.
    assert tasks["daily-market-pipeline-jp"]["open_markets"] is None
    # The all-markets refresh fetches every market's prices.
    assert tasks["weekly-full-refresh"]["open_markets"] == ["HK"]
    # Jobs that fetch no prices store no partial bar.
    assert tasks["weekly-fundamental-refresh-hk"]["open_markets"] is None


def test_open_markets_is_a_live_check_the_plain_list_skips(db_session):
    with patch("app.tasks.daily_market_pipeline_tasks._preflight_calendar") as calendar:
        tasks = _tasks(db_session)

    calendar.assert_not_called()
    assert "open_markets" not in tasks["daily-market-pipeline-hk"]


def _record(task_name, kwargs):
    return _JobRecord(
        task_id="t1", task_name=task_name, queue=None, market=None, state="running",
        worker=None, age_seconds=None, wait_reason=None, heartbeat_lag_seconds=None,
        cancel_strategy="revoke", kwargs=kwargs,
    )


def test_job_console_names_scheduled_jobs_like_the_schedule_does():
    pipeline = "app.tasks.daily_market_pipeline_tasks.queue_daily_market_pipeline"
    refresh = "app.tasks.cache_tasks.weekly_full_refresh"

    assert _record(pipeline, {"market": "hk"}).to_api()["display_name"] == "Daily Market Pipeline (HK)"
    assert _record(refresh, {}).to_api()["display_name"] == "Weekly Full Refresh (all markets)"
    assert _record("app.tasks.scan_tasks.run_bulk_scan", {"market": "US"}).to_api()["display_name"] is None
