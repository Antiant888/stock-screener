from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base
from app.domain.relative_strength import HORIZONS, STOCK_RS_RATING_ATTR_BY_HORIZON


def _placement_check() -> str:
    clauses = []
    for index, horizon in enumerate(HORIZONS):
        rating = STOCK_RS_RATING_ATTR_BY_HORIZON[horizon]
        excess = f"excess_return_{horizon}"
        clauses.append(f"({rating} IS NULL OR {rating} BETWEEN 1 AND 99)")
        clauses.append(
            f"(({rating} IS NULL AND {excess} IS NULL) "
            f"OR ({rating} IS NOT NULL AND {excess} IS NOT NULL))"
        )
        if index:
            previous = STOCK_RS_RATING_ATTR_BY_HORIZON[HORIZONS[index - 1]]
            clauses.append(f"({rating} IS NULL OR {previous} IS NOT NULL)")
    clauses.append(f"{STOCK_RS_RATING_ATTR_BY_HORIZON[HORIZONS[-1]]} IS NULL")
    return " AND ".join(clauses)


# Rated horizons form a leading prefix (a stock with the last horizon is
# eligible, so the last rating is always NULL), each in 1..99 and paired with
# its excess return.
STOCK_RS_PLACEMENT_CHECK = _placement_check()


class MarketRsRun(Base):
    __tablename__ = "market_rs_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market = Column(String(8), nullable=False)
    as_of_date = Column(Date, nullable=False)
    formula_version = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False)
    benchmark_symbol = Column(String(32), nullable=False)
    benchmark_as_of_date = Column(Date, nullable=False)
    universe_hash = Column(String(64), nullable=False)
    expected_symbol_count = Column(Integer, nullable=False)
    eligible_symbol_count = Column(Integer, nullable=False, default=0)
    excluded_symbol_count = Column(Integer, nullable=False, default=0)
    diagnostics_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at = Column(DateTime(timezone=True), nullable=True)
    # NULL: the run predates young-stock placement. 0..N: placement ran.
    placed_symbol_count = Column(Integer, nullable=True)

    rows = relationship(
        "StockRsSnapshot",
        back_populates="run",
        cascade="all, delete-orphan",
    )
    placements = relationship(
        "StockRsPlacementSnapshot",
        back_populates="run",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint(
            "market", "as_of_date", "formula_version", name="uq_market_rs_run"
        ),
        CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_market_rs_run_status",
        ),
        CheckConstraint(
            "placed_symbol_count IS NULL OR placed_symbol_count >= 0",
            name="ck_market_rs_run_placed_count",
        ),
        Index(
            "ix_market_rs_run_lookup",
            "market",
            "formula_version",
            "as_of_date",
            "status",
        ),
    )


class StockRsSnapshot(Base):
    __tablename__ = "stock_rs_snapshots"

    run_id = Column(
        Integer,
        ForeignKey("market_rs_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    symbol = Column(String(20), primary_key=True)
    overall_rs = Column(SmallInteger, nullable=False)
    rs_1d = Column(SmallInteger, nullable=False, default=50, server_default="50")
    rs_1w = Column(SmallInteger, nullable=False, default=50, server_default="50")
    rs_1m = Column(SmallInteger, nullable=False)
    rs_3m = Column(SmallInteger, nullable=False)
    rs_6m = Column(SmallInteger, nullable=False)
    rs_9m = Column(SmallInteger, nullable=False)
    rs_12m = Column(SmallInteger, nullable=False)
    weighted_composite = Column(Float, nullable=False)
    excess_return_1d = Column(Float, nullable=False, default=0.0, server_default="0")
    excess_return_1w = Column(Float, nullable=False, default=0.0, server_default="0")
    excess_return_1m = Column(Float, nullable=False)
    excess_return_3m = Column(Float, nullable=False)
    excess_return_6m = Column(Float, nullable=False)
    excess_return_9m = Column(Float, nullable=False)
    excess_return_12m = Column(Float, nullable=False)

    run = relationship("MarketRsRun", back_populates="rows")

    __table_args__ = (
        CheckConstraint(
            "overall_rs BETWEEN 1 AND 99 AND rs_1d BETWEEN 1 AND 99 "
            "AND rs_1w BETWEEN 1 AND 99 AND rs_1m BETWEEN 1 AND 99 "
            "AND rs_3m BETWEEN 1 AND 99 AND rs_6m BETWEEN 1 AND 99 "
            "AND rs_9m BETWEEN 1 AND 99 AND rs_12m BETWEEN 1 AND 99",
            name="ck_stock_rs_rating_range",
        ),
        Index("ix_stock_rs_symbol_run", "symbol", "run_id"),
    )


class StockRsPlacementSnapshot(Base):
    """One young stock's horizon ratings in one run, placed against that run's
    eligible universe. Column names match StockRsSnapshot so
    STOCK_RS_RATING_ATTR_BY_HORIZON maps both tables."""

    __tablename__ = "stock_rs_placement_snapshots"

    run_id = Column(
        Integer,
        ForeignKey("market_rs_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    symbol = Column(String(20), primary_key=True)
    rs_1d = Column(SmallInteger, nullable=False)
    rs_1w = Column(SmallInteger, nullable=True)
    rs_1m = Column(SmallInteger, nullable=True)
    rs_3m = Column(SmallInteger, nullable=True)
    rs_6m = Column(SmallInteger, nullable=True)
    rs_9m = Column(SmallInteger, nullable=True)
    rs_12m = Column(SmallInteger, nullable=True)
    excess_return_1d = Column(Float, nullable=False)
    excess_return_1w = Column(Float, nullable=True)
    excess_return_1m = Column(Float, nullable=True)
    excess_return_3m = Column(Float, nullable=True)
    excess_return_6m = Column(Float, nullable=True)
    excess_return_9m = Column(Float, nullable=True)
    excess_return_12m = Column(Float, nullable=True)

    run = relationship("MarketRsRun", back_populates="placements")

    __table_args__ = (
        CheckConstraint(STOCK_RS_PLACEMENT_CHECK, name="ck_stock_rs_placement_range"),
        Index("ix_stock_rs_placement_symbol_run", "symbol", "run_id"),
    )

