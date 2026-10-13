"""Pre-flight for manual Runs: refuse up front when the run would only skip."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest
import pytest_asyncio

import app.tasks.daily_market_pipeline_tasks as pipeline
from app.database import get_db
from app.main import app
from app.services.task_registry_service import TaskRegistryService, TaskRunBlockedError, _run_blocker


class _Calendar:
    def __init__(self, trading=True, error=None):
        self.trading, self.error = trading, error

    def market_now(self, _market):
        if self.error:
            raise self.error
        return datetime(2026, 10, 17, 9, 0)  # a Saturday

    def is_trading_day(self, _market, _today):
        return self.trading


class _Claims:
    def __init__(self, holder=None, error=None):
        self.holder, self.error = holder, error

    def get_pipeline_chain_holder(self, _market):
        if self.error:
            raise self.error
        return self.holder


def _blocker(monkeypatch, *, calendar=None, claims=None):
    monkeypatch.setattr(pipeline, "MarketCalendarService", lambda: calendar or _Calendar())
    monkeypatch.setattr(pipeline, "_pipeline_chain_claims", lambda: claims or _Claims())
    return pipeline.pipeline_run_blocker("us")


def test_a_trading_day_with_no_pipeline_running_is_not_blocked(monkeypatch):
    assert _blocker(monkeypatch) is None


def test_a_non_trading_day_blocks_with_the_date(monkeypatch):
    blocker = _blocker(monkeypatch, calendar=_Calendar(trading=False))

    assert blocker["reason"] == "not_trading_day"
    assert blocker["as_of_date"] == "2026-10-17"
    assert "Not a trading day in US" in blocker["message"]


def test_a_held_chain_claim_blocks_with_when_it_started(monkeypatch):
    holder = {"task_id": "a" * 32, "started_at": "2026-10-16T20:30:00+00:00"}

    blocker = _blocker(monkeypatch, claims=_Claims(holder=holder))

    assert blocker["reason"] == "market_pipeline_already_active"
    assert blocker["active_since"] == "2026-10-16T20:30:00+00:00"


def test_unreadable_checks_never_block(monkeypatch):
    # The task re-checks everything itself; a pre-flight read failure must
    # not lock operators out of Run.
    assert _blocker(
        monkeypatch,
        calendar=_Calendar(error=RuntimeError("calendar down")),
        claims=_Claims(error=ConnectionError("redis down")),
    ) is None


def test_registry_entries_without_a_pre_flight_are_never_blocked():
    assert _run_blocker({"task_function": "x"}) is None


def test_a_raising_pre_flight_never_blocks():
    with patch.object(pipeline, "pipeline_run_blocker", side_effect=RuntimeError("boom")):
        assert _run_blocker({
            "run_blocker": "app.tasks.daily_market_pipeline_tasks.pipeline_run_blocker",
            "manual_dispatch_kwargs": {"market": "US"},
        }) is None


def test_a_blocked_trigger_queues_nothing_and_writes_no_run_row():
    blocker = {"reason": "not_trading_day", "message": "Not a trading day in US (2026-10-17)"}
    db = MagicMock()
    service = TaskRegistryService()
    task = MagicMock()
    service._task_imports["daily-market-pipeline-us"] = task

    with patch.object(pipeline, "pipeline_run_blocker", return_value=blocker), pytest.raises(
        TaskRunBlockedError, match="Not a trading day in US"
    ):
        service.trigger_task("daily-market-pipeline-us", db)

    task.apply_async.assert_not_called()
    db.add.assert_not_called()


def test_the_task_list_carries_each_pipelines_blocker(db_session):
    def blocker_for(market):
        return {"reason": "market_pipeline_already_active", "message": "busy"} if market == "HK" else None

    with patch.object(pipeline, "pipeline_run_blocker", side_effect=blocker_for):
        tasks = {
            t["name"]: t
            for t in TaskRegistryService().get_all_scheduled_tasks(db_session, include_run_blockers=True)
        }
        plain = TaskRegistryService().get_all_scheduled_tasks(db_session)

    assert tasks["daily-market-pipeline-hk"]["run_blocker"]["reason"] == "market_pipeline_already_active"
    assert tasks["daily-market-pipeline-us"]["run_blocker"] is None
    assert tasks["monthly-price-data-cleanup"]["run_blocker"] is None
    # Other callers (the MCP copilot) don't pay for live checks or see the key.
    assert all("run_blocker" not in t for t in plain)


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_the_run_endpoint_answers_409_with_the_reason(client, monkeypatch):
    from app.services import server_auth

    monkeypatch.setattr(server_auth.settings, "server_auth_enabled", False)
    app.dependency_overrides[get_db] = lambda: MagicMock()
    blocker = {"reason": "market_pipeline_already_active", "message": "The US pipeline is already in progress"}
    try:
        with patch.object(pipeline, "pipeline_run_blocker", return_value=blocker):
            response = await client.post("/api/v1/tasks/daily-market-pipeline-us/run")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 409
    assert response.json()["detail"] == "The US pipeline is already in progress"
