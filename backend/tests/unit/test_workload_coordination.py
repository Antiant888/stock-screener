"""Unit tests for background-task lease coordination."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import Retry
from sqlalchemy.exc import OperationalError


def _postgres_recovery_error() -> OperationalError:
    return OperationalError(
        "select 1",
        {},
        Exception(
            "FATAL:  the database system is not yet accepting connections\n"
            "DETAIL:  Consistent recovery state has not been yet reached."
        ),
    )


def _make_coordination():
    with patch("app.tasks.workload_coordination.settings") as mock_settings:
        mock_settings.redis_host = "localhost"
        mock_settings.redis_port = 6379
        mock_settings.redis_db = 0
        mock_settings.data_fetch_lock_timeout = 7200

        with patch("app.tasks.workload_coordination.redis.Redis") as mock_redis_cls:
            mock_redis = MagicMock()
            mock_redis_cls.return_value = mock_redis
            mock_release_script = MagicMock()
            mock_redis.register_script.return_value = mock_release_script

            from app.tasks.workload_coordination import WorkloadCoordination

            coordination = WorkloadCoordination()

    return coordination, mock_redis, mock_release_script


def test_market_workload_lease_uses_market_scoped_key():
    coordination, mock_redis, _ = _make_coordination()
    mock_redis.get.return_value = None
    mock_redis.set.return_value = True

    acquired, is_reentrant = coordination.acquire_market_workload(
        "run_bulk_scan",
        "task-123",
        market="US",
    )

    assert (acquired, is_reentrant) == (True, False)
    args, kwargs = mock_redis.set.call_args
    assert args[0] == "market_workload:us"
    assert "run_bulk_scan:task-123:" in args[1]
    assert kwargs == {"nx": True, "ex": 7200}


def test_external_fetch_lease_uses_global_key():
    coordination, mock_redis, _ = _make_coordination()
    mock_redis.get.return_value = None
    mock_redis.set.return_value = True

    acquired, is_reentrant = coordination.acquire_external_fetch(
        "smart_refresh_cache",
        "task-123",
    )

    assert (acquired, is_reentrant) == (True, False)
    args, kwargs = mock_redis.set.call_args
    assert args[0] == "external_fetch_global"
    assert "smart_refresh_cache:task-123:" in args[1]
    assert kwargs == {"nx": True, "ex": 7200}


def test_disable_serialized_market_workload_bypasses_coordination():
    from app.tasks.workload_coordination import (
        disable_serialized_market_workload,
        serialized_market_workload,
    )

    task = SimpleNamespace(request=SimpleNamespace(id="task-123"))

    @serialized_market_workload("build_daily_snapshot")
    def my_func(self, market=None):
        return {"status": "ok", "market": market}

    with patch("app.wiring.bootstrap.get_workload_coordination") as mock_get_coordination:
        with disable_serialized_market_workload():
            result = my_func(task, market="HK")

    assert result == {"status": "ok", "market": "HK"}
    mock_get_coordination.assert_not_called()


@patch("app.wiring.bootstrap.get_workload_coordination")
def test_serialized_market_workload_retries_with_coordination_retry_budget(
    mock_get_coordination,
):
    from app.tasks.workload_coordination import serialized_market_workload

    mock_coordination = MagicMock()
    mock_coordination.acquire_market_workload.return_value = (False, False)
    mock_coordination.get_market_workload_holder.return_value = {
        "task_name": "calculate_daily_group_rankings_with_gapfill",
        "task_id": "other-task",
    }
    mock_get_coordination.return_value = mock_coordination

    retry_calls = []

    def _retry(*, exc=None, countdown=None, max_retries=None):
        retry_calls.append(
            {
                "exc": exc,
                "countdown": countdown,
                "max_retries": max_retries,
            }
        )
        raise Retry(message=str(exc))

    task = SimpleNamespace(
        request=SimpleNamespace(id="task-123", retries=1),
        retry=_retry,
    )
    body_called = False

    @serialized_market_workload("build_daily_snapshot")
    def my_func(self, market=None):
        nonlocal body_called
        body_called = True
        return {"status": "ok"}

    with pytest.raises(Retry):
        my_func(task, market="US")

    assert body_called is False
    assert retry_calls[0]["countdown"] == 30
    assert retry_calls[0]["max_retries"] == 10_000
    assert "waiting_for_market_workload:US" in str(retry_calls[0]["exc"])


@patch("app.wiring.bootstrap.get_workload_coordination")
def test_serialized_market_workload_retries_transient_postgres_recovery(
    mock_get_coordination,
):
    from app.tasks.workload_coordination import serialized_market_workload

    mock_coordination = MagicMock()
    mock_coordination.acquire_market_workload.return_value = (True, False)
    mock_get_coordination.return_value = mock_coordination

    retry_calls = []

    def _retry(*, exc=None, countdown=None, max_retries=None):
        retry_calls.append(
            {
                "exc": exc,
                "countdown": countdown,
                "max_retries": max_retries,
            }
        )
        raise Retry(message=str(exc))

    task = SimpleNamespace(
        request=SimpleNamespace(id="task-123", retries=0),
        retry=_retry,
    )

    @serialized_market_workload("build_daily_snapshot")
    def my_func(self, market=None):
        raise _postgres_recovery_error()

    with pytest.raises(Retry):
        my_func(task, market="TW")

    assert retry_calls[0]["countdown"] == 5
    assert retry_calls[0]["max_retries"] == 12
    assert "database system is not yet accepting connections" in str(
        retry_calls[0]["exc"]
    )
    mock_coordination.release_market_workload.assert_called_once_with(
        "task-123",
        market="TW",
    )


@patch("app.wiring.bootstrap.get_workload_coordination")
@patch("app.wiring.bootstrap.get_data_fetch_lock")
def test_serialized_data_fetch_retries_when_external_fetch_lease_is_busy(
    mock_get_lock,
    mock_get_coordination,
):
    from app.tasks.data_fetch_lock import _serialized_data_fetch

    mock_lock = MagicMock()
    mock_lock.acquire.return_value = (True, False)
    mock_get_lock.return_value = mock_lock

    mock_coordination = MagicMock()
    mock_coordination.acquire_market_workload.return_value = (True, False)
    mock_coordination.acquire_external_fetch.return_value = (False, False)
    mock_coordination.get_external_fetch_holder.return_value = {
        "task_name": "refresh_all_fundamentals",
        "task_id": "other-task",
    }
    mock_get_coordination.return_value = mock_coordination

    def _retry(*, exc=None, countdown=None, max_retries=None):
        raise Retry(message=str(exc))

    task = SimpleNamespace(
        request=SimpleNamespace(id="task-123", retries=0),
        retry=_retry,
    )

    @_serialized_data_fetch("smart_refresh_cache")
    def my_func(self, market=None):
        return "ok"

    with pytest.raises(Retry):
        my_func(task, market="US")

    mock_coordination.acquire_market_workload.assert_called_once_with(
        "smart_refresh_cache",
        "task-123",
        market="US",
    )
    mock_coordination.acquire_external_fetch.assert_called_once_with(
        "smart_refresh_cache",
        "task-123",
    )
    mock_coordination.release_market_workload.assert_called_once_with(
        "task-123",
        market="US",
    )
    mock_lock.release.assert_called_once_with("task-123", market="US")


def test_pipeline_chain_claim_is_an_atomic_market_scoped_lease():
    from app.tasks.workload_coordination import PIPELINE_CHAIN_TTL_SECONDS

    coordination, mock_redis, _ = _make_coordination()
    mock_redis.set.return_value = True

    assert coordination.claim_pipeline_chain("token-1", market="US") is True

    args, kwargs = mock_redis.set.call_args
    assert args[0] == "pipeline_chain:us"
    assert args[1].startswith("daily_market_pipeline:token-1:")
    # NX: two dispatches at once (Beat and a manual Run) cannot both win.
    assert kwargs == {"nx": True, "ex": PIPELINE_CHAIN_TTL_SECONDS}


def test_pipeline_chain_claim_reports_an_existing_holder():
    coordination, mock_redis, _ = _make_coordination()
    mock_redis.set.return_value = None

    assert coordination.claim_pipeline_chain("token-2", market="US") is False


def test_pipeline_chain_release_is_owner_checked():
    coordination, _, mock_release_script = _make_coordination()
    mock_release_script.return_value = 1

    assert coordination.release_pipeline_chain("token-1", market="US") is True
    mock_release_script.assert_called_once_with(keys=["pipeline_chain:us"], args=[":token-1:"])


def test_pipeline_chain_renewal_only_extends_an_existing_claim():
    from app.tasks.workload_coordination import PIPELINE_CHAIN_TTL_SECONDS

    coordination, mock_redis, _ = _make_coordination()
    mock_redis.expire.return_value = 0

    # EXPIRE never creates a key, so renewing after release is a no-op.
    assert coordination.renew_pipeline_chain("US") is False
    mock_redis.expire.assert_called_once_with("pipeline_chain:us", PIPELINE_CHAIN_TTL_SECONDS)


def test_pipeline_chain_holder_reads_token_and_start():
    coordination, mock_redis, _ = _make_coordination()
    mock_redis.get.return_value = b"daily_market_pipeline:token-1:2026-03-16T20:30:00+00:00"
    mock_redis.ttl.return_value = 1000

    holder = coordination.get_pipeline_chain_holder("US")

    assert holder["task_id"] == "token-1"
    assert holder["started_at"] == "2026-03-16T20:30:00+00:00"
    mock_redis.get.assert_called_with("pipeline_chain:us")
