"""The stock page reads cached data; Yahoo is never on its request path.

Opening a stock used to make up to six sequential Yahoo calls behind the
shared 1 req/s limiter (info twice, two histories for technicals, two earnings
lookups). Everything the page shows is already stored: universe rows,
fundamentals with the persisted event calendar, and the cached price history.
Earnings history is not stored yet; the live lookup it replaces never parsed
Yahoo's fiscal-quarter-keyed answer, so it stays reported as unavailable.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import stocks as stocks_module
from app.database import Base
from app.models.institutional_ownership import InstitutionalOwnershipHistory
from app.models.stock_universe import StockUniverse
from app.services import stock_event_context_service as event_context_module
from app.services.fundamentals_cache_service import FundamentalsCacheService
from app.services.stock_event_context_service import StockEventContextService

AS_OF = date(2026, 4, 2)


def _frame(closes, *, end="2026-04-01", highs=None, lows=None) -> pd.DataFrame:
    index = pd.bdate_range(end=end, periods=len(closes))
    closes = [float(c) for c in closes]
    return pd.DataFrame(
        {
            "Open": closes,
            "High": highs if highs is not None else [c + 1 for c in closes],
            "Low": lows if lows is not None else [c - 1 for c in closes],
            "Close": closes,
            "Volume": [1_000_000] * len(closes),
        },
        index=index,
    )


# ── Technicals from the cached price history ─────────────────────────────


def test_moving_averages_come_from_the_cached_closes():
    technicals = stocks_module._cached_technicals("NVDA", _frame(range(1, 261)))

    assert technicals["current_price"] == 260.0
    assert technicals["ma_50"] == 235.5  # mean(211..260)
    assert technicals["ma_150"] == 185.5  # mean(111..260)
    assert technicals["ma_200"] == 160.5  # mean(61..260)


def test_52_week_range_ignores_older_bars():
    closes = [100.0] * 300
    highs = [110.0] * 300
    lows = [90.0] * 300
    highs[0], lows[0] = 999.0, 0.5  # ~60 weeks ago: outside the window
    highs[-10], lows[-10] = 150.0, 60.0

    technicals = stocks_module._cached_technicals(
        "NVDA", _frame(closes, highs=highs, lows=lows)
    )

    assert (technicals["high_52w"], technicals["low_52w"]) == (150.0, 60.0)


def test_52_week_window_includes_the_bar_exactly_a_year_back():
    closes = [100.0] * 300
    highs = [110.0] * 300
    # bdate_range keeps weekdays, so 260 bars back is exactly 52 weeks earlier.
    highs[-261] = 140.0  # same weekday 52 weeks back: inside
    highs[-262] = 999.0  # one business day earlier: outside

    technicals = stocks_module._cached_technicals("NVDA", _frame(closes, highs=highs))

    assert technicals["high_52w"] == 140.0


def test_short_history_leaves_only_the_longer_averages_unset():
    technicals = stocks_module._cached_technicals("NVDA", _frame(range(1, 61)))

    assert technicals["current_price"] == 60.0
    assert technicals["ma_50"] == 35.5  # mean(11..60)
    assert technicals["ma_150"] is None and technicals["ma_200"] is None


def test_no_cached_prices_means_no_technicals():
    assert stocks_module._cached_technicals("NVDA", None) is None
    assert stocks_module._cached_technicals("NVDA", _frame([])) is None


# ── Info from the universe row, fundamentals and the last cached close ───


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine, tables=[StockUniverse.__table__, InstitutionalOwnershipHistory.__table__]
    )
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _add_nvda(db):
    db.add(
        StockUniverse(
            symbol="NVDA",
            name="NVIDIA Corp",
            sector="Technology",
            industry="Semiconductors",
            market="US",
            market_cap=2.0e12,
            is_active=True,
            status="active",
        )
    )
    db.commit()


def test_info_comes_from_stored_rows_and_the_last_close(db):
    _add_nvda(db)

    info = stocks_module._cached_stock_info(
        db,
        "NVDA",
        fundamentals={"market_cap": 2_300_000_000_000},
        price_frame=_frame([900.0, 921.45]),
    )

    assert info == {
        "symbol": "NVDA",
        "name": "NVIDIA Corp",
        "sector": "Technology",
        "industry": "Semiconductors",
        "market": "US",
        "current_price": 921.45,
        "market_cap": 2_300_000_000_000,
    }


def test_market_cap_survives_a_split_before_the_shares_refresh(db):
    """A 10:1 split: the close is post-split, the stored share count is not."""
    _add_nvda(db)

    info = stocks_module._cached_stock_info(
        db,
        "NVDA",
        fundamentals={"market_cap": 500_000_000_000, "shares_outstanding": 200_000_000},
        price_frame=_frame([2_400.0, 250.5]),
    )

    assert info["market_cap"] == 500_000_000_000


def test_a_non_finite_market_cap_is_left_out(db):
    info = stocks_module._cached_stock_info(
        db, "ZZZZ", fundamentals={"market_cap": float("nan")}, price_frame=_frame([10.0])
    )

    assert info["market_cap"] is None


def test_info_falls_back_to_the_universe_market_cap(db):
    _add_nvda(db)

    info = stocks_module._cached_stock_info(
        db, "NVDA", fundamentals=None, price_frame=None
    )

    assert info["market_cap"] == 2_000_000_000_000
    assert info["current_price"] is None


def test_info_is_missing_for_an_unknown_symbol_without_prices(db):
    assert (
        stocks_module._cached_stock_info(db, "ZZZZ", fundamentals=None, price_frame=None)
        is None
    )


# ── Fundamentals: cache_only never fetches ────────────────────────────────


class _NoRedis:
    def get(self, _key):
        return None


@pytest.fixture
def fundamentals_cache(monkeypatch):
    cache = FundamentalsCacheService(redis_client=_NoRedis(), row_facts_resolver=object())
    fetches: list[str] = []

    def fetch(symbol, market=None):
        fetches.append(symbol)
        return {"symbol": symbol, "source": "provider"}

    monkeypatch.setattr(cache, "_fetch_and_cache", fetch)
    monkeypatch.setattr(cache, "_ensure_field_availability_metadata", lambda *a, **k: False)
    monkeypatch.setattr(cache, "_store_in_redis_for_market", lambda *a, **k: None)
    cache.fetches = fetches
    return cache


def test_cache_only_serves_stale_fundamentals_without_fetching(fundamentals_cache, monkeypatch):
    stale = {"symbol": "NVDA", "market_cap": 1}
    monkeypatch.setattr(
        fundamentals_cache, "_get_from_database", lambda s: (stale, pd.Timestamp("2025-01-01"))
    )
    monkeypatch.setattr(fundamentals_cache, "_is_data_fresh", lambda _ts: False)

    assert fundamentals_cache.get_fundamentals("NVDA", cache_only=True) == stale
    assert fundamentals_cache.fetches == []
    # Without the flag the same stale row is refreshed from the provider.
    assert fundamentals_cache.get_fundamentals("NVDA")["source"] == "provider"


def test_cache_only_skips_on_demand_enrichment(fundamentals_cache, monkeypatch):
    partial = {"symbol": "NVDA", "market_cap": 1}
    monkeypatch.setattr(
        fundamentals_cache, "_get_from_database", lambda s: (partial, pd.Timestamp.now())
    )
    monkeypatch.setattr(fundamentals_cache, "_is_data_fresh", lambda _ts: True)
    monkeypatch.setattr(fundamentals_cache, "_needs_on_demand_enrichment", lambda _d: True)

    assert fundamentals_cache.get_fundamentals("NVDA", cache_only=True) == partial
    assert fundamentals_cache.fetches == []


class _RecordingRedis:
    def __init__(self):
        self.writes: list[str] = []

    def get(self, _key):
        return None

    def setex(self, key, _ttl, _value):
        self.writes.append(key)

    def pipeline(self):
        return self

    def set(self, key, *_args, **_kwargs):
        self.writes.append(key)

    def execute(self):
        return []


class _NoFx:
    def __getattr__(self, name):
        raise AssertionError(f"a cache-only read reached the FX service: {name}")


class _Facts:
    def resolve_for_payload(self, symbol, data, *, market=None, currency=None):
        from types import SimpleNamespace

        return SimpleNamespace(market=market or "HK", currency=currency or "HKD")


def test_cache_only_never_promotes_a_stale_row_into_redis(monkeypatch):
    """Every Redis reader treats a hit as fresh, so a stale row written there
    would stop other callers from refreshing it for a week."""
    redis = _RecordingRedis()
    cache = FundamentalsCacheService(redis_client=redis, fx_service=_NoFx(), row_facts_resolver=_Facts())
    stale = {"symbol": "0700.HK", "market_cap": 1, "market_cap_usd": None, "adv_usd": None}
    monkeypatch.setattr(
        cache, "_get_from_database", lambda s: (dict(stale), pd.Timestamp("2025-01-01"))
    )
    monkeypatch.setattr(cache, "_is_data_fresh", lambda _ts: False)
    monkeypatch.setattr(cache, "_fetch_and_cache", lambda *a, **k: pytest.fail("fetched"))

    served = cache.get_fundamentals("0700.HK", market="HK", cache_only=True)

    assert served["market_cap"] == 1
    assert redis.writes == []


def test_cache_only_skips_fx_conversion_of_fresh_rows(monkeypatch):
    redis = _RecordingRedis()
    cache = FundamentalsCacheService(redis_client=redis, fx_service=_NoFx(), row_facts_resolver=_Facts())
    fresh = {"symbol": "0700.HK", "market_cap": 1, "market_cap_usd": None, "adv_usd": None}
    monkeypatch.setattr(
        cache, "_get_from_database", lambda s: (dict(fresh), pd.Timestamp.now())
    )
    monkeypatch.setattr(cache, "_is_data_fresh", lambda _ts: True)
    monkeypatch.setattr(cache, "_needs_on_demand_enrichment", lambda _d: False)

    served = cache.get_fundamentals("0700.HK", market="HK", cache_only=True)

    assert served["market_cap_usd"] is None
    assert isinstance(served["field_availability"], dict)


def test_cache_only_and_force_refresh_contradict_each_other(fundamentals_cache):
    with pytest.raises(ValueError):
        fundamentals_cache.get_fundamentals("NVDA", force_refresh=True, cache_only=True)


def test_cache_only_miss_returns_nothing(fundamentals_cache, monkeypatch):
    monkeypatch.setattr(fundamentals_cache, "_get_from_database", lambda s: (None, None))

    assert fundamentals_cache.get_fundamentals("NVDA", cache_only=True) is None
    assert fundamentals_cache.fetches == []


# ── Event context: stored calendar, no provider calls ─────────────────────


class _Prices:
    def get_cached_only(self, _symbol, period="2y"):
        return None


class _NoYahoo:
    def __getattr__(self, name):
        raise AssertionError(f"the stock page called Yahoo: {name}")


TODAY = date(2026, 4, 2)
CALENDAR = {"next_earnings_date": "2026-04-09", "event_calendar_as_of_date": "2026-03-30"}


@pytest.fixture
def event_service(monkeypatch):
    monkeypatch.setattr(event_context_module, "to_eastern_date", lambda _now: TODAY)
    return StockEventContextService(price_cache=_Prices(), yfinance_service=_NoYahoo())


def test_next_earnings_date_comes_from_the_persisted_calendar(db, event_service):
    event_risk, _actions = event_service.build(
        db, symbol="NVDA", as_of_date=TODAY, fundamentals=CALENDAR
    )

    assert event_risk["next_earnings_date"] == "2026-04-09"
    assert event_risk["days_until_earnings"] == 7


def test_an_older_published_run_still_sees_a_newer_calendar(db, event_service):
    """The run's date is not the clock: an observation after it is not 'future'."""
    event_risk, _actions = event_service.build(
        db, symbol="NVDA", as_of_date=date(2026, 3, 20), fundamentals=CALENDAR
    )

    assert event_risk["next_earnings_date"] == "2026-04-09"
    assert event_risk["days_until_earnings"] == 7


def test_a_stale_calendar_observation_leaves_the_date_unknown(db, event_service):
    stale = {"next_earnings_date": "2026-04-09", "event_calendar_as_of_date": "2026-03-01"}

    event_risk, _actions = event_service.build(
        db, symbol="NVDA", as_of_date=TODAY, fundamentals=stale
    )

    assert event_risk["next_earnings_date"] is None
    assert "Upcoming earnings date is unavailable." in event_risk["notes"]


def test_earnings_history_is_reported_unavailable_without_asking_yahoo(db, event_service):
    event_risk, _actions = event_service.build(
        db, symbol="NVDA", as_of_date=TODAY, fundamentals=CALENDAR
    )

    assert event_risk["recent_earnings_count"] == 0
    assert "Recent earnings history is unavailable." in event_risk["notes"]
