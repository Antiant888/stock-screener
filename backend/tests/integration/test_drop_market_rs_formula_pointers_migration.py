"""Upgrade/downgrade proof for dropping the Market RS formula pointer table."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, inspect, text

from app.domain.relative_strength import BALANCED_RS_FORMULA_VERSION

# What a deployment that never finished the balanced rollout still stores.
STORED_LEGACY_FORMULA = "legacy-linear-v1"

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "20261021_0068_drop_market_rs_formula_pointers.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("drop_pointers_migration", MIGRATION_PATH)
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


def _create_pre_drop_schema(engine, *, pointers: dict[str, str], runs: list[tuple[str, str]]) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE market_rs_formula_pointers ("
                "market VARCHAR(8) PRIMARY KEY, "
                "formula_version VARCHAR(64) NOT NULL, "
                "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE market_rs_runs ("
                "id INTEGER PRIMARY KEY, market VARCHAR(8) NOT NULL, "
                "status VARCHAR(16) NOT NULL, formula_version VARCHAR(64) NOT NULL)"
            )
        )
        for market, formula in pointers.items():
            connection.execute(
                text(
                    "INSERT INTO market_rs_formula_pointers (market, formula_version) "
                    "VALUES (:market, :formula)"
                ),
                {"market": market, "formula": formula},
            )
        for market, status in runs:
            connection.execute(
                text(
                    "INSERT INTO market_rs_runs (market, status, formula_version) "
                    "VALUES (:market, :status, :formula)"
                ),
                {"market": market, "status": status, "formula": BALANCED_RS_FORMULA_VERSION},
            )


def _pointer_rows(engine) -> list[tuple[str, str]]:
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT market, formula_version FROM market_rs_formula_pointers "
                "ORDER BY market"
            )
        ).all()


def test_upgrade_drops_the_table_when_every_pointer_is_balanced(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'balanced.sqlite'}")
    _create_pre_drop_schema(
        engine,
        pointers={"US": BALANCED_RS_FORMULA_VERSION, "HK": BALANCED_RS_FORMULA_VERSION},
        runs=[("US", "completed"), ("HK", "completed")],
    )

    _run_revision(engine, "upgrade")

    assert inspect(engine).get_table_names() == ["market_rs_runs"]
    engine.dispose()


def test_upgrade_refuses_while_a_pointer_names_a_retired_formula(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite'}")
    _create_pre_drop_schema(
        engine,
        pointers={
            "US": BALANCED_RS_FORMULA_VERSION,
            "HK": STORED_LEGACY_FORMULA,
            "JP": STORED_LEGACY_FORMULA,
        },
        runs=[("US", "completed")],
    )

    with pytest.raises(RuntimeError) as exc_info:
        _run_revision(engine, "upgrade")

    assert str(exc_info.value) == (
        "Market RS formula pointers still name a retired formula: "
        "HK='legacy-linear-v1', JP='legacy-linear-v1'. This release has only "
        "'balanced-horizon-percentile-v2' and cannot finish a rollout. Finish the "
        "balanced rollout on the previous release first: run 'python -m "
        "app.scripts.backfill_market_rs --market <MARKET> --through-date "
        "<YYYY-MM-DD> --activate' for each listed market on the release before "
        "this one, then upgrade. If a listed market is no longer enabled, delete "
        "its row from market_rs_formula_pointers."
    )
    assert _pointer_rows(engine) == [
        ("HK", STORED_LEGACY_FORMULA),
        ("JP", STORED_LEGACY_FORMULA),
        ("US", BALANCED_RS_FORMULA_VERSION),
    ]
    engine.dispose()


def test_downgrade_seeds_a_balanced_pointer_only_for_markets_with_a_completed_run(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'downgrade.sqlite'}")
    _create_pre_drop_schema(
        engine,
        pointers={"US": BALANCED_RS_FORMULA_VERSION},
        runs=[("US", "completed"), ("US", "completed"), ("HK", "completed"), ("JP", "failed")],
    )
    _run_revision(engine, "upgrade")

    _run_revision(engine, "downgrade")

    assert _pointer_rows(engine) == [
        ("HK", BALANCED_RS_FORMULA_VERSION),
        ("US", BALANCED_RS_FORMULA_VERSION),
    ]
    _run_revision(engine, "upgrade")
    assert inspect(engine).get_table_names() == ["market_rs_runs"]
    engine.dispose()
