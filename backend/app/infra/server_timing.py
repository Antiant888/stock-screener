"""Server-Timing response header: total and database time per request.

Browsers show it in DevTools' Timing tab, e.g.
``db;dur=12.3;desc="14 queries", total;dur=41.0``.

- ``total``: until the response headers are sent, including the endpoint,
  its serialization and every other middleware; a streamed body is not.
- ``db``: time inside the cursor executing the request's statements. An
  executemany counts as one; insertmanyvalues counts one per batch.

``total`` minus ``db`` is not all Python. It also holds waiting for a pooled
connection, connecting, the pre-ping and BEGIN/COMMIT/ROLLBACK, none of which
pass through cursor execution. Statements run in a thread that doesn't
inherit the request's context (``ThreadPoolExecutor``, ``threading.Thread``,
``loop.run_in_executor``) are not counted; ``asyncio.to_thread``,
``run_in_threadpool`` and FastAPI's sync endpoints and dependencies are.
"""

from __future__ import annotations

from contextvars import ContextVar
from time import perf_counter
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from starlette.datastructures import MutableHeaders

# [statement count, seconds] for the current request. Worker threads that
# inherit the context still hold the same list, so their statements add to it.
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
                count = stats[0]
                MutableHeaders(scope=message).append(
                    "Server-Timing",
                    f'db;dur={stats[1] * 1000:.1f};desc="{count} {"query" if count == 1 else "queries"}", '
                    f"total;dur={total_ms:.1f}",
                )
            await send(message)

        try:
            await self.app(scope, receive, send_with_timing)
        finally:
            _db_time.reset(token)


def install_server_timing(app: Any) -> None:
    """Time a Starlette/FastAPI app from outside its error handling.

    ``add_middleware`` layers sit inside Starlette's ServerErrorMiddleware,
    which sends an unhandled error's 500 with the bare ``send``. Wrapping the
    built stack times those too, and they are often the slowest requests
    (a connection pool timeout, for one).
    """
    build = app.build_middleware_stack
    app.build_middleware_stack = lambda: ServerTimingMiddleware(build())
