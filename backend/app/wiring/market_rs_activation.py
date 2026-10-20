"""Operational adapters for guarded balanced Market RS activation."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from app.domain.relative_strength import (
    BALANCED_RS_FORMULA_VERSION,
    GroupSnapshotIdentity,
)
from app.services.market_rs_rollout_executor import (
    MarketRsActivationExecutionError,
    MarketRsActivationExecutor,
)
from app.services.market_rs_rollout_service import MarketRsRolloutService

logger = logging.getLogger(__name__)


def build_balanced_feature_snapshot(*, market: str, through_date: date) -> int:
    from app.interfaces.tasks.feature_store_tasks import build_daily_snapshot

    result = build_daily_snapshot.run(
        market=market,
        as_of_date_str=through_date.isoformat(),
        universe_name=f"market:{market}",
        publish_pointer_key=f"rollout_rs:{BALANCED_RS_FORMULA_VERSION}:{market}",
        static_daily_mode=True,
        ignore_runtime_market_gate=True,
        skip_if_published=False,
        rs_formula_version_override=BALANCED_RS_FORMULA_VERSION,
    )
    if not isinstance(result, dict) or result.get("status") != "published":
        raise MarketRsActivationExecutionError(
            f"Balanced Feature snapshot did not publish: {result}"
        )
    run_id = result.get("run_id")
    if run_id is None:
        raise MarketRsActivationExecutionError(
            "Balanced Feature snapshot returned no run ID"
        )
    return int(run_id)


def export_static_v3(
    *,
    market: str,
    feature_run_id: int,
    static_staging_dir: Path,
) -> None:
    from app.database import SessionLocal
    from app.services.static_site_export_service import StaticSiteExportService

    StaticSiteExportService(SessionLocal).export(
        static_staging_dir,
        clean=True,
        markets=(market,),
        feature_run_ids_by_market={market: feature_run_id},
    )


def publish_live_groups(identity: GroupSnapshotIdentity) -> None:
    from app.services.ui_snapshot_service import safe_publish_groups_bootstrap

    if identity.market != "US":
        logger.info(
            "Skipping US-only live Group bootstrap snapshot publication",
            extra={
                "market": identity.market,
                "formula_version": identity.formula_version,
                "as_of_date": identity.as_of_date.isoformat(),
            },
        )
        return
    safe_publish_groups_bootstrap(
        expected_formula_version=identity.formula_version,
        expected_through_date=identity.as_of_date,
    )


def create_market_rs_activation_executor(
    rollout_service: MarketRsRolloutService,
) -> MarketRsActivationExecutor:
    return MarketRsActivationExecutor(
        rollout_service=rollout_service,
        feature_snapshot_builder=build_balanced_feature_snapshot,
        static_exporter=export_static_v3,
        live_group_publisher=publish_live_groups,
    )


__all__ = [
    "build_balanced_feature_snapshot",
    "create_market_rs_activation_executor",
    "export_static_v3",
    "publish_live_groups",
]
