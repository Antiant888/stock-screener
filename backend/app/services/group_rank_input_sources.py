"""Source ports and default adapters for group-ranking inputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from sqlalchemy.orm import Session

from ..models.stock_universe import StockUniverse
from .stock_universe_service import StockUniverseService


class GroupRankUniverseSource(Protocol):
    def active_symbols(
        self,
        db: Session,
        market: str,
    ) -> frozenset[str]: ...

    def symbol_names(
        self,
        db: Session,
        symbols: Sequence[str],
    ) -> dict[str, str | None]: ...


@dataclass(frozen=True)
class StockUniverseGroupRankSource:
    service: StockUniverseService

    def active_symbols(
        self,
        db: Session,
        market: str,
    ) -> frozenset[str]:
        return frozenset(
            self.service.get_active_symbols(db, market=market)
        )

    def symbol_names(
        self,
        db: Session,
        symbols: Sequence[str],
    ) -> dict[str, str | None]:
        if not symbols:
            return {}
        return dict(
            db.query(
                StockUniverse.symbol,
                StockUniverse.name,
            )
            .filter(StockUniverse.symbol.in_(symbols))
            .all()
        )
