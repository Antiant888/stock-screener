"""Server-Timing response header: wall time and database time per request.

Browsers show it in DevTools' Timing tab, so a slow endpoint on the live
server says whether the time went to the database or to Python, e.g.
``db;dur=12.3;desc="14 queries", app;dur=41.0``. ``app`` is the time until
the response headers are sent: the endpoint, its serialization and the
middleware inside this one; a streamed body is not included.
"""

from __future__ import annotations

from contextvars import ContextVar
from time import perf_counter
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from starlette.datastructures import MutableHeaders

# [query count, seconds] for the current request. FastAPI runs sync endpoints
# and dependencies in worker threads with a copy of this context, which
# still holds the same list, so their queries add to it.
_db_time: ContextVar[list | None] = ContextVar("server_timing_db", default=None)


@event.listens_for(Engine, "before_cursor_execute")
def _query_started(conn, cursor, statement, parameters, context, executemany) -> None:
    if context is not None and _db_time.get() is not None:
        context._server_timing_started = perf_counter()


@event.listens_for(Engine, "after_cursor_execute")
def _query_finished(conn, cursor, statement, parameters, context, executemany) -> None:
    stats = _db_time.get()
    started = getattr(context, "_server_timing_started", None)
    if stats is not None and started is not None:
        stats[0] += 1
        stats[1] += perf_counter() - started
        # Counted once: a later fetch error reaches handle_error too.
        context._server_timing_started = None


@event.listens_for(Engine, "handle_error")
def _query_failed(exception_context) -> None:
    # A statement that fails to execute still cost a round trip, and
    # after_cursor_execute doesn't run for it.
    _query_finished(None, None, None, None, exception_context.execution_context, False)


class ServerTimingMiddleware:
    """Plain ASGI, like RuntimeServicesContextMiddleware, so streaming and
    background tasks run inside this call without re-wrapping."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        stats = [0, 0.0]
        token = _db_time.set(stats)
        started = perf_counter()

        async def send_with_timing(message: Any) -> None:
            if message["type"] == "http.response.start":
                total_ms = (perf_counter() - started) * 1000
                MutableHeaders(scope=message).append(
                    "Server-Timing",
                    f'db;dur={stats[1] * 1000:.1f};desc="{stats[0]} queries", '
                    f"app;dur={total_ms:.1f}",
                )
            await send(message)

        try:
            await self.app(scope, receive, send_with_timing)
        finally:
            _db_time.reset(token)
