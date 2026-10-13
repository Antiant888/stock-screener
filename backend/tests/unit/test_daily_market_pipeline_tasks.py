"""Daily per-market pipeline orchestration tests."""

from __future__ import annotations

from datetime import date
from datetime import datetime
from types import SimpleNamespace

import pytest


class _FakeSignature:
    def __init__(self, task: str, *, args=None, kwargs=None):
        self.task = task
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.queue = None

    def set(self, queue=None, **_kwargs):
        self.queue = queue
        return self


class _FakeTask:
    def __init__(self, task: str):
        self.task = task

    def si(self, *args, **kwargs):
        return _FakeSignature(self.task, args=args, kwargs=kwargs)


def test_daily_market_pipeline_orders_refresh_compute_and_scan(monkeypatch):
    from app.tasks import daily_market_pipeline_tasks as module

    monkeypatch.setattr(
        "app.tasks.cache_tasks.smart_refresh_cache",
        _FakeTask("app.tasks.cache_tasks.smart_refresh_cache"),
    )
    monkeypatch.setattr(
        "app.tasks.market_rs_tasks.calculate_market_rs_snapshot",
        _FakeTask("app.tasks.market_rs_tasks.calculate_market_rs_snapshot"),
    )
    monkeypatch.setattr(
        "app.tasks.breadth_tasks.calculate_daily_breadth_with_gapfill",
        _FakeTask("app.tasks.breadth_tasks.calculate_daily_breadth_with_gapfill"),
    )
    monkeypatch.setattr(
        "app.tasks.breadth_tasks.calculate_market_exposure",
        _FakeTask("app.tasks.breadth_tasks.calculate_market_exposure"),
    )
    monkeypatch.setattr(
        "app.tasks.group_rank_tasks.calculate_daily_group_rankings_with_gapfill",
        _FakeTask("app.tasks.group_rank_tasks.calculate_daily_group_rankings_with_gapfill"),
    )
    monkeypatch.setattr(
        "app.interfaces.tasks.feature_store_tasks.build_daily_snapshot",
        _FakeTask("app.interfaces.tasks.feature_store_tasks.build_daily_snapshot"),
    )

    signatures = module._build_daily_market_pipeline_signatures("hk", date(2026, 3, 16))

    assert [signature.task for signature in signatures] == [
        "app.tasks.cache_tasks.smart_refresh_cache",
        "app.tasks.daily_market_pipeline_tasks.guard_price_refresh",
        "app.tasks.market_rs_tasks.calculate_market_rs_snapshot",
        "app.tasks.daily_market_pipeline_tasks.guard_market_rs_result",
        "app.tasks.group_rank_tasks.calculate_daily_group_rankings_with_gapfill",
        "app.tasks.daily_market_pipeline_tasks.guard_group_result",
        "app.interfaces.tasks.feature_store_tasks.build_daily_snapshot",
        "app.tasks.daily_market_pipeline_tasks.guard_snapshot_result",
        # Last: a breadth failure (guarded, or an uncatchable OOM kill) must
        # not cost the day's group rankings or scan.
        "app.tasks.breadth_tasks.calculate_daily_breadth_with_gapfill",
        "app.tasks.daily_market_pipeline_tasks.guard_breadth_result",
        "app.tasks.breadth_tasks.calculate_market_exposure",
        "app.tasks.daily_market_pipeline_tasks.guard_exposure_result",
    ]
    assert signatures[0].kwargs == {"mode": "delta", "market": "HK"}
    assert signatures[2].kwargs == {
        "market": "HK",
        "calculation_date": "2026-03-16",
        "formula_version": "balanced-horizon-percentile-v2",
    }
    assert signatures[4].kwargs == {
        "market": "HK",
        "calculation_date": "2026-03-16",
        "execution_policy": "refresh_guarded",
    }
    assert signatures[8].kwargs == {
        "market": "HK",
        "calculation_date": "2026-03-16",
        "execution_policy": "refresh_guarded",
    }
    assert signatures[6].kwargs == {
        "market": "HK",
        "as_of_date_str": "2026-03-16",
        "universe_name": "market:HK",
        "publish_pointer_key": "latest_published_market:HK",
        "static_daily_mode": True,
    }


@pytest.mark.parametrize("market", ("AU", "SG", "MY"))
def test_daily_pipeline_omits_breadth_tasks_and_guards_for_disabled_market(
    monkeypatch,
    market,
):
    from app.tasks import daily_market_pipeline_tasks as module

    monkeypatch.setattr(
        "app.tasks.cache_tasks.smart_refresh_cache",
        _FakeTask("app.tasks.cache_tasks.smart_refresh_cache"),
    )
    monkeypatch.setattr(
        "app.tasks.market_rs_tasks.calculate_market_rs_snapshot",
        _FakeTask("app.tasks.market_rs_tasks.calculate_market_rs_snapshot"),
    )
    monkeypatch.setattr(
        "app.tasks.breadth_tasks.calculate_daily_breadth_with_gapfill",
        _FakeTask("app.tasks.breadth_tasks.calculate_daily_breadth_with_gapfill"),
    )
    monkeypatch.setattr(
        "app.tasks.breadth_tasks.calculate_market_exposure",
        _FakeTask("app.tasks.breadth_tasks.calculate_market_exposure"),
    )
    monkeypatch.setattr(
        "app.tasks.group_rank_tasks.calculate_daily_group_rankings_with_gapfill",
        _FakeTask("app.tasks.group_rank_tasks.calculate_daily_group_rankings_with_gapfill"),
    )
    monkeypatch.setattr(
        "app.interfaces.tasks.feature_store_tasks.build_daily_snapshot",
        _FakeTask("app.interfaces.tasks.feature_store_tasks.build_daily_snapshot"),
    )

    tasks = [
        signature.task
        for signature in module._build_daily_market_pipeline_signatures(
            market,
            date(2026, 3, 16),
        )
    ]

    assert "app.tasks.breadth_tasks.calculate_daily_breadth_with_gapfill" not in tasks
    assert "app.tasks.daily_market_pipeline_tasks.guard_breadth_result" not in tasks
    assert "app.tasks.breadth_tasks.calculate_market_exposure" not in tasks
    assert "app.tasks.daily_market_pipeline_tasks.guard_exposure_result" not in tasks
    assert "app.interfaces.tasks.feature_store_tasks.build_daily_snapshot" in tasks


def test_queue_daily_market_pipeline_skips_disabled_market(monkeypatch):
    from app.tasks import daily_market_pipeline_tasks as module

    monkeypatch.setattr(
        "app.services.runtime_preferences_service.is_market_enabled_now",
        lambda _market: False,
    )
    monkeypatch.setattr(
        "app.services.runtime_preferences_service.get_runtime_bootstrap_status",
        lambda _db: SimpleNamespace(
            bootstrap_required=False,
            bootstrap_state="ready",
            primary_market="US",
            enabled_markets=["US"],
        ),
    )
    monkeypatch.setattr(module, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))

    result = module.queue_daily_market_pipeline.run("HK")

    assert result["status"] == "skipped"
    assert result["reason"] == "market HK is disabled in local runtime preferences"


def test_queue_daily_market_pipeline_prefers_bootstrap_blocker_over_disabled_market(monkeypatch):
    from app.tasks import daily_market_pipeline_tasks as module

    monkeypatch.setattr(
        "app.services.runtime_preferences_service.is_market_enabled_now",
        lambda _market: False,
    )
    monkeypatch.setattr(
        "app.services.runtime_preferences_service.get_runtime_bootstrap_status",
        lambda _db: SimpleNamespace(
            bootstrap_required=True,
            bootstrap_state="not_started",
            primary_market="US",
            enabled_markets=["US"],
        ),
    )
    monkeypatch.setattr(module, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))

    result = module.queue_daily_market_pipeline.run("HK")

    assert result["status"] == "skipped"
    assert result["reason"] == "local_runtime_bootstrap_not_ready"
    assert result["market"] == "HK"


def test_queue_daily_market_pipeline_skips_until_local_bootstrap_ready(monkeypatch):
    from app.tasks import daily_market_pipeline_tasks as module

    class _FakeCalendar:
        def market_now(self, _market):
            return datetime(2026, 3, 16, 10, 0)

        def is_trading_day(self, _market, _today):
            return True

        def last_completed_trading_day(self, _market):
            return date(2026, 3, 16)

    chain_called = False

    class _FakeChain:
        def apply_async(self):
            nonlocal chain_called
            chain_called = True
            return SimpleNamespace(id="queued-task")

    monkeypatch.setattr(
        "app.services.runtime_preferences_service.is_market_enabled_now",
        lambda _market: True,
    )
    monkeypatch.setattr(
        "app.services.runtime_preferences_service.get_runtime_bootstrap_status",
        lambda _db: SimpleNamespace(
            bootstrap_required=True,
            bootstrap_state="not_started",
            primary_market="US",
            enabled_markets=["US"],
        ),
    )
    monkeypatch.setattr(module, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(module, "_market_pipeline_active", lambda _market: None)
    monkeypatch.setattr(module, "MarketCalendarService", lambda: _FakeCalendar())
    monkeypatch.setattr(module, "_build_daily_market_pipeline_signatures", lambda *_args: [])
    monkeypatch.setattr(module, "chain", lambda *_signatures: _FakeChain())

    result = module.queue_daily_market_pipeline.run("US")

    assert result["status"] == "skipped"
    assert result["reason"] == "local_runtime_bootstrap_not_ready"
    assert result["bootstrap_state"] == "not_started"
    assert result["bootstrap_required"] is True
    assert chain_called is False


class _ChainClaims:
    """Stands in for WorkloadCoordination's per-market pipeline-chain lease."""

    def __init__(self, *, fail_claim: bool = False):
        self.holders: dict[str, dict] = {}
        self.task_ids: dict[str, list[str]] = {}
        self.missing_since: dict[str, float] = {}
        self.clock = 0.0
        self.renewed: list[tuple[str, str]] = []
        self.released: list[tuple[str, str]] = []
        self.fail_claim = fail_claim

    def claim_pipeline_chain(self, token, *, market, task_ids):
        if self.fail_claim:
            raise ConnectionError("redis down")
        if market in self.holders:
            return False
        self.holders[market] = {"task_id": token, "started_at": "2026-03-16T20:30:00+00:00"}
        self.task_ids[token] = list(task_ids)
        return True

    def get_pipeline_chain_holder(self, market):
        return self.holders.get(market)

    def get_pipeline_chain_holders(self):
        return dict(self.holders)

    def get_pipeline_chain_task_ids(self, token):
        return self.task_ids.get(token)

    def renew_pipeline_chain(self, token, *, market):
        self.renewed.append((market, token))
        holder = self.holders.get(market)
        return bool(holder and holder["task_id"] == token)

    def pipeline_chain_missing_seconds(self, token):
        return self.clock - self.missing_since.setdefault(token, self.clock)

    def clear_pipeline_chain_missing(self, token):
        self.missing_since.pop(token, None)

    def release_pipeline_chain(self, token, *, market):
        self.released.append((market, token))
        holder = self.holders.get(market)
        if holder and holder["task_id"] == token:
            del self.holders[market]
            return True
        return False


def _ready_to_dispatch(monkeypatch, module, claims, published):
    class _FakeCalendar:
        def market_now(self, _market):
            return datetime(2026, 3, 16, 16, 30)

        def is_trading_day(self, _market, _today):
            return True

        def last_completed_trading_day(self, _market):
            return date(2026, 3, 16)

    class _FakeChain:
        def __init__(self, signatures):
            self.signatures = signatures

        def freeze(self):
            node = None
            for index, _signature in enumerate(self.signatures):
                node = SimpleNamespace(id=f"stage-{index}", parent=node)
            return node

        def apply_async(self, **options):
            published.append((self.signatures, options))
            return SimpleNamespace(id=f"chain-{len(published)}")

    monkeypatch.setattr(
        "app.services.runtime_preferences_service.is_market_enabled_now", lambda _market: True,
    )
    monkeypatch.setattr(module, "_local_runtime_bootstrap_blocker", lambda _market: None)
    monkeypatch.setattr(module, "_market_pipeline_active", lambda _market: None)
    monkeypatch.setattr(module, "MarketCalendarService", lambda: _FakeCalendar())
    monkeypatch.setattr(
        module, "_build_daily_market_pipeline_signatures", lambda *_args: ["prices", "groups", "snapshot"],
    )
    monkeypatch.setattr(module, "chain", lambda *signatures: _FakeChain(list(signatures)))
    monkeypatch.setattr(module, "_pipeline_chain_claims", lambda: claims)


def test_dispatch_holds_the_chain_until_its_last_stage_or_first_failure(monkeypatch):
    from app.tasks import daily_market_pipeline_tasks as module

    claims, published = _ChainClaims(), []
    _ready_to_dispatch(monkeypatch, module, claims, published)

    result = module.queue_daily_market_pipeline.run("US")

    assert result["status"] == "queued"
    token = claims.holders["US"]["task_id"]
    [(signatures, options)] = published
    # Released by a final stage on success...
    release = signatures[-1]
    assert signatures[:-1] == ["prices", "groups", "snapshot"]
    assert release.task == "app.tasks.daily_market_pipeline_tasks.release_daily_market_pipeline_claim"
    assert release.kwargs == {"market": "US", "token": token}
    assert release.immutable
    # ...and by the same task as every stage's error callback.
    assert options["link_error"] is release
    # The claim records the ids the chain is published with, release included.
    assert claims.task_ids[token] == ["stage-0", "stage-1", "stage-2", "stage-3"]


def test_second_dispatch_between_stages_is_refused(monkeypatch):
    # The per-stage workload lease is free between stages; the chain claim
    # is what stops a second chain for the same market.
    from app.tasks import daily_market_pipeline_tasks as module

    claims, published = _ChainClaims(), []
    _ready_to_dispatch(monkeypatch, module, claims, published)

    first = module.queue_daily_market_pipeline.run("US")
    second = module.queue_daily_market_pipeline.run("US")

    assert first["status"] == "queued"
    assert second["status"] == "skipped"
    assert second["reason"] == "market_pipeline_already_active"
    # A claim token, not a Celery task id: named so nobody looks it up as one.
    assert second["active_chain_token"] == claims.holders["US"]["task_id"]
    assert "active_task_id" not in second
    assert second["active_since"] == "2026-03-16T20:30:00+00:00"
    assert len(published) == 1


def test_claim_freed_between_claim_and_holder_lookup_is_retried(monkeypatch):
    # The first chain releases right after the second's SET NX lost: the
    # second dispatch must not skip with nothing running.
    from app.tasks import daily_market_pipeline_tasks as module

    claims, published = _ChainClaims(), []
    _ready_to_dispatch(monkeypatch, module, claims, published)
    attempts = []
    real_claim = claims.claim_pipeline_chain

    def flaky_claim(token, *, market, task_ids):
        attempts.append(token)
        return False if len(attempts) == 1 else real_claim(token, market=market, task_ids=task_ids)

    monkeypatch.setattr(claims, "claim_pipeline_chain", flaky_claim)

    result = module.queue_daily_market_pipeline.run("US")

    assert result["status"] == "queued"
    assert len(attempts) == 2
    assert len(published) == 1


def test_dispatch_fails_open_when_the_coordination_cannot_be_built(monkeypatch, caplog):
    from app.tasks import daily_market_pipeline_tasks as module

    claims, published = _ChainClaims(), []
    _ready_to_dispatch(monkeypatch, module, claims, published)

    def no_coordination():
        raise RuntimeError("Redis package is not installed")

    monkeypatch.setattr(module, "_pipeline_chain_claims", no_coordination)

    result = module.queue_daily_market_pipeline.run("US")

    assert result["status"] == "queued"
    assert "Could not claim the US pipeline chain" in caplog.text


def test_real_celery_chain_attaches_the_release_to_every_stage():
    # Uses Celery's own chain preparation, not the fakes above.
    from celery import chain
    from celery.utils.functional import arity_greater

    from app.tasks import daily_market_pipeline_tasks as module

    release = module.release_daily_market_pipeline_claim.si(market="US", token="tok")
    stages = [
        module.guard_price_refresh.s(market="US"),
        module.guard_group_result.s(market="US"),
        release,
    ]

    tasks, _results = chain(*stages).prepare_steps(
        (), {}, chain(*stages).tasks, link_error=release, app=module.celery_app,
    )

    assert len(tasks) == 3
    for task in tasks:
        [errback] = task.options["link_error"]
        assert errback["task"] == release.task
        assert errback["kwargs"] == {"market": "US", "token": "tok"}
    # Published as a task (immutable signature drops the failed task id),
    # not called inline with (request, exc, traceback).
    assert arity_greater(module.release_daily_market_pipeline_claim.__header__, 1) is False


def test_other_markets_are_not_blocked_by_a_running_chain(monkeypatch):
    from app.tasks import daily_market_pipeline_tasks as module

    claims, published = _ChainClaims(), []
    _ready_to_dispatch(monkeypatch, module, claims, published)

    assert module.queue_daily_market_pipeline.run("US")["status"] == "queued"
    assert module.queue_daily_market_pipeline.run("HK")["status"] == "queued"


def test_a_failed_publish_releases_the_claim(monkeypatch):
    from app.tasks import daily_market_pipeline_tasks as module

    claims, published = _ChainClaims(), []
    _ready_to_dispatch(monkeypatch, module, claims, published)

    class _BrokenChain:
        def freeze(self):
            return SimpleNamespace(id="stage-0", parent=None)

        def apply_async(self, **_options):
            raise ConnectionError("broker unavailable")

    monkeypatch.setattr(module, "chain", lambda *_signatures: _BrokenChain())

    with pytest.raises(ConnectionError):
        module.queue_daily_market_pipeline.run("US")

    assert claims.holders == {}


def test_a_failed_release_after_a_failed_publish_keeps_the_publish_error(monkeypatch, caplog):
    from app.tasks import daily_market_pipeline_tasks as module

    claims, published = _ChainClaims(), []
    _ready_to_dispatch(monkeypatch, module, claims, published)

    class _BrokenChain:
        def freeze(self):
            return SimpleNamespace(id="stage-0", parent=None)

        def apply_async(self, **_options):
            raise ConnectionError("broker unavailable")

    def broken_release(_token, *, market):
        raise TimeoutError("redis down")

    monkeypatch.setattr(module, "chain", lambda *_signatures: _BrokenChain())
    monkeypatch.setattr(claims, "release_pipeline_chain", broken_release)

    with pytest.raises(ConnectionError, match="broker unavailable"):
        module.queue_daily_market_pipeline.run("US")
    assert "Could not release the US pipeline chain claim" in caplog.text


def test_dispatch_still_runs_when_the_claim_store_is_down(monkeypatch, caplog):
    # The daily pipeline matters more than duplicate protection: fail open,
    # loudly, as the existing workload-holder check already does.
    from app.tasks import daily_market_pipeline_tasks as module

    claims, published = _ChainClaims(fail_claim=True), []
    _ready_to_dispatch(monkeypatch, module, claims, published)

    result = module.queue_daily_market_pipeline.run("US")

    assert result["status"] == "queued"
    [(signatures, options)] = published
    assert signatures == ["prices", "groups", "snapshot"]
    assert "link_error" not in options
    assert "Could not claim the US pipeline chain" in caplog.text


def test_release_only_frees_its_own_claim(monkeypatch):
    from app.tasks import daily_market_pipeline_tasks as module

    claims = _ChainClaims()
    claims.holders["US"] = {"task_id": "newer-chain", "started_at": "x"}
    monkeypatch.setattr(module, "_pipeline_chain_claims", lambda: claims)

    result = module.release_daily_market_pipeline_claim.run(market="US", token="older-chain")

    assert result == {"status": "ok", "market": "US", "stage": "release", "released": False}
    assert claims.holders["US"]["task_id"] == "newer-chain"


def test_release_task_takes_no_positional_args():
    # Celery calls an errback inline with (request, exc, traceback) when its
    # task takes more than one positional argument; keyword-only arguments
    # keep it a published task whose immutable signature ignores the task id.
    import inspect

    from app.tasks import daily_market_pipeline_tasks as module

    params = inspect.signature(module.release_daily_market_pipeline_claim.run).parameters.values()
    assert all(param.kind is inspect.Parameter.KEYWORD_ONLY for param in params)


# ── Heartbeat (#605): keep the claim while the chain lives ───────────────


def _dispatched_chain(monkeypatch, states, live_ids=()):
    """Dispatch a US chain, then report ``states`` for its stage ids (by
    index; unlisted stages read PENDING) and ``live_ids`` as found in the
    broker. Returns the set the broker search reads, to change between beats,
    and the list of searches made."""
    from app.tasks import daily_market_pipeline_tasks as module

    claims, published = _ChainClaims(), []
    _ready_to_dispatch(monkeypatch, module, claims, published)
    assert module.queue_daily_market_pipeline.run("US")["status"] == "queued"
    token = claims.holders["US"]["task_id"]
    stage_ids = claims.task_ids[token]
    by_id = {stage_ids[index]: state for index, state in states.items()}
    monkeypatch.setattr(
        module.celery_app,
        "AsyncResult",
        lambda task_id: SimpleNamespace(state=by_id.get(task_id, "PENDING")),
    )
    live = {stage_ids[index] for index in live_ids}
    searches = []

    def still_in_broker(task_ids, *, cache=True):
        searches.append((list(task_ids), cache))
        return {task_id: task_id in live for task_id in task_ids}

    monkeypatch.setattr(
        "app.services.task_registry_service.still_in_broker", still_in_broker
    )
    return module, claims, token, (live, searches)


def test_a_chain_queued_behind_a_backlog_keeps_its_claim(monkeypatch):
    # Prices finished long ago (its result expired, so it reads PENDING) and
    # the next stage still waits in the data-fetch queue: the chain is alive.
    module, claims, token, _ = _dispatched_chain(monkeypatch, states={}, live_ids=(1,))

    for _beat in range(30):  # 2.5 hours of heartbeats
        assert module.renew_daily_market_pipeline_claims.run()["claims"] == {"US": "renewed"}

    assert claims.holders["US"]["task_id"] == token
    assert claims.released == []
    assert module.queue_daily_market_pipeline.run("US")["reason"] == "market_pipeline_already_active"


def test_a_running_or_retrying_stage_keeps_the_claim_without_reading_the_broker(monkeypatch):
    # PROGRESS: a running price refresh reports progress through update_state,
    # and its acked message is in no queue, so only its state shows it runs.
    for state in ("STARTED", "RETRY", "PROGRESS"):
        module, claims, _token, (_live, searches) = _dispatched_chain(
            monkeypatch, states={0: "SUCCESS", 1: state}
        )

        assert module.renew_daily_market_pipeline_claims.run()["claims"] == {"US": "renewed"}
        assert searches == []


def test_a_revoked_stage_frees_the_claim_within_one_heartbeat(monkeypatch):
    module, claims, token, _ = _dispatched_chain(
        monkeypatch, states={0: "SUCCESS", 1: "REVOKED"}, live_ids=(1,)
    )

    assert module.renew_daily_market_pipeline_claims.run()["claims"] == {"US": "released_dead"}
    assert claims.released == [("US", token)]
    assert module.queue_daily_market_pipeline.run("US")["status"] == "queued"


def test_a_failed_stage_or_a_finished_chain_frees_the_claim(monkeypatch):
    module, claims, _token, _ = _dispatched_chain(monkeypatch, states={0: "FAILURE"})
    assert module.renew_daily_market_pipeline_claims.run()["claims"] == {"US": "released_dead"}

    module, claims, _token, _ = _dispatched_chain(
        monkeypatch, states={0: "SUCCESS", 1: "SUCCESS", 2: "SUCCESS", 3: "SUCCESS"}
    )
    assert module.renew_daily_market_pipeline_claims.run()["claims"] == {"US": "released_done"}


def test_a_chain_with_no_trace_is_freed_on_the_next_heartbeat_only(monkeypatch):
    # One check can fall between a worker taking a stage and storing its state.
    from app.tasks.workload_coordination import PIPELINE_CHAIN_HEARTBEAT_SECONDS

    module, claims, token, _ = _dispatched_chain(monkeypatch, states={0: "SUCCESS"})

    assert module.renew_daily_market_pipeline_claims.run()["claims"] == {"US": "missing"}
    assert claims.holders["US"]["task_id"] == token

    claims.clock += PIPELINE_CHAIN_HEARTBEAT_SECONDS
    assert module.renew_daily_market_pipeline_claims.run()["claims"] == {"US": "released_missing"}
    assert claims.holders == {}


def test_overlapping_heartbeats_cannot_both_count_one_gap(monkeypatch):
    # A slow heartbeat and the next one, both landing in the same handoff gap.
    module, claims, token, _ = _dispatched_chain(monkeypatch, states={0: "SUCCESS"})

    assert module.renew_daily_market_pipeline_claims.run()["claims"] == {"US": "missing"}
    claims.clock += 5
    assert module.renew_daily_market_pipeline_claims.run()["claims"] == {"US": "missing"}
    assert claims.holders["US"]["task_id"] == token


def test_a_live_heartbeat_resets_the_missing_clock(monkeypatch):
    from app.tasks.workload_coordination import PIPELINE_CHAIN_HEARTBEAT_SECONDS

    module, claims, token, (live, _searches) = _dispatched_chain(monkeypatch, states={})

    assert module.renew_daily_market_pipeline_claims.run()["claims"] == {"US": "missing"}
    claims.clock += PIPELINE_CHAIN_HEARTBEAT_SECONDS
    live.add(claims.task_ids[token][1])
    assert module.renew_daily_market_pipeline_claims.run()["claims"] == {"US": "renewed"}
    claims.clock += PIPELINE_CHAIN_HEARTBEAT_SECONDS
    live.clear()
    assert module.renew_daily_market_pipeline_claims.run()["claims"] == {"US": "missing"}
    assert claims.holders["US"]["task_id"] == token


def test_a_broker_search_out_of_budget_keeps_the_claim(monkeypatch):
    # still_in_broker answers None when the broker is unreadable or the
    # backlog is too big to search in its budget: keep the claim and do not
    # start the missing clock.
    from app.tasks.workload_coordination import PIPELINE_CHAIN_HEARTBEAT_SECONDS

    module, claims, token, _ = _dispatched_chain(monkeypatch, states={})
    monkeypatch.setattr(
        "app.services.task_registry_service.still_in_broker",
        lambda task_ids, *, cache=True: {task_id: None for task_id in task_ids},
    )

    for _beat in range(3):
        assert module.renew_daily_market_pipeline_claims.run()["claims"] == {"US": "unknown"}
        claims.clock += PIPELINE_CHAIN_HEARTBEAT_SECONDS
    assert claims.holders["US"]["task_id"] == token
    assert claims.missing_since == {}


def test_undecided_chains_share_one_fresh_broker_search(monkeypatch):
    module, claims, token, (_live, searches) = _dispatched_chain(monkeypatch, states={})
    claims.holders["HK"] = {"task_id": "hk-token", "started_at": "x"}
    claims.task_ids["hk-token"] = ["hk-0", "hk-1"]

    module.renew_daily_market_pipeline_claims.run()

    # One bounded search for every undecided chain, never from the cache: a
    # cached "not found" from before a stage was published would read a live
    # chain as missing.
    [(searched, cache)] = searches
    assert sorted(searched) == sorted([*claims.task_ids[token], "hk-0", "hk-1"])
    assert cache is False


def test_a_failing_heartbeat_keeps_the_claim(monkeypatch, caplog):
    module, claims, token, _ = _dispatched_chain(monkeypatch, states={})

    def backend_down(_task_id):
        raise ConnectionError("result backend down")

    monkeypatch.setattr(module.celery_app, "AsyncResult", backend_down)

    assert module.renew_daily_market_pipeline_claims.run()["claims"] == {"US": "error"}
    assert claims.holders["US"]["task_id"] == token
    assert claims.released == []
    assert "Could not check the US pipeline chain claim" in caplog.text


def test_a_claim_taken_before_stage_ids_were_recorded_is_left_to_expire(monkeypatch):
    from app.tasks import daily_market_pipeline_tasks as module

    claims = _ChainClaims()
    claims.holders["US"] = {"task_id": "pre-605", "started_at": "x"}
    monkeypatch.setattr(module, "_pipeline_chain_claims", lambda: claims)

    assert module.renew_daily_market_pipeline_claims.run()["claims"] == {"US": "untracked"}
    assert claims.holders["US"]["task_id"] == "pre-605"


def test_the_heartbeat_never_renews_or_frees_a_newer_chains_claim(monkeypatch):
    # The claim changed hands after the holder was read.
    module, claims, token, _ = _dispatched_chain(monkeypatch, states={1: "REVOKED"})
    claims.holders["US"] = {"task_id": "newer", "started_at": "y"}
    monkeypatch.setattr(
        claims, "get_pipeline_chain_holders", lambda: {"US": {"task_id": token}}
    )

    assert module.renew_daily_market_pipeline_claims.run()["claims"] == {"US": "lost"}
    assert claims.holders["US"]["task_id"] == "newer"


@pytest.mark.allow_real_celery_dispatch
def test_real_celery_chain_publishes_with_the_recorded_ids(monkeypatch):
    # Celery's own chain preparation and message building, with the network
    # seams stubbed: the broker producer is in memory, the result backend's
    # subscribe is a no-op, and send_task_message is captured. CI has no
    # Redis, so a test reaching either service fails there.
    from contextlib import nullcontext

    from celery import chain
    from kombu import Connection

    from app.tasks import daily_market_pipeline_tasks as module

    memory = Connection("memory://")
    monkeypatch.setattr(
        module.celery_app, "producer_or_acquire", lambda _producer=None: nullcontext(memory.Producer())
    )
    monkeypatch.setattr(module.celery_app.backend, "on_task_call", lambda *_args, **_kwargs: None)

    release = module.release_daily_market_pipeline_claim.si(market="US", token="tok")
    workflow = chain(
        module.guard_price_refresh.s(market="US"),
        module.guard_group_result.s(market="US"),
        release,
    )
    task_ids = module._chain_task_ids(workflow)

    published = []
    monkeypatch.setattr(
        module.celery_app.amqp,
        "send_task_message",
        lambda _producer, _name, message, *args, **kwargs: published.append(message),
    )
    workflow.apply_async(link_error=release)
    memory.release()

    [first] = published
    _args, _kwargs, embed = first.body
    later = [signature["options"]["task_id"] for signature in reversed(embed["chain"])]
    assert [first.headers["id"], *later] == task_ids
    assert len(set(task_ids)) == 3


def test_guard_price_refresh_fails_lock_contention_skip_result():
    from app.tasks import daily_market_pipeline_tasks as module

    result = {
        "status": "already_running",
        "skipped": True,
        "reason": "data_fetch_lock_active",
    }

    with pytest.raises(RuntimeError, match="Daily price refresh failed for HK"):
        module.guard_price_refresh.run(result, market="HK")


def test_snapshot_guard_preserves_source_identity_for_options_follow_on():
    from app.tasks import daily_market_pipeline_tasks as module

    result = module.guard_snapshot_result.run(
        {
            "auto_scan_id": "scan-1",
            "run_id": 33,
            "as_of_date": "2026-03-16",
        },
        market="US",
    )

    assert result["auto_scan_id"] == "scan-1"
    assert result["run_id"] == 33
    assert result["as_of_date"] == "2026-03-16"


def test_options_follow_on_is_fire_and_report(monkeypatch):
    from app.tasks import daily_market_pipeline_tasks as module

    dispatched = []
    monkeypatch.setattr(
        "app.interfaces.tasks.options_analytics_tasks.refresh_options_analytics.apply_async",
        lambda **values: dispatched.append(values) or SimpleNamespace(id="options-1"),
    )

    result = module.dispatch_options_after_snapshot.run(
        {"run_id": 33, "as_of_date": "2026-03-16", "auto_scan_id": "scan-1"},
        market="US",
    )

    assert result["options_dispatch"] == {"status": "queued", "task_id": "options-1"}
    assert dispatched[0]["queue"] == "data_fetch_us"


def test_options_follow_on_uses_already_published_snapshot_identity(monkeypatch):
    from app.tasks import daily_market_pipeline_tasks as module

    dispatched = []
    monkeypatch.setattr(
        "app.interfaces.tasks.options_analytics_tasks.refresh_options_analytics.apply_async",
        lambda **values: dispatched.append(values) or SimpleNamespace(id="options-1"),
    )
    guarded = module.guard_snapshot_result.run(
        {
            "status": "skipped",
            "reason": "already_published",
            "existing_run_id": 123,
            "auto_scan_id": 456,
        },
        market="US",
    )

    module.dispatch_options_after_snapshot.run(guarded, market="US")

    assert dispatched[0]["kwargs"] == {"source_run_id": 123, "market": "US"}


def test_guard_price_refresh_accepts_high_coverage_partial_result():
    from app.tasks import daily_market_pipeline_tasks as module

    result = {
        "status": "partial",
        "refreshed": 900,
        "failed": 100,
        "total": 1000,
    }

    assert module.guard_price_refresh.run(result, market="HK") == {
        "status": "ok",
        "market": "HK",
        "stage": "prices",
    }


def test_guard_price_refresh_uses_coverage_denominator_when_present():
    from app.tasks import daily_market_pipeline_tasks as module

    result = {
        "status": "partial",
        "refreshed": 50,
        "failed": 34,
        "total": 84,
        "coverage_refreshed": 9949,
        "coverage_failed": 34,
        "coverage_total": 9983,
        "live_top_up_refreshed": 50,
        "live_top_up_failed": 34,
        "live_top_up_total": 84,
    }

    assert module.guard_price_refresh.run(result, market="US") == {
        "status": "ok",
        "market": "US",
        "stage": "prices",
    }


def test_guard_price_refresh_falls_back_when_coverage_values_are_null():
    from app.tasks import daily_market_pipeline_tasks as module

    result = {
        "status": "partial",
        "refreshed": 900,
        "failed": 100,
        "total": 1000,
        "coverage_refreshed": None,
        "coverage_total": None,
    }

    assert module.guard_price_refresh.run(result, market="HK") == {
        "status": "ok",
        "market": "HK",
        "stage": "prices",
    }


def test_guard_price_refresh_fails_low_coverage_partial_result():
    from app.tasks import daily_market_pipeline_tasks as module

    result = {
        "status": "partial",
        "refreshed": 899,
        "failed": 101,
        "total": 1000,
    }

    with pytest.raises(RuntimeError, match="Daily price refresh failed for HK"):
        module.guard_price_refresh.run(result, market="HK")


def test_guard_snapshot_result_accepts_already_published_scan_result():
    from app.tasks import daily_market_pipeline_tasks as module

    result = {
        "status": "skipped",
        "reason": "already_published",
        "existing_run_id": 123,
        "auto_scan_id": 456,
    }

    assert module.guard_snapshot_result.run(result, market="HK") == {
        "status": "ok",
        "market": "HK",
        "stage": "scan",
        "auto_scan_id": 456,
        "run_id": 123,
    }


def test_guard_group_result_accepts_no_taxonomy_market_skip():
    from app.tasks import daily_market_pipeline_tasks as module

    result = {
        "status": "skipped",
        "reason": "no_taxonomy_for_market",
        "market": "JP",
    }

    assert module.guard_group_result.run(result, market="JP") == {
        "status": "ok",
        "market": "JP",
        "stage": "groups",
    }


def test_guard_exposure_result_skips_without_aborting_pipeline():
    # Exposure is a non-critical leaf: a failed/missing exposure result must NOT
    # raise (which would abort groups + snapshot); it returns a skipped status.
    from app.tasks import daily_market_pipeline_tasks as module

    result = {"error": "benchmark_not_current", "market": "US", "date": "2026-06-19"}
    assert module.guard_exposure_result.run(result, market="US") == {
        "status": "skipped",
        "market": "US",
        "stage": "exposure",
    }


def test_guard_market_rs_blocks_failed_result_when_balanced_is_active(monkeypatch):
    from app.domain.relative_strength import BALANCED_RS_FORMULA_VERSION
    from app.tasks import daily_market_pipeline_tasks as module

    monkeypatch.setattr(
        module,
        "_active_formula_for_market",
        lambda _market: BALANCED_RS_FORMULA_VERSION,
    )

    with pytest.raises(RuntimeError, match="Canonical Market RS failed for US"):
        module.guard_market_rs_result.run(
            {
                "status": "failed",
                "market": "US",
                "as_of_date": "2026-04-10",
                "formula_version": BALANCED_RS_FORMULA_VERSION,
                "reason_code": "benchmark_anchor_missing",
            },
            market="US",
            calculation_date="2026-04-10",
        )


def test_guard_market_rs_allows_failed_shadow_when_legacy_is_active(monkeypatch):
    from app.domain.relative_strength import (
        BALANCED_RS_FORMULA_VERSION,
        LEGACY_RS_FORMULA_VERSION,
    )
    from app.tasks import daily_market_pipeline_tasks as module

    monkeypatch.setattr(
        module,
        "_active_formula_for_market",
        lambda _market: LEGACY_RS_FORMULA_VERSION,
    )

    assert module.guard_market_rs_result.run(
        {
            "status": "failed",
            "market": "US",
            "as_of_date": "2026-04-10",
            "formula_version": BALANCED_RS_FORMULA_VERSION,
            "reason_code": "benchmark_anchor_missing",
        },
        market="US",
        calculation_date="2026-04-10",
    ) == {
        "status": "skipped",
        "market": "US",
        "stage": "market_rs_shadow",
        "as_of_date": "2026-04-10",
        "formula_version": BALANCED_RS_FORMULA_VERSION,
        "market_rs_run_id": None,
    }


def test_guard_market_rs_accepts_exact_completed_balanced_run(monkeypatch):
    from app.domain.relative_strength import BALANCED_RS_FORMULA_VERSION
    from app.tasks import daily_market_pipeline_tasks as module

    monkeypatch.setattr(
        module,
        "_active_formula_for_market",
        lambda _market: BALANCED_RS_FORMULA_VERSION,
    )

    assert module.guard_market_rs_result.run(
        {
            "status": "completed",
            "market": "US",
            "as_of_date": "2026-04-10",
            "formula_version": BALANCED_RS_FORMULA_VERSION,
            "market_rs_run_id": 42,
            "eligible_symbol_count": 5000,
        },
        market="US",
        calculation_date="2026-04-10",
    ) == {
        "status": "ok",
        "market": "US",
        "stage": "market_rs",
        "as_of_date": "2026-04-10",
        "formula_version": BALANCED_RS_FORMULA_VERSION,
        "market_rs_run_id": 42,
    }
