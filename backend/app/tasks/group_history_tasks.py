"""Celery orchestration for live Group history readiness and repair."""

from __future__ import annotations

from datetime import date

from celery import chain
from celery.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.database import SessionLocal
from app.domain.group_history import GroupHistoryTarget
from app.domain.markets import get_market_catalog
from app.services.bootstrap_readiness_service import BootstrapReadinessService
from app.services.bootstrap_publication_date import (
    resolve_bootstrap_publication_date,
)
from app.services.group_history_bootstrap_service import GroupHistoryBootstrapService
from app.services.group_history_execution_service import (
    GroupHistoryExecutionService,
)
from app.services.group_history_readiness_service import GroupHistoryReadinessService
from app.services.group_history_reconciliation import (
    GroupHistoryReconciliationRepository,
    GroupHistoryReconciliationStatus,
    GroupHistoryReservation,
)
from app.services.group_history_snapshot_coordinator import (
    build_group_history_snapshot_coordinator,
)
from app.services.group_history_universe import GroupHistoryUniverseResolver
from app.services.group_rankings_cache import bump_group_rankings_epoch
from app.services.market_activity_service import (
    mark_market_activity_completed,
    mark_market_activity_failed,
    mark_market_activity_started,
)
from app.services.point_in_time_universe_service import PointInTimeUniverseService
from app.services.rrg_history_provider import StoredGroupRankHistoryProvider
from app.services.runtime_preferences_service import get_runtime_preferences
from app.services.ui_snapshot_service import safe_publish_groups_bootstrap
from app.tasks.market_queues import (
    data_fetch_queue_for_market,
    market_jobs_queue_for_market,
    normalize_market,
)
from app.tasks.workload_coordination import serialized_market_workload
from app.wiring.bootstrap import get_market_calendar_service

_PRE_BOOTSTRAP_SEED_IMPORT_SOURCE = "group_history_reconciliation"


def _build_group_history_components():
    from app.infra.db.repositories.market_rs_repo import MarketRsRunRepository

    calendar = get_market_calendar_service()
    repository = MarketRsRunRepository()
    universe_resolver = GroupHistoryUniverseResolver(
        point_in_time_universe=PointInTimeUniverseService(market_calendar=calendar)
    )
    coordinator = build_group_history_snapshot_coordinator(
        universe_resolver=universe_resolver,
        calendar_service=calendar,
        market_rs_repository=repository,
    )
    history_provider = StoredGroupRankHistoryProvider()
    readiness = GroupHistoryReadinessService(
        calendar_service=calendar,
        snapshot_reader=coordinator.reader,
        rrg_history_provider=history_provider,
    )
    bootstrap = GroupHistoryBootstrapService(
        readiness_service=readiness,
        snapshot_coordinator=coordinator,
        universe_resolver=universe_resolver,
    )
    return readiness, bootstrap


def _build_group_history_bootstrap_service() -> GroupHistoryBootstrapService:
    return _build_group_history_components()[1]


def _build_group_history_execution_service() -> GroupHistoryExecutionService:
    return GroupHistoryExecutionService(
        bootstrap_service=_build_group_history_bootstrap_service(),
        reconciliation_repository=GroupHistoryReconciliationRepository(),
        bump_epoch=bump_group_rankings_epoch,
        publish_snapshot=lambda target: safe_publish_groups_bootstrap(
            expected_formula_version=target.formula_version,
            expected_through_date=target.through_date,
        ),
        mark_started=mark_market_activity_started,
        mark_completed=mark_market_activity_completed,
        mark_failed=mark_market_activity_failed,
        resolve_current_target=_resolve_current_group_history_target,
    )


def _resolve_current_group_history_target(db, *, market: str) -> GroupHistoryTarget:
    from app.infra.db.repositories.market_rs_repo import MarketRsRunRepository

    market_code = normalize_market(market)
    repository = MarketRsRunRepository()
    formula_version = repository.active_formula(
        db,
        market=market_code,
    )
    if not formula_version:
        raise RuntimeError(f"Active RS formula unavailable for {market_code}")
    requested_through_date = get_market_calendar_service().last_completed_trading_day(
        market_code
    )
    through_date = resolve_bootstrap_publication_date(
        db,
        market=market_code,
        requested_date=requested_through_date,
        formula_version=formula_version,
        repository=repository,
    ).selected_date
    return GroupHistoryTarget(
        market=market_code,
        formula_version=formula_version,
        through_date=through_date,
    )


def _evaluate_group_history_readiness(
    db,
    *,
    target: GroupHistoryTarget,
):
    return _build_group_history_components()[0].evaluate(db, target=target)


@celery_app.task(
    bind=True,
    name="app.tasks.group_history_tasks.ensure_group_history",
    soft_time_limit=7200,
)
@serialized_market_workload("ensure_group_history")
def ensure_group_history(
    self,
    market: str,
    activity_lifecycle: str | None = None,
    strict: bool = True,
) -> dict:
    market_code = normalize_market(market)
    task_name = getattr(self, "name", "ensure_group_history")
    task_id = getattr(getattr(self, "request", None), "id", None)
    db = SessionLocal()
    try:
        target = _resolve_current_group_history_target(db, market=market_code)
        return _build_group_history_execution_service().execute_bootstrap(
            db,
            target=target,
            task_name=task_name,
            task_id=task_id,
            strict=strict,
            activity_lifecycle=activity_lifecycle,
        )
    finally:
        db.close()


@celery_app.task(
    bind=True,
    name="app.tasks.group_history_tasks.repair_group_history_reconciliation",
    soft_time_limit=7200,
)
@serialized_market_workload("repair_group_history_reconciliation")
def repair_group_history_reconciliation(
    self,
    *,
    market: str,
    formula_version: str,
    through_date: str,
    reservation_id: str,
) -> dict:
    target = GroupHistoryTarget(
        market=market,
        formula_version=formula_version,
        through_date=date.fromisoformat(through_date),
    )
    task_name = getattr(self, "name", "repair_group_history_reconciliation")
    task_id = getattr(getattr(self, "request", None), "id", None)
    db = SessionLocal()
    try:
        return _build_group_history_execution_service().execute_reconciliation(
            db,
            reservation=GroupHistoryReservation(
                target=target,
                reservation_id=reservation_id,
            ),
            task_name=task_name,
            task_id=task_id,
        )
    finally:
        db.close()


def _dispatch_group_history_reconciliation(
    *,
    market: str,
    formula_version: str,
    through_date: date,
    reservation_id: str,
) -> str:
    from app.tasks.cache_tasks import smart_refresh_cache

    workflow = chain(
        smart_refresh_cache.si(
            mode="bootstrap",
            market=market,
            activity_lifecycle="group_history_reconciliation",
            ensure_group_history=True,
        ).set(queue=data_fetch_queue_for_market(market)),
        _group_history_repair_signature(
            market=market,
            formula_version=formula_version,
            through_date=through_date,
            reservation_id=reservation_id,
        ),
    )
    return workflow.apply_async(
        link_error=_group_history_failure_signature(
            market=market,
            formula_version=formula_version,
            through_date=through_date,
            reservation_id=reservation_id,
        )
    ).id


def _dispatch_group_history_finalization(
    *,
    market: str,
    formula_version: str,
    through_date: date,
    reservation_id: str,
) -> str:
    return (
        _group_history_repair_signature(
            market=market,
            formula_version=formula_version,
            through_date=through_date,
            reservation_id=reservation_id,
        )
        .apply_async(
            link_error=_group_history_failure_signature(
                market=market,
                formula_version=formula_version,
                through_date=through_date,
                reservation_id=reservation_id,
            )
        )
        .id
    )


def _mark_pre_bootstrap_seed_import(db) -> bool:
    service = BootstrapReadinessService()
    had_valid_marker = service.has_pre_bootstrap_seed_import_marker(db)
    marked = service.mark_pre_bootstrap_seed_import(
        db,
        source=_PRE_BOOTSTRAP_SEED_IMPORT_SOURCE,
    )
    if marked:
        db.commit()
    return marked and not had_valid_marker


def _clear_pre_bootstrap_seed_import(db) -> None:
    cleared = BootstrapReadinessService().clear_pre_bootstrap_seed_import_marker(
        db,
        source=_PRE_BOOTSTRAP_SEED_IMPORT_SOURCE,
    )
    if cleared:
        db.commit()


def _group_history_repair_signature(
    *,
    market: str,
    formula_version: str,
    through_date: date,
    reservation_id: str,
):
    return repair_group_history_reconciliation.si(
        market=market,
        formula_version=formula_version,
        through_date=through_date.isoformat(),
        reservation_id=reservation_id,
    ).set(queue=market_jobs_queue_for_market(market))


def _group_history_failure_signature(
    *,
    market: str,
    formula_version: str,
    through_date: date,
    reservation_id: str,
):
    return fail_group_history_reconciliation.s(
        market=market,
        formula_version=formula_version,
        through_date=through_date.isoformat(),
        reservation_id=reservation_id,
    ).set(queue="celery")


@celery_app.task(
    name="app.tasks.group_history_tasks.discover_group_history_reconciliation",
    queue="celery",
)
def discover_group_history_reconciliation() -> dict[str, str]:
    """Reserve and queue nonblocking repairs for enabled live markets."""
    db = SessionLocal()
    try:
        preferences = get_runtime_preferences(db)
        if preferences.bootstrap_state == "running":
            return {
                market: "bootstrap_running" for market in preferences.enabled_markets
            }

        repository = GroupHistoryReconciliationRepository()
        outcomes: dict[str, str] = {}
        for market in preferences.enabled_markets:
            market_code = normalize_market(market)
            if not get_market_catalog().get(market_code).capabilities.group_rankings:
                outcomes[market_code] = "skipped"
                continue
            try:
                target = _resolve_current_group_history_target(
                    db,
                    market=market_code,
                )
            except SoftTimeLimitExceeded:
                db.rollback()
                raise
            except Exception as exc:
                db.rollback()
                outcomes[market_code] = f"target_failed:{type(exc).__name__}"
                continue
            readiness = None
            try:
                existing = repository.load(db, market=target.market)
            except SoftTimeLimitExceeded:
                db.rollback()
                raise
            except Exception as exc:
                db.rollback()
                outcomes[market_code] = f"marker_failed:{type(exc).__name__}"
                continue
            if (
                existing is not None
                and existing.target == target
                and existing.status is GroupHistoryReconciliationStatus.READY
            ):
                try:
                    readiness = _evaluate_group_history_readiness(db, target=target)
                except SoftTimeLimitExceeded:
                    db.rollback()
                    raise
                except Exception as exc:
                    db.rollback()
                    outcomes[market_code] = f"readiness_failed:{type(exc).__name__}"
                    continue
                if readiness.ready:
                    outcomes[market_code] = "ready"
                    continue
            try:
                reservation = repository.reserve(db, target=target)
            except SoftTimeLimitExceeded:
                db.rollback()
                raise
            except Exception as exc:
                db.rollback()
                outcomes[market_code] = f"reserve_failed:{type(exc).__name__}"
                continue
            if reservation is None:
                outcomes[market_code] = "already_queued"
                continue
            if readiness is None:
                try:
                    readiness = _evaluate_group_history_readiness(db, target=target)
                except SoftTimeLimitExceeded:
                    db.rollback()
                    raise
                except Exception as exc:
                    db.rollback()
                    repository.transition(
                        db,
                        reservation=reservation,
                        expected_statuses={
                            GroupHistoryReconciliationStatus.DISPATCHING
                        },
                        status=GroupHistoryReconciliationStatus.INCOMPLETE,
                        error=str(exc),
                    )
                    outcomes[market_code] = f"readiness_failed:{type(exc).__name__}"
                    continue
            dispatch = (
                _dispatch_group_history_finalization
                if readiness.ready
                else _dispatch_group_history_reconciliation
            )
            owns_pre_bootstrap_seed_marker = False
            try:
                if not readiness.ready:
                    owns_pre_bootstrap_seed_marker = (
                        _mark_pre_bootstrap_seed_import(db)
                    )
                dispatch(
                    market=target.market,
                    formula_version=target.formula_version,
                    through_date=target.through_date,
                    reservation_id=reservation.reservation_id,
                )
            except SoftTimeLimitExceeded:
                db.rollback()
                raise
            except Exception as exc:
                db.rollback()
                if owns_pre_bootstrap_seed_marker:
                    try:
                        _clear_pre_bootstrap_seed_import(db)
                    except SoftTimeLimitExceeded:
                        raise
                    except Exception:
                        db.rollback()
                repository.transition(
                    db,
                    reservation=reservation,
                    expected_statuses={
                        GroupHistoryReconciliationStatus.DISPATCHING
                    },
                    status=GroupHistoryReconciliationStatus.INCOMPLETE,
                    counts=readiness.as_dict(),
                    error=str(exc),
                )
                outcomes[market_code] = "dispatch_failed"
                continue
            try:
                # Repair may claim DISPATCHING before this promotion CAS runs.
                repository.transition(
                    db,
                    reservation=reservation,
                    expected_statuses={
                        GroupHistoryReconciliationStatus.DISPATCHING
                    },
                    status=GroupHistoryReconciliationStatus.QUEUED,
                )
            except SoftTimeLimitExceeded:
                db.rollback()
                raise
            except Exception:
                db.rollback()
                outcomes[market_code] = "queued_unconfirmed"
                continue
            outcomes[market_code] = (
                "finalization_queued" if readiness.ready else "queued"
            )
        return outcomes
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.group_history_tasks.fail_group_history_reconciliation",
    queue="celery",
)
def fail_group_history_reconciliation(
    *_celery_errback_args,
    market: str,
    formula_version: str,
    through_date: str,
    reservation_id: str,
) -> dict:
    target = GroupHistoryTarget(
        market=market,
        formula_version=formula_version,
        through_date=date.fromisoformat(through_date),
    )
    db = SessionLocal()
    try:
        reservation = GroupHistoryReservation(
            target=target,
            reservation_id=reservation_id,
        )
        GroupHistoryReconciliationRepository().transition(
            db,
            reservation=reservation,
            expected_statuses={
                GroupHistoryReconciliationStatus.DISPATCHING,
                GroupHistoryReconciliationStatus.QUEUED,
                GroupHistoryReconciliationStatus.REPAIRING,
                GroupHistoryReconciliationStatus.FINALIZING,
            },
            status=GroupHistoryReconciliationStatus.FAILED,
            error="Group history reconciliation task failed",
        )
        return {
            "status": "failed",
            "market": target.market,
            "formula_version": target.formula_version,
            "through_date": target.through_date.isoformat(),
        }
    finally:
        db.close()


__all__ = [
    "discover_group_history_reconciliation",
    "ensure_group_history",
    "fail_group_history_reconciliation",
    "repair_group_history_reconciliation",
]
