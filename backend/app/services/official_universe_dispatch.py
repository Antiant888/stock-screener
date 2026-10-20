"""Shared dispatch for official exchange universe snapshots."""

from __future__ import annotations

from typing import Any

from ..domain.markets.catalog import get_market_catalog
from .stock_universe_service import SNAPSHOT_INGEST_MARKETS


OFFICIAL_SOURCE_MARKETS = frozenset(
    get_market_catalog().market_codes_with_capability("official_universe")
)
if OFFICIAL_SOURCE_MARKETS != SNAPSHOT_INGEST_MARKETS:
    raise RuntimeError(
        "Official universe snapshot ingest markets must match Market Catalog "
        "official_universe capability; "
        f"missing={sorted(OFFICIAL_SOURCE_MARKETS - SNAPSHOT_INGEST_MARKETS)}, "
        f"extra={sorted(SNAPSHOT_INGEST_MARKETS - OFFICIAL_SOURCE_MARKETS)}"
    )


def ingest_official_market_snapshot(
    db: Any,
    stock_universe_service: Any,
    snapshot: Any,
) -> dict[str, Any]:
    """Route a normalized official snapshot into the market-keyed ingest method."""
    market = str(snapshot.market or "").strip().upper()
    if market not in OFFICIAL_SOURCE_MARKETS:
        raise ValueError(
            f"Unsupported official universe snapshot market {snapshot.market!r}"
        )

    return stock_universe_service.ingest_snapshot_rows(
        db,
        market=market,
        rows=snapshot.rows,
        source_name=snapshot.source_name,
        snapshot_id=snapshot.snapshot_id,
        snapshot_as_of=snapshot.snapshot_as_of,
        source_metadata=snapshot.source_metadata,
    )
