"""Upgrade/downgrade proof for the young-stock RS placements migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from app.infra.db.models.relative_strength import StockRsPlacementSnapshot

BACKEND_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    BACKEND_ROOT / "alembic" / "versions" / "20261021_0069_stock_rs_placements.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("placements_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_revision(engine, fn_name: str) -> None:
    module = _load_migration()
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        original_op = module.op
        module.op = operations
        try:
            getattr(module, fn_name)()
        finally:
            module.op = original_op


def _create_pre_placement_schema(engine) -> None:
    metadata = sa.MetaData()
    sa.Table(
        "market_rs_runs",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("market", sa.String(8), nullable=False),
        sa.Column("as_of_date", sa.Date, nullable=False),
        sa.Column("formula_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("benchmark_symbol", sa.String(32), nullable=False),
        sa.Column("benchmark_as_of_date", sa.Date, nullable=False),
        sa.Column("universe_hash", sa.String(64), nullable=False),
        sa.Column("expected_symbol_count", sa.Integer, nullable=False),
        sa.Column("eligible_symbol_count", sa.Integer, nullable=False),
        sa.Column("excluded_symbol_count", sa.Integer, nullable=False),
        sa.Column("diagnostics_json", sa.JSON, nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_market_rs_run_status",
        ),
        sa.UniqueConstraint(
            "market", "as_of_date", "formula_version", name="uq_market_rs_run"
        ),
    )
    sa.Table(
        "stock_rs_snapshots",
        metadata,
        sa.Column(
            "run_id",
            sa.Integer,
            sa.ForeignKey("market_rs_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("symbol", sa.String(20), primary_key=True),
        sa.Column("overall_rs", sa.SmallInteger, nullable=False),
    )
    metadata.create_all(engine)


_RUN_INSERT = sa.text(
    """
    INSERT INTO market_rs_runs (
        market, as_of_date, formula_version, status, benchmark_symbol,
        benchmark_as_of_date, universe_hash, expected_symbol_count,
        eligible_symbol_count, excluded_symbol_count, diagnostics_json
    ) VALUES (
        'US', '2026-04-10', 'balanced-horizon-percentile-v2', :status, 'SPY',
        '2026-04-10', 'a', 3, 2, 1, '{"rs_snapshot_schema_version": 2}'
    )
    """
)
_PLACEMENT_INSERT = sa.text(
    """
    INSERT INTO stock_rs_placement_snapshots (
        run_id, symbol, rs_1d, rs_1w, rs_1m, rs_3m, rs_6m, rs_9m, rs_12m,
        excess_return_1d, excess_return_1w, excess_return_1m, excess_return_3m,
        excess_return_6m, excess_return_9m, excess_return_12m
    ) VALUES (
        :run_id, :symbol, :rs_1d, :rs_1w, :rs_1m, :rs_3m, :rs_6m, :rs_9m, :rs_12m,
        :er_1d, :er_1w, :er_1m, :er_3m, :er_6m, :er_9m, :er_12m
    )
    """
)


def _placement_row(run_id, symbol, **overrides):
    row = {
        "run_id": run_id,
        "symbol": symbol,
        "rs_1d": 99,
        "rs_1w": 99,
        "rs_1m": 66,
        "rs_3m": 1,
        "rs_6m": None,
        "rs_9m": None,
        "rs_12m": None,
        "er_1d": 0.2,
        "er_1w": 0.2,
        "er_1m": 0.05,
        "er_3m": -0.2,
        "er_6m": None,
        "er_9m": None,
        "er_12m": None,
    }
    row.update(overrides)
    return row


def test_placements_migration_adds_constrained_table_and_keeps_old_runs(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'placements.sqlite'}")
    _create_pre_placement_schema(engine)
    with engine.begin() as connection:
        run_id = connection.execute(_RUN_INSERT, {"status": "completed"}).lastrowid
        connection.execute(
            sa.text(
                "INSERT INTO stock_rs_snapshots (run_id, symbol, overall_rs) "
                "VALUES (:run_id, 'AAA', 70), (:run_id, 'BBB', 80)"
            ),
            {"run_id": run_id},
        )

    _run_revision(engine, "upgrade")

    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT placed_symbol_count FROM market_rs_runs")
        ).scalar_one() is None
        columns = [
            column["name"]
            for column in sa.inspect(connection).get_columns("stock_rs_placement_snapshots")
        ]
        assert columns == list(StockRsPlacementSnapshot.__table__.columns.keys())
    with engine.begin() as connection:
        connection.execute(_PLACEMENT_INSERT, _placement_row(run_id, "YNG"))
        connection.execute(
            _PLACEMENT_INSERT,
            _placement_row(
                run_id, "TINY", rs_1w=None, rs_1m=None, rs_3m=None,
                er_1w=None, er_1m=None, er_3m=None,
            ),
        )
        connection.execute(
            sa.text("UPDATE market_rs_runs SET placed_symbol_count = 2 WHERE id = :id"),
            {"id": run_id},
        )
    for label, overrides in (
        ("skips a horizon", {"rs_1w": None, "er_1w": None}),
        ("rates 12m", {"rs_6m": 50, "rs_9m": 50, "rs_12m": 50, "er_6m": 0.0, "er_9m": 0.0, "er_12m": 0.0}),
        ("rating below 1", {"rs_1d": 0}),
        ("rating above 99", {"rs_3m": 100}),
        ("rating without its excess return", {"er_1m": None}),
        ("excess return without its rating", {"rs_3m": None}),
        ("no 1d rating", {"rs_1d": None, "er_1d": None}),
    ):
        with engine.begin() as connection, pytest.raises(IntegrityError):
            connection.execute(_PLACEMENT_INSERT, _placement_row(run_id, "BAD", **overrides))
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            sa.text("UPDATE market_rs_runs SET placed_symbol_count = -1 WHERE id = :id"),
            {"id": run_id},
        )
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(_RUN_INSERT, {"status": "bogus"})
    with engine.connect() as connection:
        assert connection.execute(
            sa.text(
                "SELECT symbol, rs_1d, rs_1w, rs_1m, rs_3m, rs_12m "
                "FROM stock_rs_placement_snapshots ORDER BY symbol"
            )
        ).all() == [("TINY", 99, None, None, None, None), ("YNG", 99, 99, 66, 1, None)]

    _run_revision(engine, "downgrade")

    with engine.connect() as connection:
        inspector = sa.inspect(connection)
        assert not inspector.has_table("stock_rs_placement_snapshots")
        assert "placed_symbol_count" not in {
            column["name"] for column in inspector.get_columns("market_rs_runs")
        }
        assert connection.execute(
            sa.text("SELECT symbol, overall_rs FROM stock_rs_snapshots ORDER BY symbol")
        ).all() == [("AAA", 70), ("BBB", 80)]
        assert connection.execute(
            sa.text("SELECT status, excluded_symbol_count FROM market_rs_runs")
        ).one() == ("completed", 1)
    engine.dispose()
