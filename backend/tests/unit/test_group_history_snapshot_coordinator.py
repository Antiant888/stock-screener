from __future__ import annotations

from types import SimpleNamespace


def test_group_history_coordinator_uses_fallback_resolver():
    from app.services.group_history_snapshot_coordinator import (
        build_group_history_snapshot_coordinator,
    )

    resolver = object()

    coordinator = build_group_history_snapshot_coordinator(
        universe_resolver=resolver,
        calendar_service=SimpleNamespace(),
        market_rs_repository=SimpleNamespace(),
    )

    assert (
        coordinator.market_rs_snapshot_service.input_loader._point_in_time_universe
        is resolver
    )
