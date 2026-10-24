"""Upgrade/downgrade proof for the watchlist daily-scan flag migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "20261024_0070_watchlist_daily_scan_flag.py"
)


def _run_revision(engine, fn_name: str) -> None:
    spec = importlib.util.spec_from_file_location("watchlist_flag_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with engine.begin() as connection:
        module.op = Operations(MigrationContext.configure(connection))
        getattr(module, fn_name)()


def test_flag_migration_defaults_existing_watchlists_off_and_drops_cleanly(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'watchlists.sqlite'}")
    metadata = sa.MetaData()
    sa.Table(
        "user_watchlists",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO user_watchlists (name, position) VALUES ('Leaders', 0)"))

    _run_revision(engine, "upgrade")

    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO user_watchlists (name, position) VALUES ('Fresh', 1)"))
        connection.execute(
            sa.text("UPDATE user_watchlists SET include_in_daily_scan = :on WHERE name = 'Fresh'"),
            {"on": True},
        )
        assert connection.execute(
            sa.text("SELECT name, include_in_daily_scan FROM user_watchlists ORDER BY id")
        ).all() == [("Leaders", False), ("Fresh", True)]

    _run_revision(engine, "downgrade")

    with engine.connect() as connection:
        assert [column["name"] for column in sa.inspect(connection).get_columns("user_watchlists")] == [
            "id", "name", "position",
        ]
        assert connection.execute(sa.text("SELECT name FROM user_watchlists ORDER BY id")).scalars().all() == [
            "Leaders", "Fresh",
        ]
