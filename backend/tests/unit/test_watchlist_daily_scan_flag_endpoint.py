from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from app.main import app
from app.services import server_auth


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setattr(server_auth.settings, "server_auth_enabled", False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_daily_scan_flag_round_trips_through_update_and_list(client):
    created = (await client.post("/api/v1/user-watchlists/", json={"name": "Leaders"})).json()
    assert created["include_in_daily_scan"] is False

    flagged = await client.put(
        f"/api/v1/user-watchlists/{created['id']}", json={"include_in_daily_scan": True}
    )
    assert flagged.status_code == 200
    assert flagged.json()["include_in_daily_scan"] is True

    renamed = await client.put(f"/api/v1/user-watchlists/{created['id']}", json={"name": "Leaders 2"})
    assert renamed.json()["include_in_daily_scan"] is True

    listed = (await client.get("/api/v1/user-watchlists/")).json()["watchlists"]
    assert [(w["name"], w["include_in_daily_scan"]) for w in listed] == [("Leaders 2", True)]

    await client.put(f"/api/v1/user-watchlists/{created['id']}", json={"include_in_daily_scan": False})
    listed = (await client.get("/api/v1/user-watchlists/")).json()["watchlists"]
    assert [w["include_in_daily_scan"] for w in listed] == [False]
