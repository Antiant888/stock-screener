"""Recovery helpers for rebuildable theme-content storage."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from threading import Lock

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import engine
from ..infra.db.portability import is_postgres
from ..models.theme import ContentItem, ContentItemPipelineState, ThemeMention
from .economic_taxonomy_fence import ECONOMIC_TAXONOMY_FENCE_KEY
from .legacy_theme_write_guard import legacy_theme_writes_blocked

logger = logging.getLogger(__name__)


class ThemeContentResetRefused(RuntimeError):
    """Corrupt theme content storage that must be recovered from a backup.

    Not a ``LegacyThemeWritesBlocked``: this is a server-side incident, not a
    cutover conflict, so it must surface as a 5xx, not the API's 409.
    """

_THEME_CONTENT_STORAGE_LOCK = Lock()
_THEME_CONTENT_RESET_LOOKBACK_DAYS = 365
_THEME_CONTENT_RESET_ADVISORY_LOCK_KEY = 497230140118562871
_THEME_CONTENT_REINDEX_TARGETS = (
    "content_items",
    "content_sources",
    "theme_mentions",
    "content_item_pipeline_state",
)


def attempt_reindex_theme_content_storage(exc: Exception) -> bool:
    """Try to repair theme browser read paths by rebuilding relevant indexes."""
    logger.warning(
        "Attempting REINDEX for theme content browser after corruption signature: %s",
        exc,
    )
    try:
        with _THEME_CONTENT_STORAGE_LOCK:
            with engine.begin() as conn:
                for target in _THEME_CONTENT_REINDEX_TARGETS:
                    conn.execute(text(f'REINDEX TABLE "{target}"'))
    except Exception as reindex_exc:
        from ..database import is_corruption_error

        if not is_corruption_error(reindex_exc):
            raise
        logger.warning(
            "REINDEX did not clear theme content browser corruption: %s",
            reindex_exc,
        )
        return False
    logger.warning("REINDEX completed for theme content browser recovery")
    return True


def reset_corrupt_theme_content_storage(exc: Exception) -> None:
    """Drop and recreate rebuildable theme content tables after database corruption.

    Refused under economic authority (#472): ``theme_mentions`` is then rollback
    data and ``content_items`` backs economic evidence, so neither is rebuildable.
    """
    logger.warning(
        "Resetting theme content storage after database corruption signature: %s",
        exc,
    )
    with _THEME_CONTENT_STORAGE_LOCK:
        with engine.begin() as conn:
            # Held until the DDL commits, so a cutover cannot land between the
            # authority check and the drops.
            _acquire_publication_fence_shared(conn)
            if _reset_blocked_by_authority(conn):
                raise ThemeContentResetRefused(
                    "Theme content storage is not reset under economic authority "
                    "or a write fence; restore the latest valid backup instead."
                ) from exc
            _acquire_theme_content_reset_lock(conn)
            drop_theme_content_tables(conn)
            rewind_theme_content_source_cursors(conn)
            recreate_theme_content_tables(conn)


def _acquire_publication_fence_shared(conn) -> None:
    if is_postgres(conn):
        conn.execute(
            text("SELECT pg_advisory_xact_lock_shared(:key)"),
            {"key": ECONOMIC_TAXONOMY_FENCE_KEY},
        )


def _reset_blocked_by_authority(conn) -> bool:
    with Session(bind=conn) as session:
        return legacy_theme_writes_blocked(session)


def drop_theme_content_tables(conn) -> None:
    """Drop rebuildable theme content storage tables using normal DDL."""
    cascade = " CASCADE" if is_postgres(conn) else ""
    conn.execute(text(f"DROP TABLE IF EXISTS content_item_pipeline_state{cascade}"))
    conn.execute(text(f"DROP TABLE IF EXISTS theme_mentions{cascade}"))
    conn.execute(text(f"DROP TABLE IF EXISTS content_items{cascade}"))


def force_forget_theme_content_tables(conn) -> None:
    """Force-drop theme content storage tables (alias for drop)."""
    drop_theme_content_tables(conn)


def _acquire_theme_content_reset_lock(conn) -> None:
    """Serialize corruption resets across PostgreSQL workers within the transaction."""
    if not is_postgres(conn):
        return
    conn.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _THEME_CONTENT_RESET_ADVISORY_LOCK_KEY},
    )


def recreate_theme_content_tables(conn) -> None:
    """Recreate theme content storage tables after a corruption reset."""
    ContentItem.__table__.create(bind=conn, checkfirst=True)
    ThemeMention.__table__.create(bind=conn, checkfirst=True)
    ContentItemPipelineState.__table__.create(bind=conn, checkfirst=True)


def rewind_theme_content_source_cursors(conn) -> None:
    """Rewind content-source cursors so the next poll repopulates article history."""
    rewind_at = datetime.utcnow() - timedelta(days=_THEME_CONTENT_RESET_LOOKBACK_DAYS)
    conn.execute(
        text(
            """
            UPDATE content_sources
            SET last_fetched_at = :rewind_at,
                last_success_at = :rewind_at,
                total_items_fetched = 0
            """
        ),
        {"rewind_at": rewind_at},
    )
