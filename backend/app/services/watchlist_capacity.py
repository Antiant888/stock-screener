"""Size limit and write lock shared by every path that adds watchlist items."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.user_watchlist import UserWatchlist
from app.schemas.universe import MAX_CUSTOM_UNIVERSE_SYMBOLS


class WatchlistCapacityError(ValueError):
    """Adding the symbols would grow a watchlist past what a scan accepts."""

    def __init__(self, existing_count: int, adding_count: int) -> None:
        total = existing_count + adding_count
        super().__init__(
            f"A watchlist holds at most {MAX_CUSTOM_UNIVERSE_SYMBOLS:,} symbols. "
            f"This one has {existing_count:,}, and adding {adding_count:,} more "
            f"would make {total:,}."
        )
        self.existing_count = existing_count
        self.adding_count = adding_count


def check_watchlist_capacity(existing_count: int, adding_count: int) -> None:
    """Reject growing a watchlist past what a custom-universe scan accepts."""
    if adding_count and existing_count + adding_count > MAX_CUSTOM_UNIVERSE_SYMBOLS:
        raise WatchlistCapacityError(existing_count, adding_count)


def locked_watchlist_query(db: Session, watchlist_id: int):
    return db.query(UserWatchlist).filter(UserWatchlist.id == watchlist_id).with_for_update()


def lock_watchlist(db: Session, watchlist_id: int) -> UserWatchlist | None:
    """Load a watchlist row locked until the transaction ends.

    Writers lock it before counting the items, so two concurrent adds to one
    watchlist serialize and cannot pass the cap together. SQLite has no row
    locks and ignores this; its writes already serialize.
    """
    return locked_watchlist_query(db, watchlist_id).first()
