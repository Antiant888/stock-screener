"""Run the real rate-limit Lua script on a throwaway redis-server."""
from __future__ import annotations

import shutil
import socket
import subprocess
import time

import pytest
import redis

from app.services.rate_limiter import RateLimitTimeoutError, RedisRateLimiter

pytestmark = pytest.mark.skipif(
    shutil.which("redis-server") is None, reason="redis-server not installed"
)


@pytest.fixture(scope="module")
def client():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = subprocess.Popen(
        ["redis-server", "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL,
    )
    conn = redis.Redis(port=port)
    for _ in range(50):
        try:
            conn.ping()
            break
        except redis.ConnectionError:
            time.sleep(0.1)
    yield conn
    server.terminate()
    server.wait()


@pytest.fixture
def limiter(client):
    client.flushall()
    limiter = RedisRateLimiter(jitter_ms=0)
    limiter._get_redis_client = lambda: client
    return limiter


def test_queue_key_reserves_future_slots(limiter):
    assert limiter._try_redis({"p": 2.0}, 120.0) == (True, 0.0)
    reserved, wait = limiter._try_redis({"p": 2.0}, 120.0)
    assert reserved and wait == pytest.approx(2.0, abs=0.05)


def test_closed_gate_reserves_nothing(limiter, client):
    limiter._try_redis({"p": 2.0, "p:hk": 20.0}, 120.0)
    reserved, wait = limiter._try_redis({"p": 2.0, "p:hk": 20.0}, 120.0)
    assert not reserved and wait == pytest.approx(20.0, abs=0.05)
    assert float(client.get("ratelimit:p")) - time.time() == pytest.approx(2.0, abs=0.05)


def test_slot_past_the_timeout_is_not_reserved(limiter, client):
    for _ in range(3):
        limiter._try_redis({"p": 2.0}, 120.0)
    before = client.get("ratelimit:p")
    with pytest.raises(RateLimitTimeoutError):
        limiter.wait("p", min_interval_s=2.0, timeout_s=1.0)
    assert client.get("ratelimit:p") == before
