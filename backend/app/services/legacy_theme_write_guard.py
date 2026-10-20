"""Keep unfenced legacy Theme writers out of economic authority (#472).

Fenced legacy writers go through ``legacy_producer_write``, which admits only
``LEGACY_WRITE_MODES``. The writers that bypass that fence use the same mode set
here, at two points:

- On entry: tasks skip and APIs return 409, so nothing starts.
- On write: a guarded writer (a decorated task, or a session marked with
  ``mark_legacy_theme_writer``) takes the shared publication fence in the
  transaction of its first write and re-checks the mode there, holding the
  fence until commit, as ``producer_write`` does. A cutover therefore waits for
  in-flight legacy writes, and a write after the switch fails closed. Locking
  per transaction rather than for a whole task avoids a deadlock with nested
  fenced writes on other connections.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps

from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from app.models.economic_taxonomy_runtime import TaxonomyAuthority
from app.services.economic_taxonomy_fence import ECONOMIC_TAXONOMY_FENCE_KEY

logger = logging.getLogger(__name__)

LEGACY_WRITE_MODES = frozenset({"legacy", "shadow", "dual"})
ECONOMIC_AUTHORITY_SKIP_REASON = "economic_authority"
# 409 detail for legacy writer APIs, on entry and when a write is fenced mid-request.
ECONOMIC_ENDPOINT_REQUIRED = {
    "code": "economic_generation_endpoint_required",
    "endpoint": "/api/v1/economic-themes",
}

# A guarded task's run state: None outside one, else {"blocked": bool}, set
# when the fence refuses a write so the wrapper skips even if the body
# swallowed the exception.
_LEGACY_WRITER: ContextVar[dict | None] = ContextVar("legacy_theme_writer", default=None)
_MARK = "legacy_theme_writer"
_FENCED_TRANSACTION = "legacy_theme_fenced_transaction"


class LegacyThemeWritesBlocked(RuntimeError):
    """Legacy Theme tables are read-only while economic authority serves."""


def legacy_theme_writes_blocked(db: Session) -> bool:
    """True under economic authority, or while rollback recovery fences writes
    (``writes_fenced``), as ``producer_write`` enforces for fenced writers."""
    # A column select, not db.get(): the identity map may hold a stale mode.
    row = db.execute(
        select(TaxonomyAuthority.mode, TaxonomyAuthority.writes_fenced)
        .where(TaxonomyAuthority.id == 1)
    ).one_or_none()
    return row is not None and (row.writes_fenced or row.mode not in LEGACY_WRITE_MODES)


def mark_legacy_theme_writer(session: Session) -> None:
    """Fence every write this session makes (API request sessions)."""
    session.info[_MARK] = True


def _current_transaction(session: Session):
    # The innermost one: a lock taken inside a savepoint is released when that
    # savepoint rolls back, so a fence is only known to hold for the
    # (sub)transaction it was taken in. Writes after a savepoint ends re-take
    # it, which is a cheap re-entrant shared lock when it is still held.
    return session.get_nested_transaction() or session.get_transaction()


def _fence_write(session: Session) -> None:
    task_state = _LEGACY_WRITER.get()
    if task_state is None and not session.info.get(_MARK):
        return
    transaction = _current_transaction(session)
    if transaction is not None and session.info.get(_FENCED_TRANSACTION) is transaction:
        return
    with session.no_autoflush:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock_shared(:key)"),
                {"key": ECONOMIC_TAXONOMY_FENCE_KEY},
            )
        if legacy_theme_writes_blocked(session):
            if task_state is not None:
                task_state["blocked"] = True
            raise LegacyThemeWritesBlocked(
                "Legacy Theme writes are disabled under economic authority."
            )
    session.info[_FENCED_TRANSACTION] = _current_transaction(session)


@event.listens_for(Session, "before_flush")
def _fence_legacy_theme_flush(session, flush_context, instances):
    _fence_write(session)


@event.listens_for(Session, "do_orm_execute")
def _fence_legacy_theme_bulk_write(orm_execute_state):
    if orm_execute_state.is_insert or orm_execute_state.is_update or orm_execute_state.is_delete:
        _fence_write(orm_execute_state.session)


def economic_authority_skip_payload() -> dict[str, object]:
    return {
        "status": "skipped",
        "reason": ECONOMIC_AUTHORITY_SKIP_REASON,
        "message": "Legacy Theme writes are disabled under economic authority.",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def skip_in_economic_authority(task=None, *, on_skip=None):
    """Task decorator: skip in economic mode on entry, and fence every write.

    Place it below ``@celery_app.task``; bound tasks pass ``self`` through.
    ``on_skip(*args, **kwargs)`` runs when the task is skipped, on entry or
    because a cutover fenced a write mid-run, e.g. to record a terminal state
    for work the task would have updated. It runs unfenced.
    """

    def decorate(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            from app.database import SessionLocal

            with SessionLocal() as db:
                blocked = legacy_theme_writes_blocked(db)
            if not blocked:
                state = {"blocked": False}
                token = _LEGACY_WRITER.set(state)
                try:
                    result = func(*args, **kwargs)
                except Exception:
                    if not state["blocked"]:
                        raise
                else:
                    if not state["blocked"]:
                        return result
                finally:
                    _LEGACY_WRITER.reset(token)
                # Cutover landed mid-run and the fence refused a write, whether
                # the body raised it or swallowed it: skip like an entry check.
                # ponytail: bodies that catch per item keep looping until their
                # batch ends; each further write is refused, none commits.
            logger.info("Skipping %s: %s", func.__name__, ECONOMIC_AUTHORITY_SKIP_REASON)
            if on_skip is not None:
                on_skip(*args, **kwargs)
            return economic_authority_skip_payload()

        return wrapper

    return decorate(task) if task is not None else decorate

