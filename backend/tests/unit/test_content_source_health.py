"""Content sources report fetch failures, and their watermark keeps late items."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import feedparser
import pytest
import requests
from sqlalchemy import select

from app.models.theme import ContentItem, ContentSource
from app.services import content_ingestion_service as ingestion
from app.services.content_ingestion_service import ContentIngestionService

NOW = datetime.now(timezone.utc)
_parse = feedparser.parse


def _rss(*published: datetime) -> bytes:
    items = "".join(
        f"<item><title>Post {index}</title><link>https://e.com/{index}</link><guid>{index}</guid>"
        f"<pubDate>{format_datetime(moment, usegmt=True)}</pubDate>"
        "<description>Memory demand is accelerating.</description></item>"
        for index, moment in enumerate(published)
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>F</title>{items}</channel></rss>'.encode()


def _serve(monkeypatch, *, status=200, body=b"", payload=None):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))

        def raise_for_status():
            if status >= 400:
                raise requests.HTTPError(f"{status} Server Error for url: {url}")

        def json():
            if payload is None:
                raise ValueError("Expecting value: line 1 column 1 (char 0)")
            return payload

        return SimpleNamespace(status_code=status, content=body, text=body.decode(),
                               raise_for_status=raise_for_status, json=json)

    monkeypatch.setattr(ingestion.requests, "get", get)
    monkeypatch.setattr(ingestion.feedparser, "parse", lambda *_args, **_kwargs: _parse(body))
    return calls


def _source(db_session, **values):
    source = ContentSource(name="Feed", source_type="rss", url="https://e.com/feed",
                           pipelines=["technical"], **values)
    db_session.add(source)
    db_session.commit()
    return source


def test_feed_http_error_is_recorded_and_reported(db_session, monkeypatch):
    _serve(monkeypatch, status=503, body=b"<html>Service Unavailable</html>")
    source = _source(db_session)

    with pytest.raises(Exception, match="503"):
        ContentIngestionService(db_session).fetch_source(source)

    db_session.expire_all()
    stored = db_session.get(ContentSource, source.id)
    assert (stored.consecutive_failures, stored.last_success_at, stored.last_fetched_at) == (1, None, None)
    assert "503" in stored.last_error


def test_successful_fetch_clears_the_failure_streak(db_session, monkeypatch):
    _serve(monkeypatch, body=_rss(NOW - timedelta(hours=1)))
    source = _source(db_session, consecutive_failures=2, last_error="503 Server Error")

    assert ContentIngestionService(db_session).fetch_source(source) == 1

    db_session.expire_all()
    stored = db_session.get(ContentSource, source.id)
    assert stored.consecutive_failures == 0
    assert stored.last_success_at is not None


def test_feed_item_that_appears_after_the_last_poll_is_still_ingested(db_session, monkeypatch):
    # Published before the last poll, but absent from the feed until after it.
    _serve(monkeypatch, body=_rss(NOW - timedelta(hours=1)))
    polled = NOW - timedelta(minutes=30)
    source = _source(db_session, last_fetched_at=polled, last_success_at=polled)

    assert ContentIngestionService(db_session).fetch_source(source) == 1
    assert db_session.scalar(select(ContentItem.url)) == "https://e.com/0"


def test_feed_request_is_bounded_by_a_timeout(db_session, monkeypatch):
    calls = _serve(monkeypatch, body=_rss(NOW))
    source = _source(db_session)

    ContentIngestionService(db_session).fetch_source(source)

    assert [(url, kwargs.get("timeout", 0) > 0) for url, kwargs in calls] == [("https://e.com/feed", True)]


def test_reddit_block_page_is_a_failure_not_an_empty_listing(db_session, monkeypatch):
    _serve(monkeypatch, body=b"<html>blocked</html>")
    source = _source(db_session)
    source.source_type, source.url = "reddit", "https://reddit.com/r/stocks"
    db_session.commit()

    with pytest.raises(Exception):
        ContentIngestionService(db_session).fetch_source(source)

    db_session.expire_all()
    assert db_session.get(ContentSource, source.id).consecutive_failures == 1


def test_failed_fetch_waits_its_interval_but_keeps_the_watermark(db_session, monkeypatch):
    _serve(monkeypatch, status=429, body=b"Too Many Requests")
    succeeded = NOW - timedelta(hours=3)
    source = _source(db_session, last_fetched_at=succeeded, last_success_at=succeeded)

    with pytest.raises(Exception, match="429"):
        ContentIngestionService(db_session).fetch_source(source)

    db_session.expire_all()
    stored = db_session.get(ContentSource, source.id)
    assert ingestion._coerce_utc_datetime(stored.last_success_at) == succeeded
    assert ingestion._coerce_utc_datetime(stored.last_fetched_at) > succeeded


def test_failure_while_storing_items_counts_against_the_source(db_session, monkeypatch):
    _serve(monkeypatch, body=_rss(NOW - timedelta(hours=1)))
    def broken(*_args, **_kwargs):
        raise RuntimeError("admission store unavailable")
    monkeypatch.setattr(ContentIngestionService, "_admit_economic_evidence", broken)
    source = _source(db_session)

    with pytest.raises(RuntimeError, match="admission store unavailable"):
        ContentIngestionService(db_session).fetch_source(source)

    db_session.expire_all()
    stored = db_session.get(ContentSource, source.id)
    assert (stored.consecutive_failures, stored.last_success_at) == (1, None)


def test_corruption_reset_rewind_also_rewinds_the_watermark(db_session):
    from app.services.theme_content_recovery_service import rewind_theme_content_source_cursors

    source = _source(db_session, last_fetched_at=NOW, last_success_at=NOW)

    rewind_theme_content_source_cursors(db_session.connection())
    db_session.commit()

    db_session.expire_all()
    stored = db_session.get(ContentSource, source.id)
    assert ingestion._coerce_utc_datetime(stored.last_success_at) < NOW - timedelta(days=300)


@pytest.mark.asyncio
async def test_source_list_reports_fetch_health(db_session, monkeypatch):
    import httpx

    from app.database import get_db
    from app.main import app
    from app.services import server_auth

    monkeypatch.setattr(server_auth.settings, "server_auth_enabled", False)
    _source(db_session, consecutive_failures=3, last_error="HTTPError: 503 Server Error",
            last_error_at=NOW, last_success_at=NOW - timedelta(days=2))
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/themes/sources")
    finally:
        app.dependency_overrides.pop(get_db, None)

    [source] = response.json()
    assert (source["consecutive_failures"], source["last_error"]) == (3, "HTTPError: 503 Server Error")
    assert source["last_success_at"] is not None


def test_reddit_reads_the_newest_posts_with_a_timeout(db_session, monkeypatch):
    post = {"id": "p1", "title": "HBM", "created_utc": (NOW - timedelta(hours=3)).timestamp(),
            "edited": False, "permalink": "/r/stocks/p1"}
    calls = _serve(monkeypatch, payload={"data": {"children": [{"data": post}]}})
    polled = NOW - timedelta(hours=1)
    source = _source(db_session, last_fetched_at=polled, last_success_at=polled)
    source.source_type, source.url = "reddit", "https://reddit.com/r/stocks"
    db_session.commit()

    assert ContentIngestionService(db_session).fetch_source(source) == 1
    assert [(url, kwargs.get("timeout", 0) > 0) for url, kwargs in calls] == [
        ("https://www.reddit.com/r/stocks/new.json", True)]
