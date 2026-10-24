"""Daily scans of flagged watchlists, answered from their market's published snapshot."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.domain.common.errors import ValidationError
from app.domain.scanning.defaults import get_default_scan_profile
from app.domain.scanning.models import ScanWarningCode, SymbolsNotInSnapshotWarning
from app.infra.db.models.feature_store import StockFeatureDaily
from app.infra.db.repositories.universe_repo import SqlUniverseRepository
from app.infra.db.uow import SqlUnitOfWork
from app.models.scan_result import SCAN_TRIGGER_SOURCE_WATCHLIST
from app.models.user_watchlist import UserWatchlist
from app.schemas.universe import UniverseDefinition, UniverseType
from app.services.scan_execution import cleanup_old_scans
from app.services.ui_snapshot_service import safe_publish_scan_variant
from app.services.watchlist_symbols import watchlist_symbols
from app.use_cases.scanning.create_scan import (
    CreateScanCommand,
    CreateScanUseCase,
    SnapshotUnavailableError,
)
from app.use_cases.scanning.published_snapshot import DATA_MODE_LAST_PUBLISHED

logger = logging.getLogger(__name__)


class WatchlistScanStatus(str, Enum):
    CREATED = "created"
    DUPLICATE = "duplicate"
    NO_SYMBOLS_IN_MARKET = "no_symbols_in_market"
    SNAPSHOT_UNAVAILABLE = "snapshot_unavailable"
    FAILED = "failed"


@dataclass(frozen=True)
class WatchlistScanOutcome:
    watchlist_id: int
    status: WatchlistScanStatus
    scan_id: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "watchlist_id": self.watchlist_id,
            "status": self.status.value,
            "scan_id": self.scan_id,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class WatchlistInMarket:
    watchlist_id: int
    name: str
    symbols: list[str]


def flagged_watchlists_in_market(db: Session, market: str) -> list[WatchlistInMarket]:
    """Each flagged watchlist with its symbols listed in ``market``, in display order."""
    flagged = (
        db.query(UserWatchlist.id, UserWatchlist.name)
        .filter(UserWatchlist.include_in_daily_scan.is_(True))
        .order_by(UserWatchlist.position, UserWatchlist.id)
        .all()
    )
    symbols_by_id = {watchlist_id: watchlist_symbols(db, watchlist_id) or [] for watchlist_id, _ in flagged}
    markets = SqlUniverseRepository(db).resolve_markets(
        [symbol for symbols in symbols_by_id.values() for symbol in symbols]
    )
    return [
        WatchlistInMarket(
            watchlist_id,
            name,
            [symbol for symbol in symbols_by_id[watchlist_id] if markets.get(symbol.upper()) == market],
        )
        for watchlist_id, name in flagged
    ]


def _symbols_with_rows(db: Session, run_id: int, symbols: list[str]) -> set[str]:
    if not symbols:
        return set()
    return {
        symbol
        for (symbol,) in db.query(StockFeatureDaily.symbol).filter(
            StockFeatureDaily.run_id == run_id,
            StockFeatureDaily.symbol.in_({symbol.upper() for symbol in symbols}),
        )
    }


def scan_flagged_watchlists(
    use_case: CreateScanUseCase, *, market: str, run_id: int, session_date: date
) -> list[WatchlistScanOutcome]:
    """Create one completed scan per flagged watchlist with symbols in ``market``.

    Each scan answers from ``run_id``, the published run the market's snapshot
    guard passed for ``session_date``, and covers the watchlist's symbols that
    run has rows for; it warns about the rest. The scans use the default
    profile the snapshot is built with, so they reuse its stored scores and
    never compute or fetch prices. One watchlist failing records its outcome
    and the rest still run.
    """
    db = SessionLocal()
    try:
        watchlists = flagged_watchlists_in_market(db, market)
        covered = _symbols_with_rows(db, run_id, [symbol for w in watchlists for symbol in w.symbols])
    finally:
        db.close()
    profile = get_default_scan_profile(market)
    return [
        _scan_watchlist(
            use_case, watchlist, covered,
            market=market, run_id=run_id, session_date=session_date, profile=profile,
        )
        for watchlist in watchlists
    ]


def _scan_watchlist(
    use_case: CreateScanUseCase,
    watchlist: WatchlistInMarket,
    covered: set[str],
    *,
    market: str,
    run_id: int,
    session_date: date,
    profile: dict[str, Any],
) -> WatchlistScanOutcome:
    watchlist_id = watchlist.watchlist_id
    if not watchlist.symbols:
        return WatchlistScanOutcome(watchlist_id, WatchlistScanStatus.NO_SYMBOLS_IN_MARKET)
    symbols = [symbol for symbol in watchlist.symbols if symbol.upper() in covered]
    uncovered = tuple(symbol for symbol in watchlist.symbols if symbol.upper() not in covered)
    if not symbols:
        logger.warning("%s snapshot run %s has no row for any symbol of watchlist %s", market, run_id, watchlist_id)
        return WatchlistScanOutcome(
            watchlist_id,
            WatchlistScanStatus.SNAPSHOT_UNAVAILABLE,
            reason=ScanWarningCode.SYMBOLS_NOT_IN_SNAPSHOT.value,
        )
    universe_key = f"watchlist:{watchlist_id}:{market}"
    try:
        universe = UniverseDefinition(type=UniverseType.CUSTOM, symbols=symbols)
        projection = universe.storage_projection()
        result = use_case.execute(SqlUnitOfWork(SessionLocal), CreateScanCommand(
            universe_def=universe,
            universe_label=projection.label,
            # Retention keeps the newest daily scans per watchlist and market,
            # whatever the membership was on each day.
            universe_key=universe_key,
            universe_type=projection.type,
            # Every symbol is in this market, so the scan lists under it.
            universe_market=market,
            universe_symbols=projection.symbols,
            screeners=profile["screeners"],
            composite_method=profile["composite_method"],
            criteria=profile["criteria"],
            idempotency_key=f"watchlist-daily:{watchlist_id}:{market}:{session_date.isoformat()}",
            trigger_source=SCAN_TRIGGER_SOURCE_WATCHLIST,
            data_mode=DATA_MODE_LAST_PUBLISHED,
            # The idempotency key names this run's session, so no other run may answer.
            published_run_id=run_id,
            metadata={"watchlist": {"id": watchlist_id, "name": watchlist.name, "market": market}},
            warnings=(SymbolsNotInSnapshotWarning.for_dropped(uncovered),) if uncovered else (),
        ))
    except SnapshotUnavailableError as exc:
        logger.warning(
            "No published %s snapshot answers watchlist %s: %s", market, watchlist_id, exc.ineligible.reason
        )
        return WatchlistScanOutcome(
            watchlist_id, WatchlistScanStatus.SNAPSHOT_UNAVAILABLE, reason=exc.ineligible.reason
        )
    except ValidationError as exc:
        logger.warning("Daily %s scan of watchlist %s rejected: %s", market, watchlist_id, exc)
        return WatchlistScanOutcome(watchlist_id, WatchlistScanStatus.FAILED, reason=str(exc))
    except Exception as exc:
        logger.warning("Daily %s scan of watchlist %s failed", market, watchlist_id, exc_info=True)
        return WatchlistScanOutcome(watchlist_id, WatchlistScanStatus.FAILED, reason=str(exc))

    if result.is_duplicate:
        return WatchlistScanOutcome(watchlist_id, WatchlistScanStatus.DUPLICATE, scan_id=result.scan_id)
    db = SessionLocal()
    try:
        cleanup_old_scans(db, universe_key)
    finally:
        db.close()
    # Only its own variant. The scan is never its market's latest, and the
    # market's history list refetches once its published copy goes stale.
    safe_publish_scan_variant(result.scan_id)
    return WatchlistScanOutcome(watchlist_id, WatchlistScanStatus.CREATED, scan_id=result.scan_id)
