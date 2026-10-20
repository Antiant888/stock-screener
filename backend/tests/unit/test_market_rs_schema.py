from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from app.domain.relative_strength import HORIZONS
from app.infra.db.models.relative_strength import (
    MarketRsRun,
    StockRsPlacementSnapshot,
    StockRsSnapshot,
)
from app.models.industry import IBDGroupRank


def test_market_rs_models_expose_versioned_snapshot_contract():
    assert MarketRsRun.__table__.name == "market_rs_runs"
    assert StockRsSnapshot.__table__.primary_key.columns.keys() == ["run_id", "symbol"]
    assert {
        "rs_1d",
        "rs_1w",
        "excess_return_1d",
        "excess_return_1w",
    }.issubset(StockRsSnapshot.__table__.columns.keys())
    assert {
        "avg_rs_rating_1d",
        "avg_rs_rating_1w",
        "avg_rs_rating_1m",
        "avg_rs_rating_3m",
        "avg_rs_rating_6m",
        "rs_formula_version",
        "market_rs_run_id",
    }.issubset(IBDGroupRank.__table__.columns.keys())


def test_group_unique_constraint_includes_formula_version():
    names = {
        constraint.name
        for constraint in IBDGroupRank.__table__.constraints
        if constraint.name
    }
    assert "uix_ibd_group_rank_market_date_formula" in names


def test_canonical_market_rs_migration_extends_current_head():
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "20260718_0025_add_canonical_market_rs.py"
    )
    spec = spec_from_file_location("canonical_market_rs_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "20260718_0025"
    assert migration.down_revision == "20260701_0024"


def test_short_horizon_group_rs_migration_extends_canonical_rs_revision():
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "20260805_0026_add_short_horizon_group_rs.py"
    )
    spec = spec_from_file_location("short_horizon_group_rs_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "20260805_0026"
    assert migration.down_revision == "20260718_0025"


def test_placement_snapshot_columns_follow_the_horizon_list():
    assert list(StockRsPlacementSnapshot.__table__.columns.keys()) == [
        "run_id",
        "symbol",
        *(f"rs_{horizon}" for horizon in HORIZONS),
        *(f"excess_return_{horizon}" for horizon in HORIZONS),
    ]
    assert StockRsPlacementSnapshot.__table__.columns["rs_1d"].nullable is False
    assert StockRsPlacementSnapshot.__table__.columns["rs_12m"].nullable is True
    assert MarketRsRun.__table__.columns["placed_symbol_count"].nullable is True


PLACEMENT_CHECK_AT_0069 = (
    "(rs_1d IS NULL OR rs_1d BETWEEN 1 AND 99) AND "
    "((rs_1d IS NULL AND excess_return_1d IS NULL) OR (rs_1d IS NOT NULL AND excess_return_1d IS NOT NULL)) AND "
    "(rs_1w IS NULL OR rs_1w BETWEEN 1 AND 99) AND "
    "((rs_1w IS NULL AND excess_return_1w IS NULL) OR (rs_1w IS NOT NULL AND excess_return_1w IS NOT NULL)) AND "
    "(rs_1w IS NULL OR rs_1d IS NOT NULL) AND "
    "(rs_1m IS NULL OR rs_1m BETWEEN 1 AND 99) AND "
    "((rs_1m IS NULL AND excess_return_1m IS NULL) OR (rs_1m IS NOT NULL AND excess_return_1m IS NOT NULL)) AND "
    "(rs_1m IS NULL OR rs_1w IS NOT NULL) AND "
    "(rs_3m IS NULL OR rs_3m BETWEEN 1 AND 99) AND "
    "((rs_3m IS NULL AND excess_return_3m IS NULL) OR (rs_3m IS NOT NULL AND excess_return_3m IS NOT NULL)) AND "
    "(rs_3m IS NULL OR rs_1m IS NOT NULL) AND "
    "(rs_6m IS NULL OR rs_6m BETWEEN 1 AND 99) AND "
    "((rs_6m IS NULL AND excess_return_6m IS NULL) OR (rs_6m IS NOT NULL AND excess_return_6m IS NOT NULL)) AND "
    "(rs_6m IS NULL OR rs_3m IS NOT NULL) AND "
    "(rs_9m IS NULL OR rs_9m BETWEEN 1 AND 99) AND "
    "((rs_9m IS NULL AND excess_return_9m IS NULL) OR (rs_9m IS NOT NULL AND excess_return_9m IS NOT NULL)) AND "
    "(rs_9m IS NULL OR rs_6m IS NOT NULL) AND "
    "(rs_12m IS NULL OR rs_12m BETWEEN 1 AND 99) AND "
    "((rs_12m IS NULL AND excess_return_12m IS NULL) OR (rs_12m IS NOT NULL AND excess_return_12m IS NOT NULL)) AND "
    "(rs_12m IS NULL OR rs_9m IS NOT NULL) AND "
    "rs_12m IS NULL"
)


def test_placements_migration_extends_current_head_with_a_frozen_check():
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "20261021_0069_stock_rs_placements.py"
    )
    spec = spec_from_file_location("stock_rs_placements_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "20261021_0069"
    assert migration.down_revision == "20261021_0068"
    assert migration.HORIZONS == ("1d", "1w", "1m", "3m", "6m", "9m", "12m")
    assert migration.STOCK_RS_PLACEMENT_CHECK == PLACEMENT_CHECK_AT_0069
