"""Transactional persistence operations for canonical Market RS runs."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterator

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session, selectinload

from app.domain.relative_strength import (
    HORIZONS,
    STOCK_RS_RATING_ATTR_BY_HORIZON,
    StockRsPlacement,
    StockRsScore,
)
from app.infra.db.models.relative_strength import (
    MarketRsRun,
    StockRsPlacementSnapshot,
    StockRsSnapshot,
)
from app.models.industry import IBDGroupRank


@dataclass(frozen=True)
class StockRsSnapshotRecord:
    symbol: str
    overall_rs: int
    rs_1d: int
    rs_1w: int
    rs_1m: int
    rs_3m: int
    rs_6m: int
    rs_9m: int
    rs_12m: int
    weighted_composite: float
    excess_return_1d: float
    excess_return_1w: float
    excess_return_1m: float
    excess_return_3m: float
    excess_return_6m: float
    excess_return_9m: float
    excess_return_12m: float


def placement_from_row(row) -> StockRsPlacement:
    """Rebuild a domain placement from a placement row, re-checking its shape."""
    ratings = {}
    for horizon in HORIZONS:
        rating = getattr(row, STOCK_RS_RATING_ATTR_BY_HORIZON[horizon])
        if rating is not None:
            ratings[horizon] = int(rating)
    return StockRsPlacement(
        row.symbol,
        ratings,
        {horizon: float(getattr(row, f"excess_return_{horizon}")) for horizon in ratings},
    )


class MarketRsRunRepository:
    @staticmethod
    def _run_query(
        db: Session,
        *,
        market: str,
        as_of_date: date,
        formula_version: str,
    ):
        return db.query(MarketRsRun).filter(
            MarketRsRun.market == market.upper(),
            MarketRsRun.as_of_date == as_of_date,
            MarketRsRun.formula_version == formula_version,
        )

    def start_or_restart(
        self,
        db: Session,
        *,
        market: str,
        as_of_date: date,
        formula_version: str,
        benchmark_symbol: str,
        benchmark_as_of_date: date,
        universe_hash: str,
        expected_symbol_count: int,
        rebuild_completed: bool = False,
    ) -> MarketRsRun:
        normalized = market.upper()
        run = (
            self._run_query(
                db,
                market=normalized,
                as_of_date=as_of_date,
                formula_version=formula_version,
            )
            .with_for_update()
            .one_or_none()
        )
        if run is None:
            try:
                with db.begin_nested():
                    run = MarketRsRun(
                        market=normalized,
                        as_of_date=as_of_date,
                        formula_version=formula_version,
                        status="running",
                        benchmark_symbol=benchmark_symbol,
                        benchmark_as_of_date=benchmark_as_of_date,
                        universe_hash=universe_hash,
                        expected_symbol_count=expected_symbol_count,
                        eligible_symbol_count=0,
                        excluded_symbol_count=0,
                        diagnostics_json={},
                    )
                    db.add(run)
                    db.flush()
            except IntegrityError:
                run = (
                    self._run_query(
                        db,
                        market=normalized,
                        as_of_date=as_of_date,
                        formula_version=formula_version,
                    )
                    .with_for_update()
                    .one()
                )

        if run.status == "completed":
            if not rebuild_completed:
                return run
            old_run = run
            temporary_formula = f"__rebuild__{old_run.id}__{formula_version}"[:64]
            old_run.formula_version = temporary_formula
            db.flush()
            run = MarketRsRun(
                market=normalized,
                as_of_date=as_of_date,
                formula_version=formula_version,
                status="running",
                benchmark_symbol=benchmark_symbol,
                benchmark_as_of_date=benchmark_as_of_date,
                universe_hash=universe_hash,
                expected_symbol_count=expected_symbol_count,
                eligible_symbol_count=0,
                excluded_symbol_count=0,
                diagnostics_json={},
            )
            db.add(run)
            db.flush()
            db.query(IBDGroupRank).filter(
                IBDGroupRank.market_rs_run_id == old_run.id
            ).delete(synchronize_session=False)
            db.delete(old_run)
            db.flush()

        run.rows.clear()
        run.placements.clear()
        run.placed_symbol_count = None
        run.status = "running"
        run.benchmark_symbol = benchmark_symbol
        run.benchmark_as_of_date = benchmark_as_of_date
        run.universe_hash = universe_hash
        run.expected_symbol_count = expected_symbol_count
        run.eligible_symbol_count = 0
        run.excluded_symbol_count = 0
        run.diagnostics_json = {}
        run.completed_at = None
        db.flush()
        return run

    def replace_rows(
        self,
        db: Session,
        run: MarketRsRun,
        scores: Mapping[str, StockRsScore],
        *,
        placements: Mapping[str, StockRsPlacement] | None = None,
    ) -> None:
        """Write the eligible rows and, when given, the young-stock placements.
        ``placements=None`` leaves ``placed_symbol_count`` NULL, meaning placement
        was not computed for this run."""
        if run.status == "completed":
            raise ValueError("completed Market RS runs are immutable")
        run.rows.clear()
        run.rows.extend(
            StockRsSnapshot(
                symbol=symbol,
                overall_rs=score.overall_rs,
                rs_1d=score.rs_1d,
                rs_1w=score.rs_1w,
                rs_1m=score.rs_1m,
                rs_3m=score.rs_3m,
                rs_6m=score.rs_6m,
                rs_9m=score.rs_9m,
                rs_12m=score.rs_12m,
                weighted_composite=score.weighted_composite,
                excess_return_1d=score.excess_return_1d,
                excess_return_1w=score.excess_return_1w,
                excess_return_1m=score.excess_return_1m,
                excess_return_3m=score.excess_return_3m,
                excess_return_6m=score.excess_return_6m,
                excess_return_9m=score.excess_return_9m,
                excess_return_12m=score.excess_return_12m,
            )
            for symbol, score in sorted(scores.items())
        )
        run.eligible_symbol_count = len(scores)
        run.placements.clear()
        if placements is not None:
            run.placements.extend(
                StockRsPlacementSnapshot(
                    symbol=symbol,
                    **{
                        STOCK_RS_RATING_ATTR_BY_HORIZON[horizon]: placement.ratings.get(horizon)
                        for horizon in HORIZONS
                    },
                    **{
                        f"excess_return_{horizon}": placement.excess_returns.get(horizon)
                        for horizon in HORIZONS
                    },
                )
                for symbol, placement in sorted(placements.items())
            )
        run.placed_symbol_count = None if placements is None else len(placements)
        db.flush()

    @staticmethod
    def _flush(run: MarketRsRun) -> None:
        session = object_session(run)
        if session is None:
            raise ValueError("Market RS run is not attached to a session")
        session.flush()

    def mark_completed(
        self,
        run: MarketRsRun,
        *,
        excluded_symbol_count: int,
        diagnostics: dict[str, object],
    ) -> MarketRsRun:
        if len(run.rows) != run.eligible_symbol_count:
            raise ValueError("eligible symbol count does not match persisted rows")
        if run.expected_symbol_count != (
            run.eligible_symbol_count + excluded_symbol_count
        ):
            raise ValueError("expected symbol count does not reconcile")
        for row in run.rows:
            ratings = (
                row.overall_rs,
                row.rs_1d,
                row.rs_1w,
                row.rs_1m,
                row.rs_3m,
                row.rs_6m,
                row.rs_9m,
                row.rs_12m,
            )
            if any(value < 1 or value > 99 for value in ratings):
                raise ValueError(f"{row.symbol} has an out-of-range RS rating")
            if not math.isfinite(float(row.weighted_composite)):
                raise ValueError(f"{row.symbol} has a non-finite RS composite")
        if run.placed_symbol_count is not None:
            if len(run.placements) != run.placed_symbol_count:
                raise ValueError("placed symbol count does not match persisted placements")
            if run.placed_symbol_count > excluded_symbol_count:
                raise ValueError("placed symbols exceed excluded symbols")
            eligible = {row.symbol for row in run.rows}
            for row in run.placements:
                if row.symbol in eligible:
                    raise ValueError(f"{row.symbol} is both eligible and placed")
                placement_from_row(row)
        run.excluded_symbol_count = excluded_symbol_count
        run.diagnostics_json = diagnostics
        run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)
        self._flush(run)
        return run

    def mark_failed(
        self,
        run: MarketRsRun,
        *,
        diagnostics: dict[str, object],
    ) -> MarketRsRun:
        if run.status == "completed":
            raise ValueError("completed Market RS runs cannot be marked failed")
        run.status = "failed"
        run.diagnostics_json = diagnostics
        run.completed_at = datetime.now(timezone.utc)
        self._flush(run)
        return run

    def get_completed_exact(
        self,
        db: Session,
        *,
        market: str,
        as_of_date: date,
        formula_version: str,
        load_rows: bool = True,
    ) -> MarketRsRun | None:
        query = self._run_query(
            db,
            market=market,
            as_of_date=as_of_date,
            formula_version=formula_version,
        )
        if load_rows:
            query = query.options(selectinload(MarketRsRun.rows))
        return query.filter(MarketRsRun.status == "completed").one_or_none()

    def iter_stock_rows_for_run(
        self,
        db: Session,
        *,
        run_id: int,
        batch_size: int = 1000,
    ) -> Iterator[StockRsSnapshotRecord]:
        query = (
            db.query(
                StockRsSnapshot.symbol,
                StockRsSnapshot.overall_rs,
                StockRsSnapshot.rs_1d,
                StockRsSnapshot.rs_1w,
                StockRsSnapshot.rs_1m,
                StockRsSnapshot.rs_3m,
                StockRsSnapshot.rs_6m,
                StockRsSnapshot.rs_9m,
                StockRsSnapshot.rs_12m,
                StockRsSnapshot.weighted_composite,
                StockRsSnapshot.excess_return_1d,
                StockRsSnapshot.excess_return_1w,
                StockRsSnapshot.excess_return_1m,
                StockRsSnapshot.excess_return_3m,
                StockRsSnapshot.excess_return_6m,
                StockRsSnapshot.excess_return_9m,
                StockRsSnapshot.excess_return_12m,
            )
            .filter(StockRsSnapshot.run_id == run_id)
            .order_by(StockRsSnapshot.symbol)
            .yield_per(max(1, int(batch_size)))
        )
        for row in query:
            yield StockRsSnapshotRecord(
                symbol=row.symbol,
                overall_rs=int(row.overall_rs),
                rs_1d=int(row.rs_1d),
                rs_1w=int(row.rs_1w),
                rs_1m=int(row.rs_1m),
                rs_3m=int(row.rs_3m),
                rs_6m=int(row.rs_6m),
                rs_9m=int(row.rs_9m),
                rs_12m=int(row.rs_12m),
                weighted_composite=float(row.weighted_composite),
                excess_return_1d=float(row.excess_return_1d),
                excess_return_1w=float(row.excess_return_1w),
                excess_return_1m=float(row.excess_return_1m),
                excess_return_3m=float(row.excess_return_3m),
                excess_return_6m=float(row.excess_return_6m),
                excess_return_9m=float(row.excess_return_9m),
                excess_return_12m=float(row.excess_return_12m),
            )

    def iter_placements_for_run(
        self,
        db: Session,
        *,
        run_id: int,
        batch_size: int = 1000,
    ) -> Iterator[StockRsPlacement]:
        query = (
            db.query(StockRsPlacementSnapshot)
            .filter(StockRsPlacementSnapshot.run_id == run_id)
            .order_by(StockRsPlacementSnapshot.symbol)
            .yield_per(max(1, int(batch_size)))
        )
        for row in query:
            yield placement_from_row(row)

    def get_latest_completed(
        self,
        db: Session,
        *,
        market: str,
        formula_version: str,
        through_date: date | None = None,
        load_rows: bool = True,
    ) -> MarketRsRun | None:
        query = db.query(MarketRsRun).filter(
            MarketRsRun.market == market.upper(),
            MarketRsRun.formula_version == formula_version,
            MarketRsRun.status == "completed",
        )
        if load_rows:
            query = query.options(selectinload(MarketRsRun.rows))
        if through_date is not None:
            query = query.filter(MarketRsRun.as_of_date <= through_date)
        return query.order_by(
            MarketRsRun.as_of_date.desc(), MarketRsRun.id.desc()
        ).first()

    def list_completed_runs(
        self,
        db: Session,
        *,
        market: str,
        formula_version: str,
        start_date: date,
        through_date: date,
    ) -> tuple[MarketRsRun, ...]:
        return tuple(
            db.query(MarketRsRun)
            .filter(
                MarketRsRun.market == market.upper(),
                MarketRsRun.formula_version == formula_version,
                MarketRsRun.status == "completed",
                MarketRsRun.as_of_date >= start_date,
                MarketRsRun.as_of_date <= through_date,
            )
            .order_by(MarketRsRun.as_of_date.desc(), MarketRsRun.id.desc())
            .all()
        )
