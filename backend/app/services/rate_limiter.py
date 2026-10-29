"""
Distributed rate limiter using Redis atomic operations.

Provides a single, unified rate limiting mechanism for all external API calls
(yfinance, finviz, etc.) across all Celery workers and processes.

Falls back to in-process limiter when Redis is unavailable, with automatic
recovery when the connection restores.
"""
import logging
import random
import threading
import time
from typing import Dict, Optional, Tuple

from ..config import settings

logger = logging.getLogger(__name__)


class RateLimitTimeoutError(Exception):
    """Raised when rate limit wait would exceed the specified timeout."""
    pass


# Lua script for atomic rate limiting using Redis server time.
# KEYS[1] queues: a caller reserves its next free slot, even a future one.
# KEYS[2..] gate: while one of them is not open yet, nothing is reserved and
# the caller retries after the returned delay, so a market waiting out its
# own share never holds slots on the queue key that other callers could use.
# Once every gate is open, the slot is the queue key's next-allowed time (or
# now) and each key advances from it by its own interval.
# ARGV[i] = min_interval_s for KEYS[i] (float, seconds between calls)
# ARGV[#KEYS + 1] = longest wait the caller accepts; a slot further out is
# not reserved, so a caller that will time out leaves no slot behind.
# Returns: {reserved ('1' or '0'), seconds to wait} as strings
_LUA_RATE_LIMIT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local gate = now
for i = 2, #KEYS do
    local next_allowed = tonumber(redis.call('GET', KEYS[i]) or 0)
    if next_allowed > gate then
        gate = next_allowed
    end
end
if gate > now then
    return {'0', tostring(gate - now)}
end

local slot = math.max(now, tonumber(redis.call('GET', KEYS[1]) or 0))
if slot - now > tonumber(ARGV[#KEYS + 1]) then
    return {'0', tostring(slot - now)}
end
for i, key in ipairs(KEYS) do
    local interval = tonumber(ARGV[i])
    local ttl = math.ceil(slot - now + interval * 3 + 10)
    redis.call('SET', key, tostring(slot + interval), 'EX', ttl)
end
return {'1', tostring(slot - now)}
"""


class RateLimiterUnavailable(RuntimeError):
    """Strict callers refuse to proceed without the distributed limiter."""


class RedisRateLimiter:
    """
    Distributed rate limiter using Redis atomic operations.

    Falls back to in-process limiter when Redis is unavailable.
    Automatically recovers to Redis when connection restores.
    """

    def __init__(self, jitter_ms: int = 50, redis_retry_interval: float = 30.0):
        """
        Initialize the rate limiter.

        Args:
            jitter_ms: Maximum jitter in milliseconds to add when waiting.
                       Prevents thundering herd when multiple workers wake.
            redis_retry_interval: Seconds between Redis reconnection attempts
                                  when in fallback mode.
        """
        self._jitter_ms = jitter_ms
        self._redis_retry_interval = redis_retry_interval

        # Fallback state
        self._fallback_lock = threading.Lock()
        self._next_allowed: Dict[str, float] = {}
        self._using_fallback = False
        self._last_redis_retry = 0.0
        self._fallback_warned = False

    def _get_redis_client(self):
        """Get Redis client, returning None if unavailable."""
        if not getattr(settings, "redis_enabled", True):
            return None
        try:
            from .redis_pool import get_redis_client
            client = get_redis_client()
            if client:
                client.ping()
                return client
        except Exception:
            pass
        return None

    def _try_redis(
        self, intervals: Dict[str, float], max_wait_s: float
    ) -> Optional[Tuple[bool, float]]:
        """
        Try to execute rate limiting via Redis.

        Returns:
            ``(reserved, seconds to wait)``, or None if Redis is unavailable.
        """
        # If we're in fallback mode, check if it's time to retry Redis
        if self._using_fallback:
            now = time.monotonic()
            if now - self._last_redis_retry < self._redis_retry_interval:
                return None
            self._last_redis_retry = now

        client = self._get_redis_client()
        if client is None:
            if not self._using_fallback:
                self._using_fallback = True
                self._fallback_warned = False
            return None

        try:
            redis_keys = [f"ratelimit:{key}" for key in intervals]
            reserved, wait_time = client.eval(
                _LUA_RATE_LIMIT,
                len(redis_keys),
                *redis_keys,
                *(str(interval) for interval in intervals.values()),
                str(max_wait_s),
            )

            # If we were in fallback, we've recovered
            if self._using_fallback:
                self._using_fallback = False
                self._fallback_warned = False
                logger.info("Rate limiter: recovered Redis connection, switching back from fallback")

            # results are bytes from Redis
            if isinstance(reserved, bytes):
                reserved, wait_time = reserved.decode(), wait_time.decode()
            return reserved == "1", float(wait_time)

        except Exception as e:
            logger.warning(f"Rate limiter: Redis error, falling back to in-process: {e}")
            self._using_fallback = True
            self._last_redis_retry = time.monotonic()
            return None

    def _fallback_wait(
        self, intervals: Dict[str, float], max_wait_s: float
    ) -> Tuple[bool, float]:
        """
        In-process fallback rate limiter using threading.Lock, with the same
        queue and gate rules as the Lua script.

        Returns:
            ``(reserved, seconds to wait)``.
        """
        if not self._fallback_warned:
            logger.warning(
                f"Rate limiter: using in-process fallback for {list(intervals)} "
                f"(Redis unavailable, not distributed)"
            )
            self._fallback_warned = True

        queue_key, *gate_keys = intervals
        with self._fallback_lock:
            now = time.monotonic()
            gate = max([now, *(self._next_allowed.get(key, 0.0) for key in gate_keys)])
            if gate > now:
                return False, gate - now
            slot = max(now, self._next_allowed.get(queue_key, 0.0))
            if slot - now > max_wait_s:
                return False, slot - now
            for key, interval in intervals.items():
                self._next_allowed[key] = slot + interval
            return True, slot - now

    def wait(
        self,
        key: str,
        min_interval_s: float,
        timeout_s: float = 120.0,
        *,
        strict: bool = False,
    ) -> float:
        """
        Block until rate limit allows the call. Returns actual time waited.

        Args:
            key: Rate limit key (e.g., "yfinance", "finviz", "yfinance:batch",
                or per-market like "yfinance:hk" — see RateBudgetPolicy)
            min_interval_s: Minimum seconds between calls for this key
            timeout_s: Maximum seconds to wait before raising RateLimitTimeoutError

        Returns:
            Actual time waited in seconds

        Raises:
            RateLimitTimeoutError: If wait would exceed timeout_s
            RateLimiterUnavailable: If ``strict`` and Redis is unavailable.
                Strict callers (company-exposure research) must share the
                distributed budget with every other caller of the same key
                rather than pace themselves with a process-local limiter.
        """
        return self.wait_all({key: min_interval_s}, timeout_s=timeout_s, strict=strict)

    def wait_all(
        self,
        intervals: Dict[str, float],
        timeout_s: float = 120.0,
        *,
        strict: bool = False,
    ) -> float:
        """Block until one call slot satisfies every ``key: min_interval_s``
        in ``intervals``. The first key queues callers; the rest gate them
        (see ``_LUA_RATE_LIMIT``). ``timeout_s`` bounds the whole wait. Same
        returns and raises as ``wait``."""
        waited = 0.0
        while True:
            result = self._try_redis(intervals, timeout_s - waited)

            if result is None and strict:
                raise RateLimiterUnavailable(
                    f"distributed rate limiter unavailable for {list(intervals)}"
                )

            if result is None:
                # Redis unavailable — use fallback
                result = self._fallback_wait(intervals, timeout_s - waited)
                backend = "local"
            else:
                backend = "redis"
            reserved, wait_time = result

            # Check timeout before sleeping
            if waited + wait_time > timeout_s:
                raise RateLimitTimeoutError(
                    f"Rate limit wait for {list(intervals)} would be "
                    f"{waited + wait_time:.1f}s, exceeding timeout of {timeout_s:.1f}s"
                )

            if wait_time > 0:
                # Add jitter to prevent thundering herd
                total_wait = wait_time + random.uniform(0, self._jitter_ms / 1000.0)
                time.sleep(total_wait)
                waited += total_wait

            if reserved:
                logger.debug(
                    f"Rate limit: keys={list(intervals)} waited={waited:.3f}s backend={backend}"
                )
                return waited

    def wait_for_market(
        self,
        provider: str,
        market: Optional[str],
        timeout_s: float = 120.0,
    ) -> float:
        """Per-market convenience wrapper.

        Queues on the provider's unsuffixed key at its global interval, so
        every caller of the provider shares one global budget. When
        RateBudgetPolicy resolves a slower interval for the market (its
        universe-weighted share or an override), the provider×market key at
        that interval gates the call too. Records throttle telemetry when
        the call actually slept. This is the preferred API for new code; raw
        ``wait()`` remains for shared/legacy keys.
        """
        from .rate_budget_policy import get_rate_budget_policy

        policy = get_rate_budget_policy()
        global_interval = policy.get_rate_interval(provider, None)
        intervals = {provider: global_interval}
        market_interval = policy.get_rate_interval(provider, market)
        if market_interval > global_interval:
            intervals[policy.provider_key(provider, market)] = market_interval
        waited = self.wait_all(intervals, timeout_s=timeout_s)
        if waited > 0:
            policy.record_throttle_wait(provider, market, waited)
        return waited
