"""Blocking endpoint work must not stall other requests on the same worker (#494).

Each case swaps the endpoint's synchronous service for a fake that parks on
a gate. While it is parked, ``/livez`` must still answer. A handler that runs
its blocking body on the event loop stalls the loop inside the fake, so
``/livez`` cannot answer until a watchdog thread opens the gate, and the test
fails instead of hanging.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
import threading
from datetime import datetime, timezone

import httpx
import pandas as pd
import pytest
import pytest_asyncio
from fastapi.routing import APIRoute

from app.api.v1 import cache as cache_module
from app.api.v1 import scans as scans_module
from app.api.v1 import stocks as stocks_module
from app.main import app
from app.services import server_auth
from app.services.ui_snapshot_service import SnapshotResult
from app.wiring.runtime_context import current_runtime_services

pytestmark = pytest.mark.integration

_WATCHDOG_SECONDS = 5.0


class _Gate:
    """Parks the calling thread until released; the watchdog bounds the wait."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.runtime_seen = None

    def block(self) -> None:
        self.runtime_seen = current_runtime_services()
        self.entered.set()
        self.release.wait(_WATCHDOG_SECONDS * 2)


def _frame(days: int = 30) -> pd.DataFrame:
    index = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=days, freq="D", name="Date")
    return pd.DataFrame(
        {
            "Open": [100.0] * days,
            "High": [101.0] * days,
            "Low": [99.0] * days,
            "Close": [100.5] * days,
            "Volume": [1_000_000] * days,
        },
        index=index,
    )


class _PriceCache:
    def __init__(self, gate: _Gate) -> None:
        self.gate = gate

    def get_cached_only(self, symbol, period="2y"):
        self.gate.block()
        return _frame()

    def get_many_cached_only(self, symbols, period="2y"):
        self.gate.block()
        return {sym: _frame() for sym in symbols}


class _FundamentalsCache:
    def __init__(self, gate: _Gate) -> None:
        self.gate = gate

    def get_many(self, symbols):
        self.gate.block()
        return {sym: {"pe_ratio": 20.0} for sym in symbols}


class _YFinance:
    def __init__(self, gate: _Gate) -> None:
        self.gate = gate

    def get_stock_info(self, symbol):
        self.gate.block()


class _SnapshotService:
    def __init__(self, gate: _Gate) -> None:
        self.gate = gate

    def get_scan_bootstrap(self, scan_id, market=None):
        self.gate.block()

    def get_breadth_bootstrap(self, market=None):
        self.gate.block()
        return SnapshotResult(
            snapshot_revision="breadth-r1",
            source_revision="breadth-s1",
            published_at=datetime(2026, 10, 28, tzinfo=timezone.utc),
            is_stale=False,
            payload={"market": market},
        )


def _patch_price_cache(monkeypatch, gate):
    monkeypatch.setattr(stocks_module, "get_price_cache", lambda: _PriceCache(gate))


def _patch_fundamentals(monkeypatch, gate):
    monkeypatch.setattr(stocks_module, "get_fundamentals_cache", lambda: _FundamentalsCache(gate))


def _patch_stock_yfinance(monkeypatch, gate):
    monkeypatch.setattr(stocks_module, "_get_yfinance_service", lambda: _YFinance(gate))


def _patch_snapshot(monkeypatch, gate):
    app.dependency_overrides[scans_module.get_ui_snapshot_service] = lambda: _SnapshotService(gate)


def _patch_refresh(monkeypatch, gate):
    def _queue(mode, market=None):
        gate.block()
        raise ValueError("refresh rejected by test")

    monkeypatch.setattr(cache_module, "_queue_manual_smart_refresh", _queue)


# (id, patch, method, path, json body, expected status after release)
_CASES = [
    ("stock-info", _patch_stock_yfinance, "GET", "/api/v1/stocks/AAPL/info", None, 404),
    ("history", _patch_price_cache, "GET", "/api/v1/stocks/AAPL/history", None, 200),
    (
        "history-batch",
        _patch_price_cache,
        "POST",
        "/api/v1/stocks/history/batch",
        {"symbols": ["AAPL", "MSFT"]},
        200,
    ),
    (
        "fundamentals-batch",
        _patch_fundamentals,
        "POST",
        "/api/v1/stocks/fundamentals/batch",
        {"symbols": ["AAPL"]},
        200,
    ),
    ("scan-bootstrap", _patch_snapshot, "GET", "/api/v1/scans/bootstrap", None, 404),
    ("breadth-bootstrap", _patch_snapshot, "GET", "/api/v1/breadth/bootstrap?market=US", None, 200),
    (
        "scan-refresh-cache",
        _patch_refresh,
        "POST",
        "/api/v1/scans/refresh-cache",
        {"market": "US"},
        400,
    ),
]


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _disable_server_auth(monkeypatch):
    monkeypatch.setattr(server_auth.settings, "server_auth_enabled", False)
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("patch", "method", "path", "body", "expected_status"),
    [case[1:] for case in _CASES],
    ids=[case[0] for case in _CASES],
)
async def test_blocking_endpoint_does_not_stall_other_requests(
    client, monkeypatch, patch, method, path, body, expected_status
):
    runtime = object()
    monkeypatch.setattr(app.state, "runtime_services", runtime, raising=False)
    gate = _Gate()
    patch(monkeypatch, gate)

    watchdog = threading.Timer(_WATCHDOG_SECONDS, gate.release.set)
    watchdog.start()
    try:
        slow = asyncio.create_task(client.request(method, path, json=body))
        assert await asyncio.to_thread(gate.entered.wait, _WATCHDOG_SECONDS)

        quick = await client.get("/livez")
        released_before_quick = gate.release.is_set()

        gate.release.set()
        slow_response = await slow
    finally:
        gate.release.set()
        watchdog.cancel()

    assert quick.status_code == 200
    assert not released_before_quick, f"{path} blocked the event loop until the watchdog fired"
    assert slow_response.status_code == expected_status, slow_response.text
    # The offloaded body still sees the request's runtime services.
    assert gate.runtime_seen is runtime


def test_route_modules_have_no_async_endpoints():
    """New handlers in these modules call synchronous services; keep them `def`."""
    for module in (stocks_module, scans_module):
        offenders = [
            route.path
            for route in module.router.routes
            if inspect.iscoroutinefunction(getattr(route, "endpoint", None))
        ]
        assert offenders == [], f"{module.__name__} async endpoints: {offenders}"


def _awaits_in_own_body(handler: ast.AsyncFunctionDef) -> bool:
    """Nested defs, lambdas and classes are separate scopes, so their awaits do not count."""
    pending = list(handler.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
            return True
        if isinstance(node, ast.comprehension) and node.is_async:
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        pending.extend(ast.iter_child_nodes(node))
    return False


# Handlers that stay `async def` even though they never await. A probe must answer
# without waiting for a threadpool token, so it cannot run in the threadpool, and
# its body does no I/O, so it cannot stall the loop either. A trivial constant
# handler such as `root` has no such need and belongs in the threadpool with the rest.
_LOOP_ONLY_ENDPOINTS = {
    "app.main:liveness",
}


def test_async_endpoints_await_in_their_own_body():
    """An `async def` handler that never awaits runs its whole body on the event loop."""
    offenders = set()
    for route in app.routes:
        if not isinstance(route, APIRoute) or not inspect.iscoroutinefunction(route.endpoint):
            continue
        handler = ast.parse(textwrap.dedent(inspect.getsource(route.endpoint))).body[0]
        if not _awaits_in_own_body(handler):
            offenders.add(f"{route.endpoint.__module__}:{route.endpoint.__name__}")
    stale_exemptions = _LOOP_ONLY_ENDPOINTS - offenders
    assert not stale_exemptions, f"drop exemptions for handlers that no longer need them: {stale_exemptions}"
    offenders -= _LOOP_ONLY_ENDPOINTS
    assert not offenders, (
        "These `async def` endpoints never await, so FastAPI runs their blocking body on the "
        "event loop and every other request on the worker waits. Declare them with plain "
        "`def` so FastAPI runs them in the threadpool:\n  " + "\n  ".join(sorted(offenders))
    )
