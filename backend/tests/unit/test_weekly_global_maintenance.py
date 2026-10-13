"""#615: the market-independent cleanups run weekly without an operator."""

from __future__ import annotations

import fnmatch
from unittest.mock import MagicMock, patch

import pytest

from app.celery_app import _build_cache_warmup_beat_schedule
from app.services.task_registry_service import SCHEDULED_TASKS, _registry_entry_for
from app.tasks import cache_tasks

TASK = "app.tasks.cache_tasks.weekly_global_maintenance"


class _Redis:
    def __init__(self, keys, error=None):
        self.keys = {k.encode() for k in keys}
        self.error = error

    def scan_iter(self, match, count):
        if self.error:
            raise self.error
        return [k for k in sorted(self.keys) if fnmatch.fnmatchcase(k.decode(), match)]

    def delete(self, *keys):
        self.keys -= set(keys)


def _db(rows):
    db = MagicMock()
    db.query.return_value = rows  # iterated as (symbol, is_active)
    return db


def _clean(rows, keys, **redis_kwargs):
    redis = _Redis(keys, **redis_kwargs)
    with patch("app.services.redis_pool.get_redis_client", return_value=redis):
        cleaned = cache_tasks._cleanup_orphaned_price_cache([(s.upper(), a) for s, a in rows])
    return cleaned, {k.decode() for k in redis.keys}


def test_removes_every_markets_keys_for_a_delisted_symbol_and_keeps_the_rest():
    rows = [("AAPL", True), ("0700.HK", False), ("OLDCO", False)]
    keys = {
        # Delisted HK symbol: the old cleanup only ever deleted price:US:... keys.
        "price:HK:0700.HK:recent", "price:HK:0700.HK:last_update", "price:HK:0700.HK:fetch_meta",
        "price:OLDCO:recent",  # legacy unscoped key
        "price:US:AAPL:recent", "price:US:AAPL:last_update",  # active
        # Never in the universe (benchmarks, COT instruments): cached on purpose.
        "price:US:SPY:recent", "price:US:^VIX:recent",
        "fundamentals:US:0700.HK",  # another namespace
    }

    cleaned, left = _clean(rows, keys)

    assert cleaned == 2
    assert left == {
        "price:US:AAPL:recent", "price:US:AAPL:last_update",
        "price:US:SPY:recent", "price:US:^VIX:recent",
        "fundamentals:US:0700.HK",
    }


def test_a_symbol_active_in_any_row_is_kept():
    _, left = _clean([("DUAL", False), ("DUAL", True)], {"price:US:DUAL:recent"})

    assert left == {"price:US:DUAL:recent"}


def test_an_empty_universe_deletes_nothing():
    # The old cleanup diffed against the active set, so an empty universe
    # made the whole price cache look orphaned.
    cleaned, left = _clean([], {"price:US:AAPL:recent", "price:HK:0700.HK:recent"})

    assert cleaned == 0
    assert left == {"price:US:AAPL:recent", "price:HK:0700.HK:recent"}


def test_redis_errors_fail_the_run_instead_of_reading_as_zero_cleaned():
    with pytest.raises(ConnectionError):
        _clean([("OLDCO", False)], {"price:OLDCO:recent"}, error=ConnectionError("redis down"))
    with patch("app.services.redis_pool.get_redis_client", return_value=None), pytest.raises(RuntimeError):
        cache_tasks._cleanup_orphaned_price_cache([("OLDCO", False)])


def test_the_task_runs_telemetry_retention_even_if_the_cache_step_fails():
    telemetry = MagicMock()
    telemetry.cleanup_old_events.return_value = 7
    with patch.object(cache_tasks, "SessionLocal", return_value=_db([("OLDCO", False)])), patch(
        "app.services.telemetry.get_telemetry", return_value=telemetry
    ), patch("app.services.redis_pool.get_redis_client", return_value=_Redis(set(), error=ConnectionError("x"))):
        with pytest.raises(ConnectionError):
            cache_tasks.weekly_global_maintenance.run()

    telemetry.cleanup_old_events.assert_called_once_with()


def test_the_task_reports_both_counts():
    telemetry = MagicMock()
    telemetry.cleanup_old_events.return_value = 7
    with patch.object(cache_tasks, "SessionLocal", return_value=_db([("OLDCO", False)])), patch(
        "app.services.telemetry.get_telemetry", return_value=telemetry
    ), patch("app.services.redis_pool.get_redis_client", return_value=_Redis({"price:US:OLDCO:recent"})):
        result = cache_tasks.weekly_global_maintenance.run()

    assert (result["orphans_cleaned"], result["telemetry_events_deleted"]) == (1, 7)


@pytest.mark.parametrize("enabled_markets", [["US", "HK"], []])
def test_beat_runs_it_weekly_whatever_markets_are_enabled(enabled_markets):
    # A shared entry: needed even when no market's cache warming is enabled.
    entry = _build_cache_warmup_beat_schedule(enabled_markets)["weekly-global-maintenance"]

    assert entry["task"] == TASK
    schedule = entry["schedule"]
    assert (schedule.day_of_week, schedule.hour, schedule.minute) == ({0}, {6}, {0})


def test_runs_show_under_their_own_scheduled_job():
    assert SCHEDULED_TASKS["weekly-global-maintenance"]["task_function"] == TASK
    assert _registry_entry_for(TASK, {}) == "weekly-global-maintenance"
