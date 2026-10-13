"""
Feature Store SQLAlchemy models.

Stores pre-computed daily screening snapshots decoupled from individual scans.
Uses a pointer-based atomic publishing pattern: compute a new FeatureRun,
populate StockFeatureDaily rows, then atomically swap FeatureRunPointer
to make the new data visible to readers.

Tables:
    feature_runs              — One row per screening computation
    feature_run_universe_symbols — Symbols included in each run
    stock_feature_daily       — Per-symbol screening scores
    feature_run_pointers      — Atomic publish pointers (e.g. 'latest_published')
"""
from sqlalchemy import (
    Boolean, Column, Integer, Float, Text, Date, DateTime, JSON,
    ForeignKey, Index, event, false, inspect,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import deferred, relationship
from sqlalchemy.orm.attributes import flag_modified

from app.database import Base


class FeatureRun(Base):
    """A single screening computation run."""

    __tablename__ = "feature_runs"

    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    as_of_date = Column(Date, nullable=False, index=True)
    run_type = Column(Text, nullable=False)  # daily_snapshot / backfill / manual
    status = Column(Text, nullable=False, index=True)  # running / completed / failed / quarantined / published
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    code_version = Column(Text, nullable=True)
    universe_hash = Column(Text, nullable=True)
    input_hash = Column(Text, nullable=True)
    config_json = Column(JSON, nullable=True)
    correlation_id = Column(Text, nullable=True, index=True)
    stats_json = Column(JSON, nullable=True)
    warnings_json = Column(JSON, nullable=True)

    # Relationships
    universe_symbols = relationship(
        "FeatureRunUniverseSymbol",
        back_populates="run",
        cascade="all, delete-orphan",
    )
    features = relationship(
        "StockFeatureDaily",
        back_populates="run",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_feature_runs_date_status", "as_of_date", "status"),
        Index(
            "ix_feature_runs_exact_lookup",
            "status",
            "universe_hash",
            "input_hash",
            "published_at",
        ),
    )


class FeatureRunUniverseSymbol(Base):
    """Symbol included in a feature run's universe."""

    __tablename__ = "feature_run_universe_symbols"

    run_id = Column(
        Integer,
        ForeignKey("feature_runs.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    symbol = Column(Text, nullable=False, primary_key=True, index=True)

    # Relationships
    run = relationship("FeatureRun", back_populates="universe_symbols")


class StockFeatureDaily(Base):
    """Pre-computed screening scores for a single stock on a single day."""

    __tablename__ = "stock_feature_daily"

    run_id = Column(
        Integer,
        ForeignKey("feature_runs.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    symbol = Column(Text, nullable=False, primary_key=True, index=True)
    as_of_date = Column(Date, nullable=False, index=True)  # denormalized from FeatureRun
    composite_score = Column(Float, nullable=True, index=True)
    overall_rating = Column(Integer, nullable=True, index=True)
    passes_count = Column(Integer, nullable=True)
    rs_line_new_high = Column(
        Boolean, nullable=False, default=False, server_default=false(), index=True
    )
    rs_line_new_high_before_price = Column(
        Boolean, nullable=False, default=False, server_default=false(), index=True
    )
    rs_line_blue_dot_recent = Column(
        Boolean, nullable=False, default=False, server_default=false(), index=True
    )
    rs_line_new_high_date = Column(Text, nullable=True, index=True)
    details_json = Column(JSON, nullable=True)
    # details_json minus the setup payload (~6 kB vs ~64 kB a row): what JSON
    # filters, sorts and table pages read. _store_table_details derives it on
    # unit-of-work flushes; Core statements and ORM bulk operations skip mapper
    # events, so they must set it via derived_details_columns().
    # NULL on rows written before 20261011_0064; such runs read details_json.
    table_details_json = deferred(
        Column(JSON().with_variant(JSONB(), "postgresql"), nullable=True)
    )
    # Typed copies of the hottest sort/filter fields (HOT_NUMERIC_FIELDS,
    # HOT_TEXT_FIELDS), written with table_details_json by the same writers.
    # NULL on rows written before 20261013_0066; hot_fields_stored marks rows
    # that carry them, since NULL is also a legitimate value for each field.
    hot_fields_stored = Column(Boolean, nullable=True)
    rs_rating = Column(Float, nullable=True)
    rs_rating_1m = Column(Float, nullable=True)
    rs_rating_3m = Column(Float, nullable=True)
    rs_rating_12m = Column(Float, nullable=True)
    minervini_score = Column(Float, nullable=True)
    canslim_score = Column(Float, nullable=True)
    adr_percent = Column(Float, nullable=True)
    eps_growth_qq = Column(Float, nullable=True)
    sales_growth_qq = Column(Float, nullable=True)
    beta = Column(Float, nullable=True)
    perf_week = Column(Float, nullable=True)
    perf_month = Column(Float, nullable=True)
    perf_3m = Column(Float, nullable=True)
    perf_6m = Column(Float, nullable=True)
    avg_dollar_volume = Column(Float, nullable=True)
    ibd_industry_group = Column(Text, nullable=True)
    gics_sector = Column(Text, nullable=True)

    # Relationships
    run = relationship("FeatureRun", back_populates="features")

    __table_args__ = (
        Index("ix_stock_feature_daily_run_score", "run_id", "composite_score"),
        Index("ix_stock_feature_daily_run_rating", "run_id", "overall_rating"),
        Index("ix_sfd_run_rs_line_new_high", "run_id", "rs_line_new_high"),
        Index("ix_sfd_run_rs_line_new_high_before_price", "run_id", "rs_line_new_high_before_price"),
        Index("ix_sfd_run_rs_line_blue_dot_recent", "run_id", "rs_line_blue_dot_recent"),
        Index("ix_sfd_run_rs_line_new_high_date", "run_id", "rs_line_new_high_date"),
    )


_SETUP_PAYLOAD_KEYS = ("explain", "candidates")


def table_details(details):
    """``details`` without the setup engine's explain/candidates payload."""
    setup = details.get("setup_engine") if isinstance(details, dict) else None
    if not isinstance(setup, dict):
        return details
    return {
        **details,
        "setup_engine": {
            key: value for key, value in setup.items() if key not in _SETUP_PAYLOAD_KEYS
        },
    }


# Hot details keys stored as typed columns of the same name.
HOT_NUMERIC_FIELDS = (
    "rs_rating",
    "rs_rating_1m",
    "rs_rating_3m",
    "rs_rating_12m",
    "minervini_score",
    "canslim_score",
    "adr_percent",
    "eps_growth_qq",
    "sales_growth_qq",
    "beta",
    "perf_week",
    "perf_month",
    "perf_3m",
    "perf_6m",
    "avg_dollar_volume",
)
HOT_TEXT_FIELDS = ("ibd_industry_group", "gics_sector")


def _hot_number(value):
    """``value`` as PostgreSQL's ``CAST(details ->> key AS FLOAT)`` reads it,
    with NULL where that cast would fail the whole query or overflow."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (ValueError, OverflowError):
        return None


def derived_details_columns(details) -> dict:
    """Every column derived from ``details_json``; writers set all of them."""
    source = details if isinstance(details, dict) else {}
    return {
        "table_details_json": table_details(details),
        "hot_fields_stored": True,
        **{field: _hot_number(source.get(field)) for field in HOT_NUMERIC_FIELDS},
        **{
            field: value if isinstance(value := source.get(field), str) else None
            for field in HOT_TEXT_FIELDS
        },
    }


@event.listens_for(StockFeatureDaily, "before_insert")
@event.listens_for(StockFeatureDaily, "before_update")
def _store_table_details(_mapper, _connection, target) -> None:
    if inspect(target).attrs.details_json.history.has_changes():
        for column, value in derived_details_columns(target.details_json).items():
            setattr(target, column, value)
        # Without a setup payload the copy is the details object itself, so
        # an in-place edit leaves no history on it; always write it.
        flag_modified(target, "table_details_json")


class FeatureRunPointer(Base):
    """Named pointer to a feature run for atomic publishing."""

    __tablename__ = "feature_run_pointers"

    key = Column(Text, primary_key=True)  # e.g. 'latest_published'
    run_id = Column(
        Integer,
        ForeignKey("feature_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
