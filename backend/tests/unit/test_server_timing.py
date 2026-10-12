"""Server-Timing header: wall time and database time per request."""

import asyncio
import re

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

from app.infra.server_timing import ServerTimingMiddleware, install_server_timing

HEADER = re.compile(
    r'^db;dur=(?P<db>[\d.]+);desc="(?P<queries>\d+) quer(?:y|ies)", total;dur=(?P<total>[\d.]+)$'
)
# A streamed body runs after the headers, so only what came before is known.
STREAMED = re.compile(
    r'^db;dur=(?P<db>[\d.]+);desc="(?P<queries>\d+) quer(?:y|ies) before streaming", '
    r'headers;dur=(?P<total>[\d.]+)$'
)


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

    @app.get("/stream-queries")
    def stream_queries_route():
        def rows():
            run(3)  # all the database work happens after the headers
            yield b"done"

        return StreamingResponse(rows(), media_type="text/plain")

    @app.get("/empty")
    def empty_route():
        run(1)
        return Response(status_code=204)

    @app.get("/crash")
    def crash_route():
        run(1)
        raise RuntimeError("unhandled")

    install_server_timing(app)
    return app


def _timing(response, pattern=HEADER):
    match = pattern.match(response.headers["server-timing"])
    assert match, response.headers["server-timing"]
    return int(match["queries"]), float(match["db"]), float(match["total"])


@pytest.fixture
def client():
    engine = _engine()
    # Unhandled errors come back as the 500 a real server would send.
    transport = httpx.ASGITransport(app=_app(engine), raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_reports_queries_run_in_the_endpoints_worker_thread(client):
    async with client:
        response = await client.get("/sync/3")

    queries, db_ms, total_ms = _timing(response)
    assert queries == 3
    assert 0 <= db_ms <= total_ms


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
async def test_one_statement_reads_as_one_query(client):
    async with client:
        response = await client.get("/sync/1")

    assert 'desc="1 query"' in response.headers["server-timing"]


@pytest.mark.asyncio
async def test_an_unhandled_error_still_reports_its_timing(client):
    # Starlette builds the 500 outside every add_middleware layer, so the
    # slowest failures (a pool timeout) would otherwise go untimed.
    async with client:
        response = await client.get("/crash")

    assert response.status_code == 500
    assert _timing(response)[0] == 1


@pytest.mark.asyncio
async def test_streaming_responses_get_the_header_before_the_body(client):
    async with client:
        response = await client.get("/stream")

    assert response.text == "ab"
    assert _timing(response, STREAMED)[0] == 1


@pytest.mark.asyncio
async def test_a_stream_never_passes_for_a_complete_count(client):
    # /content/export runs every query inside its generator: a plain
    # "0 queries, total" would read as a cheap request.
    async with client:
        response = await client.get("/stream-queries")

    assert response.text == "done"
    assert _timing(response, STREAMED)[0] == 0


@pytest.mark.asyncio
async def test_an_empty_response_is_complete_not_streamed(client):
    async with client:
        response = await client.get("/empty")

    assert response.status_code == 204
    assert _timing(response)[0] == 1


def test_queries_outside_a_request_run_normally():
    # Celery tasks and scripts that import the app have no request context.
    engine = _engine()
    with engine.connect() as conn:
        assert conn.execute(text("select 1")).scalar() == 1


@pytest.mark.asyncio
async def test_the_api_reports_server_timing():
    from app.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/livez")

    assert response.status_code == 200
    assert _timing(response)[0] == 0


def test_the_api_times_outside_its_error_handling():
    from app.main import app

    assert isinstance(app.build_middleware_stack(), ServerTimingMiddleware)
