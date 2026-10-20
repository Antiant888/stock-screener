"""Fresh-install balanced Market RS bootstrap lifecycle tests."""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.domain.relative_strength import BALANCED_RS_FORMULA_VERSION, HORIZON_SESSIONS
from app.models.stock import StockPrice
from app.models.stock_universe import UNIVERSE_STATUS_ACTIVE, StockUniverse
from app.services.market_rs_inputs import MarketRsInputUnavailable

RUNTIME_MARKET_RS_ANCHORS = {
    0: date(2026, 4, 10),
    1: date(2026, 4, 9),
    5: date(2026, 4, 3),
    21: date(2026, 3, 10),
    63: date(2026, 1, 9),
    126: date(2025, 10, 10),
    189: date(2025, 7, 11),
    252: date(2025, 4, 10),
}


class _RecordingStore:
    def __init__(self) -> None:
        self.manifests = []

    def claim(self, manifest):
        self.manifests.append(manifest)
        return manifest

    def update(self, manifest):
        self.manifests.append(manifest)
        return manifest


class _MarketRsCalendarStub:
    @staticmethod
    def normalize_market(market):
        return str(market).upper()

    @staticmethod
    def market_now(_market):
        return datetime(2026, 8, 1, tzinfo=timezone.utc)

    @staticmethod
    def session_anchors(_market, _as_of_date, *, offsets):
        assert set(offsets) == set(HORIZON_SESSIONS.values())
        return dict(RUNTIME_MARKET_RS_ANCHORS)


def _runtime_market_rs_price(symbol: str, offset: int, adjusted: float) -> StockPrice:
    return StockPrice(
        symbol=symbol,
        date=RUNTIME_MARKET_RS_ANCHORS[offset],
        adj_close=adjusted,
        close=adjusted,
    )


def _seed_fresh_import_market_rs_inputs(db_session) -> None:
    first_seen_after_history = datetime(2026, 7, 24, tzinfo=timezone.utc)
    db_session.add_all(
        [
            StockUniverse(
                symbol="AAA",
                market="US",
                is_active=True,
                status=UNIVERSE_STATUS_ACTIVE,
                first_seen_at=first_seen_after_history,
            ),
            StockUniverse(
                symbol="BBB",
                market="US",
                is_active=True,
                status=UNIVERSE_STATUS_ACTIVE,
                first_seen_at=first_seen_after_history,
            ),
            *[
                _runtime_market_rs_price("AAA", offset, 110.0 + offset)
                for offset in RUNTIME_MARKET_RS_ANCHORS
            ],
            *[
                _runtime_market_rs_price("BBB", offset, 105.0 + offset)
                for offset in RUNTIME_MARKET_RS_ANCHORS
            ],
            *[
                _runtime_market_rs_price("SPY", offset, 100.0 + offset)
                for offset in RUNTIME_MARKET_RS_ANCHORS
            ],
        ]
    )
    db_session.commit()


def test_runtime_market_rs_default_input_loader_remains_strict_for_fresh_import_history(
    db_session,
) -> None:
    from app.wiring.canonical_rs_runtime import CanonicalRsRuntime

    _seed_fresh_import_market_rs_inputs(db_session)
    runtime = CanonicalRsRuntime(
        session_factory=lambda: db_session,
        market_calendar=_MarketRsCalendarStub(),
    )

    with pytest.raises(MarketRsInputUnavailable) as exc_info:
        runtime.input_loader().load(
            db_session,
            market="US",
            as_of_date=RUNTIME_MARKET_RS_ANCHORS[0],
        )

    assert (
        exc_info.value.reason_code
        == "current_adjusted_price_coverage_below_threshold"
    )
    assert exc_info.value.expected_symbol_count == 0


def test_rollout_market_rs_uses_current_active_fallback_for_fresh_import_history(
    db_session,
) -> None:
    from app.wiring.canonical_rs_runtime import CanonicalRsRuntime

    _seed_fresh_import_market_rs_inputs(db_session)
    runtime = CanonicalRsRuntime(
        session_factory=lambda: db_session,
        market_calendar=_MarketRsCalendarStub(),
    )

    inputs = runtime.rollout_service().backfill_service.input_loader.load(
        db_session,
        market="US",
        as_of_date=RUNTIME_MARKET_RS_ANCHORS[0],
    )

    assert inputs.expected_symbols == ("AAA", "BBB")
    assert inputs.current_price_coverage == pytest.approx(1.0)


def test_fresh_dispatch_identity_survives_partial_bootstrap(monkeypatch) -> None:
    from app.tasks import runtime_bootstrap_tasks as module

    db = MagicMock()
    manifest_repository = MagicMock()
    manifest_repository.load.return_value = SimpleNamespace(
        fresh_install=True,
        pending_balanced_activation_markets=("US",),
    )
    readiness = MagicMock()
    readiness.is_pristine_installation.return_value = False
    monkeypatch.setattr(module, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        module,
        "BootstrapRunManifestRepository",
        lambda: manifest_repository,
    )
    monkeypatch.setattr(
        "app.services.bootstrap_readiness_service.BootstrapReadinessService",
        lambda: readiness,
    )

    state = module._balanced_activation_state_at_dispatch(("US",))

    assert state.fresh_install is True
    assert state.pending_markets == ("US",)
    manifest_repository.load.assert_called_once_with(db)
    readiness.is_pristine_installation.assert_not_called()
    db.close.assert_called_once_with()


def _seed_startup_daily_price_input(db_session) -> None:
    db_session.add(
        StockUniverse(
            symbol="AAPL",
            market="US",
            is_active=True,
            status=UNIVERSE_STATUS_ACTIVE,
        )
    )
    db_session.add(
        StockPrice(
            symbol="AAPL",
            date=date(2026, 7, 31),
            open=210.0,
            high=211.0,
            low=209.0,
            close=210.0,
            adj_close=210.0,
            volume=1_000_000,
        )
    )
    db_session.commit()


def test_fresh_dispatch_rejects_raw_data_without_startup_marker(
    db_session,
) -> None:
    from app.tasks import runtime_bootstrap_tasks as module

    _seed_startup_daily_price_input(db_session)

    state = module._balanced_activation_state_at_dispatch(("US", "HK"))

    assert state.fresh_install is False
    assert state.pending_markets == ()


def test_fresh_dispatch_ignores_marked_startup_daily_price_seed(
    db_session,
) -> None:
    from app.services.bootstrap_readiness_service import BootstrapReadinessService
    from app.tasks import runtime_bootstrap_tasks as module

    BootstrapReadinessService().mark_pre_bootstrap_seed_import(
        db_session,
        source="group_history_reconciliation",
    )
    db_session.commit()
    _seed_startup_daily_price_input(db_session)

    state = module._balanced_activation_state_at_dispatch(("US", "HK"))

    assert state.fresh_install is True
    assert state.pending_markets == ("US", "HK")


@pytest.mark.parametrize("fresh_install", [True, False])
def test_queue_bootstrap_captures_pristine_installation_once(
    monkeypatch,
    fresh_install,
):
    from app.tasks import runtime_bootstrap_tasks as module

    class _FakeAsyncResult:
        def __init__(self, task_id: str) -> None:
            self.id = task_id

    classifications = []
    store = _RecordingStore()
    queued_operations = []
    completion_payloads = []

    def _classify(_markets):
        classifications.append(fresh_install)
        return module.BalancedActivationDispatchState(
            fresh_install=fresh_install,
            pending_markets=("US", "HK") if fresh_install else (),
        )

    def _queue(market_plan, **kwargs):
        queued_operations.append([stage.operation for stage in market_plan.stages])
        completion_payloads.append(dict(kwargs["completion_kwargs"]))
        return _FakeAsyncResult(f"task-{market_plan.market.lower()}")

    monkeypatch.setattr(module, "_balanced_activation_state_at_dispatch", _classify)
    monkeypatch.setattr(module, "_bootstrap_dispatch_store", lambda: store)
    monkeypatch.setattr(module, "_queue_market_bootstrap_workflow", _queue)

    module.queue_local_runtime_bootstrap(
        primary_market="US",
        enabled_markets=("US", "HK"),
    )

    assert classifications == [fresh_install]
    assert store.manifests
    assert {record.fresh_install for record in store.manifests} == {fresh_install}
    expected_operation = (
        module.BootstrapOperation.BOOTSTRAP_BALANCED_MARKET_RS
        if fresh_install
        else module.BootstrapOperation.CALCULATE_MARKET_RS_SNAPSHOT
    )
    assert queued_operations
    assert all(expected_operation in operations for operations in queued_operations)
    if fresh_install:
        assert all(
            payload["expected_formula_version"] == BALANCED_RS_FORMULA_VERSION
            for payload in completion_payloads
        )
    else:
        assert all(
            "expected_formula_version" not in payload for payload in completion_payloads
        )


def test_partial_retry_activates_only_the_pending_market(monkeypatch) -> None:
    from app.tasks import runtime_bootstrap_tasks as module

    class _FakeAsyncResult:
        def __init__(self, task_id: str) -> None:
            self.id = task_id

    queued = {}
    store = _RecordingStore()

    monkeypatch.setattr(
        module,
        "_balanced_activation_state_at_dispatch",
        lambda _markets: module.BalancedActivationDispatchState(
            fresh_install=True,
            pending_markets=("HK",),
        ),
    )
    monkeypatch.setattr(module, "_bootstrap_dispatch_store", lambda: store)

    def _queue(market_plan, **kwargs):
        queued[market_plan.market] = {
            "operations": tuple(stage.operation for stage in market_plan.stages),
            "completion": dict(kwargs["completion_kwargs"]),
        }
        return _FakeAsyncResult(f"task-{market_plan.market.lower()}")

    monkeypatch.setattr(module, "_queue_market_bootstrap_workflow", _queue)

    module.queue_local_runtime_bootstrap(
        primary_market="US",
        enabled_markets=("US", "HK"),
    )

    assert (
        module.BootstrapOperation.CALCULATE_MARKET_RS_SNAPSHOT
        in queued["US"]["operations"]
    )
    assert "expected_formula_version" not in queued["US"]["completion"]
    assert (
        module.BootstrapOperation.BOOTSTRAP_BALANCED_MARKET_RS
        in queued["HK"]["operations"]
    )
    assert (
        queued["HK"]["completion"]["expected_formula_version"]
        == BALANCED_RS_FORMULA_VERSION
    )
    assert store.manifests[0].pending_balanced_activation_markets == ("HK",)


def test_fresh_bootstrap_signature_routes_activation_to_market_queue() -> None:
    from app.domain.bootstrap.plan import build_bootstrap_plan
    from app.tasks.runtime_bootstrap_tasks import _build_market_bootstrap_signatures

    market_plan = build_bootstrap_plan(
        primary_market="HK",
        enabled_markets=("HK",),
        balanced_activation_markets=("HK",),
    ).market_plans[0]

    signatures = _build_market_bootstrap_signatures(market_plan)
    activation = next(
        signature
        for signature in signatures
        if signature.task == "app.tasks.market_rs_tasks.bootstrap_balanced_market_rs"
    )

    assert activation.kwargs == {
        "market": "HK",
        "activity_lifecycle": "bootstrap",
    }
    assert activation.options["queue"] == "market_jobs_hk"


@pytest.mark.parametrize(
    ("completion_task_name", "task_kwargs", "expected_result"),
    [
        (
            "complete_local_runtime_bootstrap",
            {"primary_market": "US"},
            {
                "status": "failed",
                "primary_market": "US",
                "market": "US",
                "reason": "balanced market rs formula not active",
            },
        ),
        (
            "complete_background_market_bootstrap",
            {"market": "US"},
            {
                "status": "failed",
                "market": "US",
                "reason": "balanced market rs formula not active",
            },
        ),
    ],
)
def test_fresh_bootstrap_completion_fails_until_the_balanced_publication_commits(
    monkeypatch,
    completion_task_name,
    task_kwargs,
    expected_result,
):
    from app.services.bootstrap_readiness_service import (
        BootstrapReadiness,
        MarketBootstrapReadiness,
    )
    from app.tasks import runtime_bootstrap_tasks as module

    class _FakeSession:
        def close(self):
            pass

    class _FakeReadinessService:
        def evaluate(
            self,
            db,
            *,
            enabled_markets,
            bootstrap_started_at=None,
            expected_formula_versions=None,
        ):
            calls["expectations"] = expected_formula_versions
            return BootstrapReadiness(
                empty_system=False,
                market_results={
                    "US": MarketBootstrapReadiness(
                        market="US",
                        core_ready=True,
                        scan_ready=True,
                        rs_ready=False,
                    )
                },
            )

    calls = {}
    completions = []
    monkeypatch.setattr(module, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        module, "_is_current_bootstrap_dispatch", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        module,
        "_finish_bootstrap_market",
        lambda *_args, **kwargs: completions.append(kwargs["completion"]) or True,
    )
    monkeypatch.setattr(
        "app.services.bootstrap_readiness_service.BootstrapReadinessService",
        _FakeReadinessService,
    )
    monkeypatch.setattr(
        "app.services.runtime_preferences_service.get_runtime_preferences",
        lambda _db: SimpleNamespace(bootstrap_started_at=None),
    )

    task = getattr(module, completion_task_name)
    result = task.run(
        **task_kwargs,
        expected_formula_version=BALANCED_RS_FORMULA_VERSION,
        dispatch_id="dispatch-current",
    )

    assert result == expected_result
    assert calls["expectations"] == {
        "US": BALANCED_RS_FORMULA_VERSION,
    }
    assert completions[0].failure_stage_key == "market_rs"
    assert completions[0].failure_message == (
        "Balanced Market RS activation incomplete"
    )
    assert completions[0].primary is (
        completion_task_name == "complete_local_runtime_bootstrap"
    )
