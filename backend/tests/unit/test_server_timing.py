"""Server-Timing header: wall time and database time per request."""

import asyncio
import re

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

from app.infra.server_timing import ServerTimingMiddleware

HEADER = re.compile(r'^db;dur=(?P<db>[\d.]+);desc="(?P<queries>\d+) queries", app;dur=(?P<app>[\d.]+)$')


def _engine():
    return create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )


def _app(engine):
    app = FastAPI()

    def run(count):
        with engine.connect() as conn:
            for _ in range(count):
                conn.execute(text("select 1"))

    @app.get("/sync/{count}")
    def sync_route(count: int):  # FastAPI runs this in a worker thread
        run(count)
        return {"ok": True}

    @app.get("/async/{count}")
    async def async_route(count: int):
        # Interleave with other requests before querying.
        await asyncio.sleep(0.01)
        run(count)
        await asyncio.sleep(0.01)
        return {"ok": True}

    @app.get("/broken")
    def broken_route():
        with engine.connect() as conn:
            with pytest.raises(OperationalError):
                conn.execute(text("select * from no_such_table"))
            conn.execute(text("select 1"))
        return {"ok": True}

    @app.get("/fetch-fails")
    def fetch_fails_route():
        calls = []

        def fail_on_second_row(value):
            calls.append(value)
            if len(calls) > 1:
                raise ValueError("second row")
            return value

        with engine.connect() as conn:
            conn.connection.driver_connection.create_function("fail_on_second_row", 1, fail_on_second_row)
            # Executes fine (first row); fetching the second row raises.
            with pytest.raises(OperationalError):
                conn.execute(text("select fail_on_second_row(v) from (select 1 as v union all select 2)")).all()
        return {"ok": True}

    @app.get("/stream")
    def stream_route():
        run(1)
        return StreamingResponse(iter([b"a", b"b"]), media_type="text/plain")

    app.add_middleware(ServerTimingMiddleware)
    return app


def _timing(response):
    match = HEADER.match(response.headers["server-timing"])
    assert match, response.headers["server-timing"]
    return int(match["queries"]), float(match["db"]), float(match["app"])


@pytest.fixture
def client():
    engine = _engine()
    transport = httpx.ASGITransport(app=_app(engine))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_reports_queries_run_in_the_endpoints_worker_thread(client):
    async with client:
        response = await client.get("/sync/3")

    queries, db_ms, app_ms = _timing(response)
    assert queries == 3
    assert 0 <= db_ms <= app_ms


@pytest.mark.asyncio
async def test_concurrent_requests_count_only_their_own_queries(client):
    async with client:
        responses = await asyncio.gather(*(client.get(f"/async/{n}") for n in (1, 4, 2, 5)))

    assert [_timing(r)[0] for r in responses] == [1, 4, 2, 5]


@pytest.mark.asyncio
async def test_a_failed_query_still_counts_and_breaks_nothing(client):
    async with client:
        response = await client.get("/broken")

    assert response.status_code == 200
    assert _timing(response)[0] == 2


@pytest.mark.asyncio
async def test_a_statement_whose_fetch_fails_counts_once(client):
    async with client:
        response = await client.get("/fetch-fails")

    assert response.status_code == 200
    assert _timing(response)[0] == 1


@pytest.mark.asyncio
async def test_streaming_responses_get_the_header_before_the_body(client):
    async with client:
        response = await client.get("/stream")

    assert response.text == "ab"
    assert _timing(response)[0] == 1


def test_queries_outside_a_request_are_not_counted():
    engine = _engine()
    with engine.connect() as conn:
        conn.execute(text("select 1"))  # must not raise without a request


@pytest.mark.asyncio
async def test_the_api_reports_server_timing():
    from app.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/livez")

    assert response.status_code == 200
    assert _timing(response)[0] == 0
