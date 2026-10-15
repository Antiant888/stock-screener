"""#501: polls see corrections to entries published before the last poll."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app.models.economic_taxonomy_runtime import EvidencePacket
from app.models.theme import ContentItem, ContentSource
from app.services.content_ingestion_service import (
    ContentIngestionService,
    RedditFetcher,
    RSSFetcher,
)

SINCE = datetime(2026, 3, 27, 5, 0, tzinfo=timezone.utc)


class _Entry(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _struct(moment: datetime) -> tuple:
    return moment.utctimetuple()[:9]


def _entry(*, link, published, updated, summary="Memory demand is accelerating."):
    return _Entry(
        title="Memory upcycle", link=link, author="Analyst", summary=summary,
        published_parsed=_struct(published), updated_parsed=_struct(updated),
    )


def _response(**fields):
    return SimpleNamespace(raise_for_status=lambda: None, content=b"", headers={}, **fields)


def _feed(monkeypatch, *entries):
    monkeypatch.setattr(
        "app.services.content_ingestion_service.requests.get", lambda *a, **k: _response()
    )
    monkeypatch.setattr(
        "app.services.content_ingestion_service.feedparser.parse",
        lambda content, **_: SimpleNamespace(entries=list(entries)),
    )


def test_rss_entry_updated_since_the_last_poll_is_returned(monkeypatch):
    published = SINCE - timedelta(hours=1)
    _feed(
        monkeypatch,
        _entry(link="https://e.com/corrected", published=published, updated=SINCE + timedelta(hours=1)),
        _entry(link="https://e.com/stale", published=published, updated=SINCE - timedelta(minutes=30)),
    )
    source = ContentSource(name="Feed", source_type="rss", url="https://e.com/feed")

    items = RSSFetcher().fetch(source, since=SINCE)

    assert [item["url"] for item in items] == ["https://e.com/corrected"]
    assert items[0]["published_at"] == published  # still the publish time


def test_reddit_post_edited_since_the_last_poll_is_returned(monkeypatch):
    created = SINCE - timedelta(hours=1)
    posts = [
        {"id": "edited", "title": "DRAM", "created_utc": created.timestamp(),
         "edited": (SINCE + timedelta(hours=1)).timestamp(), "permalink": "/r/x/edited"},
        {"id": "unedited", "title": "HBM", "created_utc": created.timestamp(),
         "edited": False, "permalink": "/r/x/unedited"},
    ]
    monkeypatch.setattr(
        "app.services.content_ingestion_service.requests.get",
        lambda *a, **k: _response(
            json=lambda: {"data": {"children": [{"data": post} for post in posts]}}
        ),
    )
    source = ContentSource(name="r/x", source_type="reddit", url="https://reddit.com/r/x")

    items = RedditFetcher().fetch(source, since=SINCE)

    assert [item["url"] for item in items] == ["https://reddit.com/r/x/edited"]


def test_correction_to_an_older_entry_is_held_on_the_next_ordinary_poll(db_session, monkeypatch):
    now = datetime.now(timezone.utc)
    published = now - timedelta(hours=2)
    source = ContentSource(name="Feed", source_type="rss", url="https://e.com/feed",
                           pipelines=["technical"])
    db_session.add(source)
    db_session.commit()
    _feed(monkeypatch, _entry(link="https://e.com/a", published=published, updated=published))
    ContentIngestionService(db_session).fetch_source(source)

    # The next ordinary poll is after the original publish; the feed now
    # carries a corrected body with the same publish date.
    source.last_fetched_at = now - timedelta(hours=1)
    db_session.commit()
    _feed(monkeypatch, _entry(link="https://e.com/a", published=published,
                              updated=now - timedelta(minutes=30), summary="Memory demand is slowing."))
    ContentIngestionService(db_session).fetch_source(source)

    states = db_session.scalars(
        select(EvidencePacket.precedence_state).order_by(EvidencePacket.evidence_revision_ordinal)
    ).all()
    assert states == ["effective", "hold_review"]
    stored = db_session.scalar(select(ContentItem))
    assert stored.content == "Memory demand is accelerating."  # legacy row unchanged
