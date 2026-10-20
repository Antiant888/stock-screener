"""Focused tests for runtime Alembic bootstrap behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from app.infra.db.migrations import (
    _alembic_config,
    _engine_database_url,
    _migration_lock,
    migrate_database_to_head,
)
from sqlalchemy import create_engine


def test_engine_database_url_preserves_password_for_alembic_config():
    engine = create_engine("postgresql://stockscanner:secret@localhost/stockscanner")
    database_url = _engine_database_url(engine)
    assert database_url == "postgresql://stockscanner:secret@localhost/stockscanner"
    assert _alembic_config(database_url).get_main_option("sqlalchemy.url") == database_url
    engine.dispose()


def test_postgres_migration_lock_polling_does_not_hold_a_waiting_snapshot():
    """Break caught: blocking lock waiters stalling CREATE INDEX CONCURRENTLY."""
    lock_attempts = iter([False, True])
    statements: list[str] = []

    def execute(statement, _params=None):
        sql = str(statement)
        statements.append(sql)
        if "pg_try_advisory_lock" in sql:
            return SimpleNamespace(scalar_one=lambda: next(lock_attempts))
        return MagicMock()

    conn = MagicMock()
    conn.dialect = SimpleNamespace(name="postgresql")
    conn.execute.side_effect = execute

    with patch("app.infra.db.migrations.sleep", create=True) as sleep:
        with _migration_lock(conn):
            statements.append("migration")

    assert statements == [
        "SELECT pg_try_advisory_lock(:key)",
        "SELECT pg_try_advisory_lock(:key)",
        "migration",
        "SELECT pg_advisory_unlock(:key)",
    ]
    sleep.assert_called_once()


def _stub_engine(dialect_name: str, calls: list[str]):
    """Engine stub whose single connection records advisory lock/unlock.

    Mirrors the runtime flow: engine.connect() yields one connection,
    execution_options() returns the same connection, and all lock SQL
    goes through it.
    """

    def _record_execute(statement, params=None):
        sql = str(statement)
        if "pg_try_advisory_lock" in sql:
            calls.append("lock")
            return SimpleNamespace(scalar_one=lambda: True)
        elif "pg_advisory_unlock" in sql:
            calls.append("unlock")
        return MagicMock()

    conn = MagicMock()
    conn.dialect = SimpleNamespace(name=dialect_name)
    conn.execute.side_effect = _record_execute
    conn.execution_options.return_value = conn
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)

    return SimpleNamespace(
        url=f"{dialect_name}://stockscanner:secret@localhost/stockscanner",
        connect=MagicMock(return_value=conn),
    )


def test_migrate_database_to_head_refuses_schema_without_alembic_version():
    calls: list[str] = []
    engine = _stub_engine("postgresql", calls)

    with patch("app.infra.db.migrations._has_alembic_version_table", return_value=False), patch(
        "app.infra.db.migrations._has_user_tables", return_value=True
    ), patch("app.infra.db.migrations._alembic_config", return_value=object()), patch(
        "app.infra.db.migrations.command.upgrade"
    ) as upgrade, pytest.raises(RuntimeError) as refused:
        migrate_database_to_head(engine)

    assert str(refused.value) == (
        "Database has tables but no alembic_version table, so it predates Alembic baseline "
        "20260408_0001. Start release v1.6.0 once against this database, then upgrade; see "
        "'Legacy Pre-Alembic Upgrade Path' in docs/INSTALL_DOCKER.md. If this app never used "
        "this database, point DATABASE_URL at an empty one."
    )
    assert calls == ["lock", "unlock"]
    upgrade.assert_not_called()


def test_migrate_database_to_head_upgrades_new_schema():
    # Non-Postgres dialect (test harness SQLite) skips the advisory lock path.
    calls: list[str] = []
    engine = _stub_engine("sqlite", calls)
    with patch("app.infra.db.migrations._has_alembic_version_table", return_value=False), patch(
        "app.infra.db.migrations._has_user_tables", return_value=False
    ), patch("app.infra.db.migrations._alembic_config", return_value=object()), patch(
        "app.infra.db.migrations.command.upgrade"
    ) as upgrade:
        migrate_database_to_head(engine)

    assert calls == []
    upgrade.assert_called_once()


def test_migrate_database_to_head_releases_lock_when_upgrade_fails():
    calls: list[str] = []
    engine = _stub_engine("postgresql", calls)

    with patch("app.infra.db.migrations._has_alembic_version_table", return_value=True), patch(
        "app.infra.db.migrations._has_user_tables", return_value=True
    ), patch("app.infra.db.migrations._alembic_config", return_value=object()), patch(
        "app.infra.db.migrations.command.upgrade", side_effect=RuntimeError("boom")
    ), pytest.raises(RuntimeError):
        migrate_database_to_head(engine)

    assert calls == ["lock", "unlock"]


def test_single_connection_serves_lock_and_inspection():
    """The lock window must not open extra connections (one conn for
    lock + schema inspection; Alembic connects separately)."""
    calls: list[str] = []
    engine = _stub_engine("postgresql", calls)
    seen_conns = []

    with patch(
        "app.infra.db.migrations._has_alembic_version_table",
        side_effect=lambda conn: seen_conns.append(conn) or True,
    ), patch(
        "app.infra.db.migrations._has_user_tables",
        side_effect=lambda conn: seen_conns.append(conn) or True,
    ), patch("app.infra.db.migrations._alembic_config", return_value=object()), patch(
        "app.infra.db.migrations.command.upgrade"
    ):
        migrate_database_to_head(engine)

    engine.connect.assert_called_once()
    lock_conn = engine.connect.return_value
    assert seen_conns == [lock_conn, lock_conn]
