"""Process and context ownership for the runtime service container."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token
from threading import RLock
from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from app.wiring.bootstrap import RuntimeServices


_runtime_services_ctx: ContextVar[RuntimeServices | None] = ContextVar(
    "runtime_services_ctx",
    default=None,
)
_process_runtime_services_lock = RLock()
_process_runtime_services: RuntimeServices | None = None


def set_runtime_services(
    runtime: RuntimeServices,
    *,
    bind_process: bool = False,
) -> Token[RuntimeServices | None]:
    global _process_runtime_services
    if bind_process:
        with _process_runtime_services_lock:
            _process_runtime_services = runtime
    return _runtime_services_ctx.set(runtime)


def reset_runtime_services(token: Token[RuntimeServices | None]) -> None:
    _runtime_services_ctx.reset(token)


def initialize_process_runtime_services(
    factory: Callable[[], RuntimeServices],
    *,
    force: bool = False,
) -> RuntimeServices:
    global _process_runtime_services
    with _process_runtime_services_lock:
        if _process_runtime_services is None or force:
            _process_runtime_services = factory()
        runtime = _process_runtime_services
    set_runtime_services(runtime, bind_process=True)
    return runtime


def clear_runtime_services() -> None:
    global _process_runtime_services
    with _process_runtime_services_lock:
        _process_runtime_services = None
    _runtime_services_ctx.set(None)


def resolve_runtime_services(request: Request | None = None) -> RuntimeServices:
    if request is not None:
        request_runtime = getattr(request.app.state, "runtime_services", None)
        if request_runtime is not None:
            return request_runtime
    context_runtime = _runtime_services_ctx.get()
    if context_runtime is not None:
        return context_runtime
    with _process_runtime_services_lock:
        if _process_runtime_services is not None:
            return _process_runtime_services
    raise RuntimeError(
        "RuntimeServices are not initialized for this context. "
        "Call initialize_process_runtime_services() at process startup."
    )


def current_runtime_services() -> RuntimeServices | None:
    return _runtime_services_ctx.get()


__all__ = [
    "clear_runtime_services",
    "current_runtime_services",
    "initialize_process_runtime_services",
    "reset_runtime_services",
    "resolve_runtime_services",
    "set_runtime_services",
]
