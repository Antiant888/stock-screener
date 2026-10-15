"""
Content Ingestion Service for Theme Discovery

Fetches content from multiple sources:
- Substack RSS feeds
- Twitter/X via official X API by default, with pluggable provider override
- News APIs (Benzinga, Seeking Alpha)
- Reddit API
"""
import logging
import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from abc import ABC, abstractmethod

import feedparser
import requests
from sqlalchemy.orm import Session

from ..domain.economic_taxonomy.contracts import EvidenceChannel
from ..infra.db.models.social_signals import ContentPipelineEligibility
from ..models.theme import ContentSource, ContentItem, ContentItemPipelineState
from .economic_source_admission import (
    CONTENT_INGESTION_ROUTE,
    EconomicSourceAdmissionService,
    EvidenceAdmission,
    content_family_key,
    content_route_record_id,
)
from .theme_evidence_eligibility_service import grant_eligibility, is_social_owned_source, legacy_sources, legacy_eligibility_exists
from ..models.app_settings import AppSetting
from ..config import settings
from .language_detection_service import build_detection_text, detect_language
from .theme_pipeline_state_service import normalize_pipelines

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 20
_USER_AGENT = "StockScreener/1.0 (theme discovery)"
# An item can reach a feed after a poll has passed its publish time. Re-reading
# this margin before the watermark costs only duplicates, which dedup drops.
_WATERMARK_OVERLAP = timedelta(days=2)


class SourceFetchError(RuntimeError):
    """A content source could not be read; distinct from a source with nothing new."""


def _coerce_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _twitter_official_since_id_key(source_id: int) -> str:
    from .twitter_ingestion_providers import official_since_id_key

    return official_since_id_key(source_id)


class BaseContentFetcher(ABC):
    """Base class for content fetchers"""

    @abstractmethod
    def fetch(self, source: ContentSource, since: Optional[datetime] = None) -> list[dict]:
        """Fetch content from source, returns list of content items"""
        pass

    def generate_external_id(self, source_type: str, unique_string: str) -> str:
        """Generate a unique external ID for deduplication"""
        return hashlib.md5(f"{source_type}:{unique_string}".encode()).hexdigest()


class RSSFetcher(BaseContentFetcher):
    """Fetches content from RSS/Atom feeds (Substack, blogs, etc.)"""

    def fetch(self, source: ContentSource, since: Optional[datetime] = None) -> list[dict]:
        """Fetch RSS feed items"""
        items = []
        response = requests.get(
            source.url, headers={"User-Agent": _USER_AGENT}, timeout=_REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        feed = feedparser.parse(response.content, response_headers=dict(response.headers))
        if getattr(feed, "bozo", False) and not feed.entries:
            raise SourceFetchError(f"Unreadable feed from {source.url}: {feed.get('bozo_exception')}")

        for entry in feed.entries:
            # Parse published date
            published_at = None
            updated_at = None
            if hasattr(entry, "updated_parsed") and entry.updated_parsed:
                updated_at = _coerce_utc_datetime(datetime(*entry.updated_parsed[:6]))
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                published_at = _coerce_utc_datetime(datetime(*entry.published_parsed[:6]))
            else:
                published_at = updated_at

            # Skip entries unchanged since the last poll. A correction keeps
            # its publish date but bumps <updated>, so it still reaches
            # economic admission (#501).
            since_bound = _coerce_utc_datetime(since)
            last_changed = max(
                (moment for moment in (published_at, updated_at) if moment), default=None
            )
            if since_bound and last_changed and last_changed < since_bound:
                continue

            # Extract content
            content = ""
            if hasattr(entry, "content") and entry.content:
                content = entry.content[0].get("value", "")
            elif hasattr(entry, "summary"):
                content = entry.summary
            elif hasattr(entry, "description"):
                content = entry.description

            # Generate unique ID
            external_id = self.generate_external_id(
                source.source_type,
                entry.get("id", entry.get("link", entry.get("title", "")))
            )

            items.append({
                "external_id": external_id,
                "title": entry.get("title", ""),
                "content": content,
                "url": entry.get("link", ""),
                "author": entry.get("author", source.name),
                "published_at": published_at,
            })

        logger.info(f"Fetched {len(items)} items from RSS feed: {source.name}")
        return items


class TwitterFetcher(BaseContentFetcher):
    """Fetches content from Twitter/X through the configured provider."""

    def __init__(self):
        from .twitter_ingestion_providers import build_twitter_fetcher

        self._provider_fetcher = build_twitter_fetcher()

    def fetch(self, source: ContentSource, since: Optional[datetime] = None) -> list[dict]:
        return self._provider_fetcher.fetch(source, since)


class NewsFetcher(BaseContentFetcher):
    """
    Fetches financial news from various APIs

    Supported sources:
    - Benzinga (requires API key)
    - Seeking Alpha RSS
    - MarketWatch RSS
    - Yahoo Finance RSS
    """

    def __init__(self):
        self.benzinga_api_key = getattr(settings, "benzinga_api_key", None)

    def fetch(self, source: ContentSource, since: Optional[datetime] = None) -> list[dict]:
        """Fetch news based on source URL pattern"""
        url = source.url.lower()

        if "benzinga" in url and self.benzinga_api_key:
            return self._fetch_benzinga(source, since)
        elif "seekingalpha" in url or "seeking-alpha" in url:
            return self._fetch_seekingalpha_rss(source, since)
        else:
            # Default to RSS parsing
            rss_fetcher = RSSFetcher()
            return rss_fetcher.fetch(source, since)

    def _fetch_benzinga(self, source: ContentSource, since: Optional[datetime] = None) -> list[dict]:
        """Fetch from Benzinga API"""
        items = []
        params = {
            "token": self.benzinga_api_key,
            "pageSize": 50,
            "displayOutput": "full",
        }
        if since:
            params["dateFrom"] = since.strftime("%Y-%m-%d")

        response = requests.get(
            "https://api.benzinga.com/api/v2/news",
            params=params,
            headers={"Accept": "application/json"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        news_items = response.json()

        for article in news_items:
            try:
                published_at = _coerce_utc_datetime(
                    datetime.fromisoformat(article.get("created", "").replace("Z", "+00:00"))
                )
            except (AttributeError, ValueError):
                logger.warning("Skipping Benzinga article %s with unreadable date", article.get("id"))
                continue
            external_id = self.generate_external_id("benzinga", str(article.get("id", "")))

            items.append({
                "external_id": external_id,
                "title": article.get("title", ""),
                "content": article.get("body", article.get("teaser", "")),
                "url": article.get("url", ""),
                "author": article.get("author", "Benzinga"),
                "published_at": published_at,
            })

        logger.info(f"Fetched {len(items)} articles from Benzinga")
        return items

    def _fetch_seekingalpha_rss(self, source: ContentSource, since: Optional[datetime] = None) -> list[dict]:
        """Fetch from Seeking Alpha RSS feed"""
        rss_fetcher = RSSFetcher()
        return rss_fetcher.fetch(source, since)


class RedditFetcher(BaseContentFetcher):
    """Fetches content from Reddit via JSON API (no auth needed for public)"""

    def fetch(self, source: ContentSource, since: Optional[datetime] = None) -> list[dict]:
        """Fetch posts from subreddit"""
        items = []
        # Extract subreddit from URL
        # Format: reddit.com/r/wallstreetbets or just /r/wallstreetbets
        parts = source.url.replace("https://", "").replace("http://", "").split("/")
        subreddit = None
        for i, part in enumerate(parts):
            if part == "r" and i + 1 < len(parts):
                subreddit = parts[i + 1]
                break

        if not subreddit:
            raise SourceFetchError(f"Could not extract subreddit from URL: {source.url}")

        # The newest listing is chronological, so the watermark filter below keeps
        # what it should; the hot listing's posts were mostly older than any recent
        # poll and filtered out. Its limit: only the newest 100 posts are seen, so
        # a busy subreddit needs a short interval, and edits further back are missed.
        response = requests.get(
            f"https://www.reddit.com/r/{subreddit}/new.json",
            headers={"User-Agent": _USER_AGENT},
            params={"limit": 100},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise SourceFetchError(f"Reddit returned a non-JSON page for r/{subreddit}") from exc

        for post in data.get("data", {}).get("children", []):
            post_data = post.get("data", {})

            # Convert Unix timestamp
            created_utc = post_data.get("created_utc", 0)
            published_at = _coerce_utc_datetime(datetime.utcfromtimestamp(created_utc))

            # An edited post keeps created_utc; "edited" is its edit time
            # (False if never edited), so edits still reach admission (#501).
            edited = post_data.get("edited")
            last_changed = published_at
            if edited and not isinstance(edited, bool):
                last_changed = max(published_at, datetime.fromtimestamp(edited, tz=timezone.utc))
            since_bound = _coerce_utc_datetime(since)
            if since_bound and last_changed < since_bound:
                continue

            external_id = self.generate_external_id("reddit", post_data.get("id", ""))

            # Combine title and selftext for content
            content = post_data.get("title", "")
            if post_data.get("selftext"):
                content += "\n\n" + post_data.get("selftext", "")

            items.append({
                "external_id": external_id,
                "title": post_data.get("title", ""),
                "content": content,
                "url": f"https://reddit.com{post_data.get('permalink', '')}",
                "author": post_data.get("author", ""),
                "published_at": published_at,
            })

        logger.info(f"Fetched {len(items)} posts from r/{subreddit}")
        return items


class ContentIngestionService:
    """Main service for ingesting content from all sources"""

    def __init__(self, db: Session):
        self.db = db
        self.fetchers = {
            "substack": RSSFetcher(),
            "rss": RSSFetcher(),
            "twitter": TwitterFetcher(),
            "news": NewsFetcher(),
            "reddit": RedditFetcher(),
        }

    def get_fetcher(self, source_type: str) -> BaseContentFetcher:
        """Get appropriate fetcher for source type"""
        return self.fetchers.get(source_type, RSSFetcher())

    def _seed_pipeline_state_rows_for_item(self, content_item: ContentItem, pipelines: list[str]) -> int:
        """Create any missing pending per-pipeline state rows for a content item."""
        existing_pipelines = {
            row[0]
            for row in self.db.query(ContentItemPipelineState.pipeline).filter(
                ContentItemPipelineState.content_item_id == content_item.id,
                ContentItemPipelineState.pipeline.in_(pipelines),
            ).all()
        }

        created = 0
        for pipeline in pipelines:
            if pipeline in existing_pipelines:
                continue
            self.db.add(
                ContentItemPipelineState(
                    content_item_id=content_item.id,
                    pipeline=pipeline,
                    status="pending",
                    attempt_count=0,
                )
            )
            created += 1
        return created

    def _admit_economic_evidence(
        self,
        content_item: ContentItem,
        item_data: dict,
        pipelines: list[str],
        *,
        source_id: int | None,
        source_name: str | None,
        captured_at: datetime | None = None,
    ) -> bool:
        """Admit the fetched text as economic taxonomy evidence; return whether it was.

        Runs on every poll, not just for new items: an unchanged recapture
        reuses its packet, and changed text (a correction, even to empty)
        enters the same lineage, where the precedence policy decides it.
        Admission shares the ingest transaction, so a failure rolls back the
        batch and the next poll retries it.

        X posts join Social's ``x:post:<tweet_id>`` family; Social's later
        capture of the same post supersedes this one (#500). A post with no
        status URL has no tweet id to key on and is skipped.
        """
        family_key = content_family_key(
            content_item.source_type,
            content_item.external_id,
            item_data.get("url") or content_item.url,
        )
        if family_key is None:
            return False
        now = captured_at or datetime.now(timezone.utc)
        title = item_data.get("title") or ""
        content = item_data.get("content") or ""
        # Some fetchers (Reddit) already lead the content with the title.
        if title and not content.startswith(title):
            text = f"{title}\n\n{content}" if content else title
        else:
            text = content
        EconomicSourceAdmissionService(self.db).admit_content(
            EvidenceAdmission(
                provider="x" if content_item.source_type == "twitter" else content_item.source_type,
                canonical_source_family=family_key,
                capture_route=CONTENT_INGESTION_ROUTE,
                # A mirror feed gets its own packet instead of the first feed's.
                route_record_id=content_route_record_id(content_item.id, source_id),
                original_text=text,
                preparation_version="content-ingestion-v1",
                source_metadata={
                    "content_item_id": content_item.id,
                    # The polled source: an item can be shared by two feeds.
                    "content_source_id": source_id,
                    "source_name": source_name,
                    "title": title,
                    "url": item_data.get("url"),
                    "author": item_data.get("author"),
                },
                captured_at=now,
                observed_at=_coerce_utc_datetime(item_data.get("published_at")),
                available_at=now,
                evidence_channels=tuple(
                    p for p in pipelines if p in {c.value for c in EvidenceChannel}
                ),
            )
        )
        return True

    def backfill_economic_evidence(self, *, batch_size: int = 500, after_id: int = 0) -> dict:
        """Admit items ingested before content admission existed (#471, #500).

        Replays each recorded legacy observation (``ContentPipelineEligibility``):
        one capture per observing source, with the pipelines that source granted,
        dated by its first grant, so evidence is never available earlier than it
        was. An item with no recorded grant falls back to its own source, except
        an X post: without a legacy grant it was collected by Social, which
        admits it. Re-running is safe: an already admitted observation reuses
        its packet. Commits per batch; pass the last reported id as
        ``after_id`` to resume.
        """
        admitted = 0
        last_id = after_id
        while True:
            items = (
                self.db.query(ContentItem)
                .filter(ContentItem.id > last_id)
                .order_by(ContentItem.id)
                .limit(batch_size)
                .all()
            )
            if not items:
                return {"admitted": admitted, "last_id": last_id}
            # {item_id: {source_id: (pipelines, first observed_at)}}
            observations: dict[int, dict[int, tuple[set[str], datetime]]] = {}
            for grant in self.db.query(ContentPipelineEligibility).filter(
                ContentPipelineEligibility.content_item_id.in_([item.id for item in items]),
                ContentPipelineEligibility.channel == "legacy",
            ):
                by_source = observations.setdefault(grant.content_item_id, {})
                pipelines, first = by_source.get(
                    grant.originating_source_id, (set(), grant.observed_at)
                )
                pipelines.add(grant.pipeline)
                by_source[grant.originating_source_id] = (pipelines, min(first, grant.observed_at))
            for item in items:
                last_id = item.id
                item_observations = observations.get(item.id)
                if not item_observations:
                    if item.source_type == "twitter":
                        continue
                    item_observations = {item.source_id: (None, item.fetched_at)}
                item_admitted = False
                for source_id, (pipelines, observed_at) in sorted(
                    item_observations.items(), key=lambda entry: entry[1][1]
                ):
                    source = self.db.get(ContentSource, source_id) if source_id else None
                    item_admitted |= self._admit_economic_evidence(
                        item,
                        {
                            "title": item.title,
                            "content": item.content,
                            "url": item.url,
                            "author": item.author,
                            "published_at": item.published_at,
                        },
                        sorted(pipelines) if pipelines else normalize_pipelines(
                            source.pipelines if source else None
                        ),
                        source_id=source_id,
                        source_name=source.name if source else item.source_name,
                        captured_at=_coerce_utc_datetime(observed_at),
                    )
                admitted += item_admitted
            self.db.commit()
            logger.info("Economic evidence backfill: %d items admitted through id %d", admitted, last_id)

    def fetch_source(self, source: ContentSource, lookback_days: int | None = None) -> int:
        """Fetch new content from a single source, returns count of new items.

        Args:
            source: The content source to fetch from.
            lookback_days: If set, override the since date to fetch articles
                from the last N days. Used for backfilling gaps. When backfilling,
                last_fetched_at and last_success_at are NOT updated so normal
                runs resume correctly.

        A failure while fetching or storing is recorded on the source and re-raised.
        """
        if is_social_owned_source(self.db, source):
            return 0
        attempted_at = datetime.now(timezone.utc)
        source_id = source.id
        try:
            return self._fetch_and_store(source, lookback_days, attempted_at)
        except Exception as exc:
            self._record_failure(source_id, exc, attempted_at, scheduled=not lookback_days)
            raise

    def _fetch_and_store(self, source: ContentSource, lookback_days: int | None, attempted_at: datetime) -> int:
        fetcher = self.get_fetcher(source.source_type)

        # Determine since date
        if lookback_days:
            since = attempted_at - timedelta(days=lookback_days)
        else:
            watermark = _coerce_utc_datetime(source.last_success_at)
            # Default 30-day lookback for new sources
            since = watermark - _WATERMARK_OVERLAP if watermark else attempted_at - timedelta(days=30)

        items = fetcher.fetch(source, since)

        # Store new items - track seen external_ids to avoid duplicates within batch
        new_count = 0
        seen_ids = set()
        source_id = source.id
        source_type = source.source_type
        source_name = source.name

        source_pipelines = normalize_pipelines(source.pipelines)

        for item_data in items:
            external_id = item_data["external_id"]

            # Skip if we've already seen this in this batch
            if external_id in seen_ids:
                continue
            seen_ids.add(external_id)

            # Check if already exists in database
            existing = self.db.query(ContentItem).filter(
                ContentItem.source_type == source_type,
                ContentItem.external_id == external_id
            ).first()

            if not existing:
                # Deterministic language detection at ingest — rule-first,
                # cached on the row. See language_detection_service.
                detection_text = build_detection_text(
                    item_data.get("title"), item_data.get("content")
                )
                content_item = ContentItem(
                    source_id=source_id,
                    source_type=source_type,
                    source_name=source_name,
                    external_id=external_id,
                    title=item_data["title"],
                    content=item_data["content"],
                    url=item_data["url"],
                    author=item_data["author"],
                    published_at=_coerce_utc_datetime(item_data["published_at"]),
                    is_processed=False,
                    source_language=detect_language(detection_text),
                )
                self.db.add(content_item)
                self.db.flush()
                self._seed_pipeline_state_rows_for_item(content_item, source_pipelines)
                new_count += 1
            else:
                self._seed_pipeline_state_rows_for_item(existing, source_pipelines)

            from app.services.live_attachment_service import record_attachments
            record_attachments(self.db, existing or content_item, item_data.get("attachments", []))

            for pipeline in source_pipelines:
                grant_eligibility(self.db, (existing or content_item).id, pipeline, "legacy", source_id, datetime.now(timezone.utc))

            # Social-owned X sources never reach here (see the guard above), so
            # an X post here is a legacy-only observation (#500).
            self._admit_economic_evidence(
                existing or content_item,
                item_data,
                source_pipelines,
                source_id=source_id,
                source_name=source_name,
            )

        # Commit all new items
        if new_count > 0:
            self.db.commit()

        # Update source metadata (refresh source first in case session was affected)
        source = self.db.query(ContentSource).filter(ContentSource.id == source_id).first()
        # Only update last_fetched_at during normal runs, not backfills.
        # Backfill leaves last_fetched_at unchanged so the next normal run
        # resumes from the correct point.
        if not lookback_days:
            source.last_fetched_at = attempted_at
            source.last_success_at = attempted_at
            if source_type == "twitter":
                self._persist_twitter_since_id(source_id, items)
        source.consecutive_failures = 0
        source.total_items_fetched = (source.total_items_fetched or 0) + new_count
        self.db.commit()

        logger.info(f"Ingested {new_count} new items from {source.name}")
        return new_count

    def _record_failure(self, source_id: int, exc: Exception, attempted_at: datetime, *, scheduled: bool) -> None:
        """Commit the failure before the caller's rollback, leaving the watermark where it was."""
        self.db.rollback()
        source = self.db.get(ContentSource, source_id)
        if source is None:
            return
        failures = (source.consecutive_failures or 0) + 1
        error = f"{type(exc).__name__}: {exc}"[:500]
        source.consecutive_failures, source.last_error, source.last_error_at = failures, error, attempted_at
        if scheduled:
            # The watermark stays put, so waiting the normal interval loses nothing
            # and spares a failing or rate-limiting provider a retry every poll.
            source.last_fetched_at = attempted_at
        try:
            self.db.commit()
        except Exception:
            # Never let bookkeeping replace the fetch error the caller re-raises.
            self.db.rollback()
            logger.exception("Could not record the failure of content source %s", source_id)
            return
        logger.warning("Content source %s failed (%d in a row): %s", source_id, failures, error)

    def fetch_source_by_id(self, source_id: int, lookback_days: int | None = None) -> int:
        source = self.db.get(ContentSource, source_id)
        if source is None:
            return 0
        return self.fetch_source(source, lookback_days=lookback_days)

    def _persist_twitter_since_id(self, source_id: int, items: list[dict]) -> None:
        candidate_ids = [
            str(item.get("_twitter_since_id", "")).strip()
            for item in items
            if str(item.get("_twitter_since_id", "")).strip().isdigit()
        ]
        if not candidate_ids:
            return
        since_id = str(max(int(candidate_id) for candidate_id in candidate_ids))
        key = _twitter_official_since_id_key(source_id)
        setting = self.db.query(AppSetting).filter(AppSetting.key == key).first()
        if setting is None:
            self.db.add(
                AppSetting(
                    key=key,
                    value=since_id,
                    category="theme",
                    description="Newest official X API post id successfully ingested for this content source.",
                )
            )
        else:
            setting.value = since_id
            setting.category = "theme"
            setting.description = "Newest official X API post id successfully ingested for this content source."

    def fetch_all_active_sources(self, lookback_days: int | None = None) -> dict:
        """Fetch from all active sources, returns summary.

        Args:
            lookback_days: If set, re-fetch articles from the last N days
                (backfill mode). Deduplication ensures no duplicates.
        """
        sources = legacy_sources(self.db, self.db.query(ContentSource).filter(
            ContentSource.is_active == True
        ).order_by(ContentSource.priority.desc()))

        results = {
            "total_sources": len(sources),
            "total_new_items": 0,
            "sources_fetched": [],
            "errors": [],
        }

        for source in sources:
            try:
                new_count = self.fetch_source(source, lookback_days=lookback_days)
                results["total_new_items"] += new_count
                results["sources_fetched"].append({
                    "name": source.name,
                    "type": source.source_type,
                    "new_items": new_count,
                })
            except Exception as e:
                # Rollback the failed transaction before continuing
                self.db.rollback()
                logger.error(f"Error fetching {source.name}: {e}")
                results["errors"].append({
                    "name": source.name,
                    "error": str(e),
                })

            # Rate-limit twitter source requests to avoid overloading upstream services.
            if source.source_type == "twitter":
                time.sleep(settings.twitter_request_delay)

        return results

    def add_source(
        self,
        name: str,
        source_type: str,
        url: str,
        priority: int = 50,
        fetch_interval_minutes: int = 60,
    ) -> ContentSource:
        """Add a new content source"""
        source = ContentSource(
            name=name,
            source_type=source_type,
            url=url,
            priority=priority,
            fetch_interval_minutes=fetch_interval_minutes,
            is_active=True,
        )
        self.db.add(source)
        self.db.commit()
        self.db.refresh(source)
        return source

    def get_unprocessed_items(self, limit: int = 100) -> list[ContentItem]:
        """Get content items that haven't been processed by LLM yet"""
        return self.db.query(ContentItem).filter(
            ContentItem.is_processed == False,
            legacy_eligibility_exists(ContentItem.id, "technical") | legacy_eligibility_exists(ContentItem.id, "fundamental"),
        ).order_by(
            ContentItem.published_at.desc()
        ).limit(limit).all()

    def mark_processed(self, content_item_id: int, error: Optional[str] = None):
        """Mark a content item as processed"""
        item = self.db.query(ContentItem).filter(ContentItem.id == content_item_id).first()
        if item:
            item.is_processed = True
            item.processed_at = datetime.utcnow()
            if error:
                item.extraction_error = error
            self.db.commit()


# Default sources to seed the system
DEFAULT_SOURCES = [
    # Financial Substacks
    {"name": "Doomberg", "type": "substack", "url": "https://doomberg.substack.com/feed", "priority": 80},
    {"name": "SentimenTrader", "type": "substack", "url": "https://sentimentrader.substack.com/feed", "priority": 75},

    # Twitter/X Financial Accounts (examples - user should customize)
    {"name": "@Jesse_Livermore", "type": "twitter", "url": "https://twitter.com/Jesse_Livermore", "priority": 70},
    {"name": "@markminervini", "type": "twitter", "url": "https://twitter.com/markminervini", "priority": 70},

    # Reddit
    {"name": "r/wallstreetbets", "type": "reddit", "url": "https://reddit.com/r/wallstreetbets", "priority": 40},
    {"name": "r/stocks", "type": "reddit", "url": "https://reddit.com/r/stocks", "priority": 50},

    # News RSS
    {"name": "Yahoo Finance", "type": "news", "url": "https://finance.yahoo.com/rss/topstories", "priority": 60},
    {"name": "MarketWatch", "type": "news", "url": "https://feeds.marketwatch.com/marketwatch/topstories/", "priority": 60},
]


def seed_default_sources(db: Session):
    """Seed the database with default content sources"""
    service = ContentIngestionService(db)

    for source_config in DEFAULT_SOURCES:
        # Check if already exists
        existing = db.query(ContentSource).filter(
            ContentSource.source_type == source_config["type"],
            ContentSource.url == source_config["url"]
        ).first()

        if not existing:
            service.add_source(
                name=source_config["name"],
                source_type=source_config["type"],
                url=source_config["url"],
                priority=source_config["priority"],
            )
            logger.info(f"Added default source: {source_config['name']}")
