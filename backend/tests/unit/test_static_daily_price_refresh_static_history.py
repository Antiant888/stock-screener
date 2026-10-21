from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.stock import StockPrice
from app.models.stock_universe import StockUniverse
from app.services.static_daily_price_refresh_service import (
    STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
    StaticDailyPriceRefreshService,
)


def _sqlite_session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[StockUniverse.__table__, StockPrice.__table__],
    )
    return sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )


class _CompleteGroupHistoryCoverage:
    @staticmethod
    def required_anchor_dates(*, market, through_date):
        return frozenset({through_date})

    @staticmethod
    def classify(db, *, market, through_date, symbols, required_anchor_dates):
        return SimpleNamespace(incomplete_symbols=())


class _ShortBreadthHistoryCoverage:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def classify(self, db, *, market, through_date, symbols):
        self.calls.append(
            {
                "market": market,
                "through_date": through_date,
                "symbols": tuple(symbols),
            }
        )
        return SimpleNamespace(
            incomplete_symbols=("SHORT.NS",),
            required_price_date_count=4,
        )


def test_static_daily_price_refresh_bootstraps_breadth_incomplete_active_symbols() -> None:
    session_factory = _sqlite_session_factory()

    with session_factory() as db:
        db.add(
            StockUniverse(
                symbol="SHORT.NS",
                market="IN",
                is_active=True,
                market_cap=100.0,
            )
        )
        db.add(
            StockPrice(
                symbol="SHORT.NS",
                date=date(2026, 6, 4),
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=1000,
            )
        )
        db.commit()

    fetch_calls: list[dict[str, object]] = []
    breadth_coverage = _ShortBreadthHistoryCoverage()

    class _FakeFetcher:
        def fetch_prices_in_batches(
            self,
            symbols,
            period="2y",
            start_batch_size=None,
            market=None,
        ):
            fetch_calls.append(
                {
                    "symbols": list(symbols),
                    "period": period,
                    "start_batch_size": start_batch_size,
                    "market": market,
                }
            )
            return {
                symbol: {"price_data": SimpleNamespace(empty=False), "has_error": False}
                for symbol in symbols
            }

    service = StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *_args, **_kwargs: None),
        fetcher=_FakeFetcher(),
        batch_size_for_market=lambda _market: 25,
        group_history_price_coverage=_CompleteGroupHistoryCoverage(),
        breadth_history_price_coverage=breadth_coverage,
        sleep=lambda _seconds: None,
    )

    result = service.refresh(
        as_of_date=date(2026, 6, 4),
        market="IN",
        ensure_static_history=True,
    )

    assert fetch_calls == [
        {
            "symbols": ["^NSEI", "NIFTYBEES.NS"],
            "period": STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
            "start_batch_size": 25,
            "market": "IN",
        },
        {
            "symbols": ["SHORT.NS", "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS"],
            "period": STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
            "start_batch_size": 25,
            "market": "IN",
        },
    ]
    assert breadth_coverage.calls == [
        {
            "market": "IN",
            "through_date": date(2026, 6, 4),
            "symbols": ("SHORT.NS",),
        }
    ]
    assert result["history_incomplete_symbols"] == 1
    assert result["rrg_history_incomplete_symbols"] == 0
    assert result["breadth_history_incomplete_symbols"] == 1
