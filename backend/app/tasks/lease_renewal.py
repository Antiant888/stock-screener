"""Heartbeat renewal for short-lived Redis workload leases.

Leases are taken with a short TTL (``settings.data_fetch_lock_timeout``) and
renewed by a daemon thread for as long as the holder runs. If the worker
process dies (OOM kill, container restart) the thread dies with it and the
lease expires within one TTL instead of blocking the market for hours.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import threading

from ..config import settings

logger = logging.getLogger(__name__)

DEFAULT_LEASE_TTL_SECONDS = 300
# Lease clients must never block indefinitely: one stalled call would stall
# every lease renewed on the same heartbeat thread. Three renewals at the
# worst case (connect + read) stay far below the default 100 s interval.
LEASE_REDIS_CONNECT_TIMEOUT_SECONDS = 2
LEASE_REDIS_SOCKET_TIMEOUT_SECONDS = 5

# Refresh the TTL only while the lease still names this holder, so a lease
# that expired and was taken by another task is never extended by us.
RENEW_LEASE_LUA = """
local val = redis.call('get', KEYS[1])
if val and string.find(val, ARGV[1], 1, true) then
    return redis.call('expire', KEYS[1], tonumber(ARGV[2]))
end
return 0
"""


def lease_ttl_seconds(value: object = None) -> int:
    """The configured lease TTL, or the default for a missing/invalid value."""
    if value is None:
        value = getattr(settings, "data_fetch_lock_timeout", DEFAULT_LEASE_TTL_SECONDS)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return DEFAULT_LEASE_TTL_SECONDS
    return int(value)


def lease_renew_interval_seconds() -> float:
    """Renew three times per TTL so one missed beat never drops the lease.

    No lower bound: even a one-second TTL must be renewed before it expires.
    """
    return lease_ttl_seconds() / 3


class LeaseNotHeld(RuntimeError):
    """A lease the task was about to rely on is already gone on entry.

    Raised before the task body starts, so the body never runs unserialized.
    Callers treat it like a busy lease and wait/retry.
    """


@contextmanager
def keep_leases_alive(
    renewals: Sequence[tuple[str, Callable[[], bool]]],
    *,
    interval_seconds: float | None = None,
) -> Iterator[threading.Event]:
    """Run each ``(name, renew)`` every interval until the block exits.

    ``renew`` returns whether the lease is still held. Every lease is renewed
    once before the block runs; if one is already gone then, or its renewal
    raises, ``LeaseNotHeld`` is raised and the block never starts. Once
    running, a lost lease is dropped and logged, and the yielded event is set
    to signal the loss to the task. The task is not interrupted here; commits
    are rejected by ``workload_fence`` instead, which also covers a write
    already in flight. Redis errors are logged and retried on the next beat.
    """
    lost_event = threading.Event()
    if not renewals:
        yield lost_event
        return

    interval = (
        lease_renew_interval_seconds() if interval_seconds is None else interval_seconds
    )
    stop = threading.Event()
    active = list(renewals)

    def renew_all(*, errors_lose_lease: bool = False) -> list[str]:
        lost: list[str] = []
        for entry in tuple(active):
            name, renew = entry
            try:
                held = renew()
            except Exception:
                logger.warning("Lease renewal failed for %s", name, exc_info=True)
                if not errors_lose_lease:
                    continue
                held = False
            if not held:
                logger.error(
                    "Lease %s is no longer held by this task; stopped renewing",
                    name,
                )
                active.remove(entry)
                lost.append(name)
        return lost

    def beat() -> None:
        while active and not stop.wait(interval):
            if renew_all():
                lost_event.set()

    # Renew once before the body starts: a lease reused by a retried task
    # (same id) may have less than one interval left and would otherwise
    # expire before the first beat. If it already expired and was taken by
    # another task, or the renewal cannot be confirmed at all (e.g. a Redis
    # timeout), the body must not start: this check fails closed.
    lost = renew_all(errors_lose_lease=True)
    if lost:
        raise LeaseNotHeld(", ".join(lost))
    thread = threading.Thread(target=beat, name="lease-renewal", daemon=True)
    thread.start()
    try:
        yield lost_event
    finally:
        stop.set()
        thread.join(timeout=5)


# The running task's lease-lost event while it holds external_fetch_global.
# Worker threads do not inherit it, so they pace on their market's share.
_EXTERNAL_FETCH_LEASE_LOST: ContextVar[threading.Event | None] = ContextVar(
    "external_fetch_lease_lost",
    default=None,
)


@contextmanager
def holding_external_fetch_lease(lease_lost: threading.Event) -> Iterator[None]:
    """Mark the block as holding external_fetch_global until it exits or
    ``lease_lost`` is set."""
    token = _EXTERNAL_FETCH_LEASE_LOST.set(lease_lost)
    try:
        yield
    finally:
        _EXTERNAL_FETCH_LEASE_LOST.reset(token)


def holds_external_fetch_lease() -> bool:
    lease_lost = _EXTERNAL_FETCH_LEASE_LOST.get()
    return lease_lost is not None and not lease_lost.is_set()
