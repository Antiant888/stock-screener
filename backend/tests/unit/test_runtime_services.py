from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.domain.markets import market_registry
from app.domain.relative_strength import LEGACY_RS_FORMULA_VERSION
from app.infra.db.models.relative_strength import MarketRsFormulaPointer
from app.wiring.bootstrap import (
    build_runtime_services,
    clear_runtime_services,
    initialize_process_runtime_services,
)


def test_runtime_services_reset_for_tests_clears_github_bootstrap_services():
    runtime = build_runtime_services()

    market_calendar = runtime.market_calendar_service()
    daily_price_bundle = runtime.daily_price_bundle_service()

    runtime.reset_for_tests()

    assert runtime._market_calendar_service is None
    assert runtime._daily_price_bundle_service is None

    assert runtime.market_calendar_service() is not market_calendar
    assert runtime.daily_price_bundle_service() is not daily_price_bundle


def test_process_runtime_provisions_formula_pointers_for_supported_markets():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[MarketRsFormulaPointer.__table__])
    Session = sessionmaker(bind=engine)

    try:
        initialize_process_runtime_services(session_factory=Session, force=True)
        with Session() as db:
            pointers = {
                row.market: row.formula_version
                for row in db.query(MarketRsFormulaPointer).all()
            }
    finally:
        clear_runtime_services()
        engine.dispose()

    assert pointers == {
        market: LEGACY_RS_FORMULA_VERSION
        for market in market_registry.supported_market_codes()
    }


def test_runtime_daily_price_bundle_service_keeps_price_cache_dependency_explicit():
    runtime = build_runtime_services()

    daily_price_bundle = runtime.daily_price_bundle_service()

    assert not hasattr(daily_price_bundle, "price_cache")


def test_runtime_services_reuses_rrg_service_for_process_lifetime(monkeypatch):
    runtime = build_runtime_services()

    monkeypatch.setattr(runtime, "group_rank_service", lambda: object())
    monkeypatch.setattr(runtime, "market_rs_run_repository", lambda: object())
    monkeypatch.setattr(
        "app.services.market_taxonomy_service.get_market_taxonomy_service",
        lambda: object(),
    )

    rrg_service = runtime.rrg_service()

    assert runtime.rrg_service() is rrg_service

    runtime.reset_for_tests()

    assert runtime.rrg_service() is not rrg_service


def test_runtime_services_reuses_and_resets_canonical_market_rs_dependencies():
    runtime = build_runtime_services()

    point_in_time = runtime.point_in_time_universe_service()
    input_loader = runtime.market_rs_input_loader()
    repository = runtime.market_rs_run_repository()
    snapshot_service = runtime.market_rs_snapshot_service()
    rollout_service = runtime.market_rs_rollout_service()
    activation_executor = runtime.market_rs_activation_executor()
    reader = runtime.market_rs_reader()

    assert runtime.point_in_time_universe_service() is point_in_time
    assert runtime.market_rs_input_loader() is input_loader
    assert runtime.market_rs_run_repository() is repository
    assert runtime.market_rs_snapshot_service() is snapshot_service
    assert runtime.market_rs_rollout_service() is rollout_service
    assert runtime.market_rs_activation_executor() is activation_executor
    assert activation_executor.rollout_service is rollout_service
    assert runtime.market_rs_reader() is reader

    runtime.reset_for_tests()

    assert runtime.point_in_time_universe_service() is not point_in_time
    assert runtime.market_rs_input_loader() is not input_loader
    assert runtime.market_rs_run_repository() is not repository
    assert runtime.market_rs_snapshot_service() is not snapshot_service
    assert runtime.market_rs_rollout_service() is not rollout_service
    assert runtime.market_rs_activation_executor() is not activation_executor
    assert runtime.market_rs_reader() is not reader


def test_runtime_orchestrator_uses_process_market_rs_reader():
    runtime = build_runtime_services()

    orchestrator = runtime.scan_orchestrator()

    assert orchestrator._market_rs_reader is runtime.market_rs_reader()
    assert orchestrator._data_provider._layer._defer_market_rs_resolution is True


def test_runtime_group_rank_service_uses_process_canonical_group_service():
    runtime = build_runtime_services()

    canonical = runtime.canonical_group_ranking_service()
    group_service = runtime.group_rank_service()

    assert runtime.canonical_group_ranking_service() is canonical
    assert group_service.canonical_group_service is canonical

    runtime.reset_for_tests()

    assert runtime._canonical_rs_runtime is None
    assert runtime.canonical_group_ranking_service() is not canonical


def test_lazy_builds_once_under_concurrency_and_allows_reentrant_factories():
    import threading
    import time

    runtime = build_runtime_services()
    calls = []

    def build_inner():
        calls.append("inner")
        return object()

    def build_outer():
        calls.append("outer")
        time.sleep(0.05)  # widen the race window
        # Factories call other accessors while holding the (reentrant) lock.
        return (runtime._lazy("_rate_limiter", build_inner),)

    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(runtime._lazy("_eps_rating_service", build_outer))
        )
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == ["outer", "inner"]
    assert all(result is results[0] for result in results)
