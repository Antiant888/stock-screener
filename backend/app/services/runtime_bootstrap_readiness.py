"""Readiness evaluation for one completed runtime bootstrap workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.tasks.market_queues import normalize_market

if TYPE_CHECKING:
    from app.services.bootstrap_readiness_service import MarketBootstrapReadiness


@dataclass(frozen=True)
class ReadinessFailure:
    stage_key: str
    activity_message: str
    result_reason: str


BALANCED_PUBLICATION_MISSING = ReadinessFailure(
    stage_key="market_rs",
    activity_message="Balanced Market RS publication not committed",
    result_reason="no committed balanced market rs publication",
)


@dataclass(frozen=True)
class MarketReadinessCompletion:
    market: str
    ready: bool
    failure: ReadinessFailure | None = None


def readiness_failure(
    market_result: MarketBootstrapReadiness | None,
) -> ReadinessFailure:
    if market_result is None or not market_result.core_ready:
        return ReadinessFailure(
            stage_key="core",
            activity_message="Bootstrap core data incomplete",
            result_reason="missing core market data",
        )
    if not market_result.scan_ready:
        return ReadinessFailure(
            stage_key="scan",
            activity_message="Bootstrap scan did not publish",
            result_reason="missing published auto scan",
        )
    return BALANCED_PUBLICATION_MISSING


def evaluate_market_readiness(
    db: Session,
    *,
    market: str,
    bootstrap_started_at: datetime | None = None,
    requires_balanced_publication: bool = False,
) -> MarketReadinessCompletion:
    from app.services.bootstrap_readiness_service import BootstrapReadinessService

    market_code = normalize_market(market)
    readiness = BootstrapReadinessService().evaluate(
        db,
        enabled_markets=[market_code],
        bootstrap_started_at=bootstrap_started_at,
        balanced_publication_markets=(
            [market_code] if requires_balanced_publication else ()
        ),
    )
    market_result = readiness.market_results.get(market_code)
    result_market = market_result.market if market_result else market_code
    if market_result and market_result.ready:
        return MarketReadinessCompletion(market=result_market, ready=True)
    return MarketReadinessCompletion(
        market=result_market,
        ready=False,
        failure=readiness_failure(market_result),
    )


__all__ = [
    "BALANCED_PUBLICATION_MISSING",
    "MarketReadinessCompletion",
    "ReadinessFailure",
    "evaluate_market_readiness",
    "readiness_failure",
]
