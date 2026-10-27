"""Every watchlist writer locks the row before counting items toward the cap."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

import app.api.v1.user_watchlists as endpoints
import app.services.watchlist_capacity as capacity
from app.database import Base
from app.models.stock_universe import StockUniverse
from app.models.user_watchlist import UserWatchlist, WatchlistItem
from app.schemas.user_watchlist import (
    BulkAddItemsRequest,
    WatchlistImportRequest,
    WatchlistItemCreate,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[UserWatchlist.__table__, WatchlistItem.__table__, StockUniverse.__table__],
    )
    session = sessionmaker(bind=engine)()
    session.add(UserWatchlist(id=1, name="Leaders", position=0))
    for symbol in ("NVDA", "MSFT", "AAPL"):
        session.add(StockUniverse(symbol=symbol, name=symbol, is_active=True, status="active"))
    session.add(WatchlistItem(watchlist_id=1, symbol="NVDA", position=0))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_the_watchlist_query_locks_the_row_on_postgres(db):
    sql = str(
        capacity.locked_watchlist_query(db, 1).statement.compile(dialect=postgresql.dialect())
    )

    assert sql.rstrip().endswith("FOR UPDATE")


@pytest.mark.parametrize(
    "add",
    [
        lambda db: endpoints.add_item(1, WatchlistItemCreate(symbol="MSFT"), db),
        lambda db: endpoints.bulk_add_items(1, BulkAddItemsRequest(symbols=["MSFT", "AAPL"]), db),
        lambda db: endpoints.import_items(1, WatchlistImportRequest(content="MSFT\nAAPL"), db),
    ],
    ids=["add", "bulk", "import"],
)
def test_every_rest_writer_locks_the_watchlist_before_checking_the_cap(db, monkeypatch, add):
    events = []
    real_lock = endpoints.lock_watchlist
    real_check = endpoints.check_watchlist_capacity

    def lock(session, watchlist_id):
        events.append(("lock", watchlist_id))
        return real_lock(session, watchlist_id)

    def check(existing_count, adding_count):
        events.append(("check", existing_count))
        return real_check(existing_count, adding_count)

    monkeypatch.setattr(endpoints, "lock_watchlist", lock)
    monkeypatch.setattr(endpoints, "check_watchlist_capacity", check)

    add(db)

    assert events[0] == ("lock", 1)
    assert ("check", 1) in events[1:]


def test_the_rest_cap_message_is_unchanged(db, monkeypatch):
    monkeypatch.setattr(capacity, "MAX_CUSTOM_UNIVERSE_SYMBOLS", 2)

    with pytest.raises(HTTPException) as excinfo:
        endpoints.import_items(1, WatchlistImportRequest(content="MSFT\nAAPL"), db)

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == (
        "A watchlist holds at most 2 symbols. This one has 1, and adding 2 more would make 3."
    )
