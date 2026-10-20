"""Alembic-backed runtime schema bootstrap helpers."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from time import sleep

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_INI = _BACKEND_ROOT / "alembic.ini"
_ALEMBIC_SCRIPT_LOCATION = _BACKEND_ROOT / "alembic"
_BASELINE_REVISION = "20260408_0001"

# Serializes startup migrations across uvicorn workers booting concurrently.
_MIGRATION_ADVISORY_LOCK_KEY = 0x5343414E  # "SCAN"
_MIGRATION_LOCK_POLL_SECONDS = 0.1


def _alembic_config(database_url: str) -> Config:
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("script_location", str(_ALEMBIC_SCRIPT_LOCATION))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _engine_database_url(engine: Engine) -> str:
    url = engine.url
    if hasattr(url, "render_as_string"):
        return url.render_as_string(hide_password=False)
    return str(url)


def _has_user_tables(conn: Connection) -> bool:
    return any(name != "alembic_version" for name in inspect(conn).get_table_names())


def _has_alembic_version_table(conn: Connection) -> bool:
    return "alembic_version" in inspect(conn).get_table_names()


@contextmanager
def _migration_lock(conn: Connection):
    """Hold a Postgres advisory lock so concurrent uvicorn workers (or
    replicas sharing one database) cannot run DDL simultaneously; late
    arrivals block, then no-op against the migrated schema. Releases
    automatically if the holding session dies. No-op on other dialects
    (the SQLite test harness)."""
    if conn.dialect.name != "postgresql":
        yield
        return
    while not conn.execute(
        text("SELECT pg_try_advisory_lock(:key)"),
        {"key": _MIGRATION_ADVISORY_LOCK_KEY},
    ).scalar_one():
        sleep(_MIGRATION_LOCK_POLL_SECONDS)
    try:
        yield
    finally:
        conn.execute(
            text("SELECT pg_advisory_unlock(:key)"),
            {"key": _MIGRATION_ADVISORY_LOCK_KEY},
        )


def migrate_database_to_head(engine: Engine, revision: str = "head") -> None:
    """Upgrade the database to ``revision``, refusing a pre-Alembic schema.

    One AUTOCOMMIT connection serves the lock and the schema inspection.
    AUTOCOMMIT matters: pg_advisory_lock is session-level, and without it the
    lock connection would sit idle-in-transaction for the whole upgrade —
    servers with idle_in_transaction_session_timeout would kill the session
    and release the lock mid-migration. Alembic itself connects separately
    (env.py builds its own engine from the config URL).
    """
    config = _alembic_config(_engine_database_url(engine))
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn, _migration_lock(conn):
        has_version_table = _has_alembic_version_table(conn)
        has_user_tables = _has_user_tables(conn)

        if has_user_tables and not has_version_table:
            raise RuntimeError(
                "Database has tables but no alembic_version table, so it predates Alembic baseline "
                f"{_BASELINE_REVISION}. Start release v1.6.0 once against this database, then "
                "upgrade; see 'Legacy Pre-Alembic Upgrade Path' in docs/INSTALL_DOCKER.md. "
                "If this app never used this database, point DATABASE_URL at an empty one."
            )

        logger.info("Running Alembic upgrade to revision %s", revision)
        command.upgrade(config, revision)
