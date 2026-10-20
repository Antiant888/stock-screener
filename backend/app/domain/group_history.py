"""Shared identity contracts for Group ranking history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class GroupHistoryTarget:
    market: str
    through_date: date

    def __post_init__(self) -> None:
        market = str(self.market or "").strip().upper()
        if not market:
            raise ValueError("Group history target market is required")
        object.__setattr__(self, "market", market)


__all__ = ["GroupHistoryTarget"]
