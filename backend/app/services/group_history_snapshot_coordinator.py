"""Group snapshot coordinator for history repair."""

from __future__ import annotations

from app.infra.db.repositories.market_rs_repo import MarketRsRunRepository
from app.services.canonical_group_ranking_service import CanonicalGroupRankingService
from app.services.group_rank_snapshot_coordinator import GroupRankSnapshotCoordinator
from app.services.group_rank_snapshot_reader import GroupRankSnapshotReader
from app.services.market_calendar_service import MarketCalendarService
from app.services.market_rs_inputs import MarketRsInputLoader
from app.services.market_rs_snapshot_service import MarketRsSnapshotService


def build_group_history_snapshot_coordinator(
    *,
    universe_resolver,
    calendar_service: MarketCalendarService | None = None,
    market_rs_repository: MarketRsRunRepository | None = None,
) -> GroupRankSnapshotCoordinator:
    calendar = calendar_service or MarketCalendarService()
    repository = market_rs_repository or MarketRsRunRepository()
    snapshot_service = MarketRsSnapshotService(
        input_loader=MarketRsInputLoader(
            point_in_time_universe=universe_resolver,
            market_calendar=calendar,
        ),
        repository=repository,
    )
    return GroupRankSnapshotCoordinator(
        reader=GroupRankSnapshotReader(),
        market_rs_snapshot_service=snapshot_service,
        canonical_group_service=CanonicalGroupRankingService(repository=repository),
    )


__all__ = ["build_group_history_snapshot_coordinator"]
