"""The stock page reads cached data; Yahoo is never on its request path.

Opening a stock used to make up to six sequential Yahoo calls behind the
shared 1 req/s limiter (info twice, two histories for technicals, two earnings
lookups). Everything the page shows is already stored: universe rows,
fundamentals with the persisted event calendar and the reported quarters
stored with it, and the cached price history.
"""

from __future__ import annotations

import json
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
from app.services.market_calendar_service import MarketCalendarService
from app.services.price_cache_service import PriceCacheService
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

    # Unknown, not zero: the page would otherwise read "0 beats, 0 misses".
    assert event_risk["recent_earnings_count"] is None
    assert event_risk["beat_count_last_4"] is None
    assert event_risk["miss_count_last_4"] is None
    assert "Recent earnings history is unavailable." in event_risk["notes"]


def _earnings_bars() -> pd.DataFrame:
    """Flat 100 bars, moved only around each report's reaction session."""
    frame = pd.DataFrame(
        {"Open": 100.0, "High": 100.0, "Low": 100.0, "Close": 100.0},
        index=pd.bdate_range("2026-03-02", "2026-04-02"),
    )
    frame.loc[pd.Timestamp("2026-03-17"), "Open"] = 110.0
    frame.loc[pd.Timestamp("2026-03-23"), "Close"] = 132.0
    frame.loc[pd.Timestamp("2026-03-24"), "Open"] = 125.4
    frame.loc[pd.Timestamp("2026-03-30"), "Close"] = 112.86
    # Today's bar, written mid-session.
    frame.loc[pd.Timestamp("2026-04-02"), ["Open", "Close"]] = 150.0
    return frame


class _MetaRedis:
    def __init__(self, values: dict[str, str]):
        self.values = values

    def get(self, key):
        return self.values.get(key)


class _EarningsPrices(PriceCacheService):
    """Real fetch-metadata reads; only the database read is stubbed."""

    def __init__(self, calendar, bars, fetch_meta):
        super().__init__(
            redis_client=_MetaRedis(fetch_meta),
            session_factory=lambda: None,
            market_calendar=calendar,
        )
        self._bars = bars

    def get_cached_only(self, _symbol, period="2y"):
        return self._bars.copy()


class _Calendar(MarketCalendarService):
    """Real sessions and timezones; the last completed session is pinned."""

    def __init__(self, last_completed: date):
        super().__init__()
        self.last_completed = last_completed

    def last_completed_trading_day(self, _market, now=None, **_kwargs):
        return self.last_completed


REPORTED = {
    "event_calendar_as_of_date": "2026-04-01",
    "next_earnings_date": None,
    "recent_earnings": [
        # After Wednesday's close: Thursday (today) reacts.
        {"report_ts": "2026-04-01T16:05:00-04:00", "eps_estimate": 1.0, "reported_eps": 1.05, "surprise_pct": 5.0},
        # 08:00 New York time, before the open: the same Tuesday reacts.
        {"report_ts": "2026-03-24T12:00:00+00:00", "eps_estimate": 1.0, "reported_eps": 0.97, "surprise_pct": -3.0},
        # After Monday's close: Tuesday reacts.
        {"report_ts": "2026-03-16T16:00:00-04:00", "eps_estimate": 1.0, "reported_eps": 1.02, "surprise_pct": 2.0},
        {"report_ts": "2026-03-03T16:00:00-05:00", "eps_estimate": None, "reported_eps": 0.9, "surprise_pct": None},
    ],
}


def _earnings_service(
    monkeypatch, *, last_completed: date, bars=None, fetch_meta=None
) -> StockEventContextService:
    monkeypatch.setattr(event_context_module, "to_eastern_date", lambda _now: TODAY)
    calendar = _Calendar(last_completed)
    return StockEventContextService(
        price_cache=_EarningsPrices(
            calendar,
            _earnings_bars() if bars is None else bars,
            fetch_meta or {},
        ),
        yfinance_service=_NoYahoo(),
        market_calendar=calendar,
    )


def test_stored_reports_give_beat_miss_counts_and_drift_mid_session(db, monkeypatch):
    service = _earnings_service(monkeypatch, last_completed=date(2026, 4, 1))

    event_risk, _actions = service.build(
        db, symbol="NVDA", as_of_date=TODAY, fundamentals=REPORTED, market="US"
    )

    assert {
        key: event_risk[key]
        for key in (
            "recent_earnings_count",
            "beat_count_last_4",
            "miss_count_last_4",
            "avg_post_earnings_gap_pct",
            "avg_post_earnings_5s_return_pct",
        )
    } == {
        "recent_earnings_count": 4,
        "beat_count_last_4": 2,
        "miss_count_last_4": 1,
        # Gaps +10 (03-17), -5 (03-24, same session), 0 (03-04); today's
        # partial bar is not a reaction session yet.
        "avg_post_earnings_gap_pct": 1.6667,
        # Returns +20, -10, 0 over five completed sessions.
        "avg_post_earnings_5s_return_pct": 3.3333,
    }
    assert "Recent earnings history is unavailable." not in event_risk["notes"]


def test_after_the_close_today_counts_as_the_reaction_session(db, monkeypatch):
    service = _earnings_service(monkeypatch, last_completed=TODAY)

    event_risk, _actions = service.build(
        db, symbol="NVDA", as_of_date=TODAY, fundamentals=REPORTED, market="US"
    )

    # Today's +50 gap joins; its five-session return is not complete yet.
    assert event_risk["avg_post_earnings_gap_pct"] == 13.75
    assert event_risk["avg_post_earnings_5s_return_pct"] == 3.3333


def test_reports_stay_shown_after_their_calendar_observation_goes_stale(db, monkeypatch):
    """Reported quarters stay true when the next-date lookup ages out."""
    service = _earnings_service(monkeypatch, last_completed=date(2026, 4, 1))
    stale = {**REPORTED, "event_calendar_as_of_date": "2026-03-01"}

    event_risk, _actions = service.build(
        db, symbol="NVDA", as_of_date=TODAY, fundamentals=stale, market="US"
    )

    assert event_risk["next_earnings_date"] is None
    assert event_risk["recent_earnings_count"] == 4
    assert event_risk["beat_count_last_4"] == 2
    assert event_risk["miss_count_last_4"] == 1
    assert event_risk["avg_post_earnings_gap_pct"] == 1.6667
    assert "Recent earnings history is unavailable." not in event_risk["notes"]


def test_a_partial_bar_left_after_the_close_is_not_a_reaction_session(db, monkeypatch):
    """Today's bar was fetched at 11:00 ET; the post-close refresh has not run."""
    service = _earnings_service(
        monkeypatch,
        last_completed=TODAY,
        fetch_meta={
            "price:US:NVDA:fetch_meta": json.dumps(
                {"fetch_timestamp": "2026-04-02T11:00:00-04:00", "needs_refresh_after_close": True}
            )
        },
    )

    event_risk, _actions = service.build(
        db, symbol="NVDA", as_of_date=TODAY, fundamentals=REPORTED, market="US"
    )

    # As mid-session: the partial +50 open on 04-02 is left out.
    assert event_risk["avg_post_earnings_gap_pct"] == 1.6667
    assert event_risk["avg_post_earnings_5s_return_pct"] == 3.3333


def _reaction_bars() -> pd.DataFrame:
    """Flat 100 bars: a +10% gap if Tuesday 03-10 reacts, -10% if Wednesday does."""
    frame = pd.DataFrame(
        {"Open": 100.0, "High": 100.0, "Low": 100.0, "Close": 100.0},
        index=pd.bdate_range("2026-03-02", "2026-03-20"),
    )
    frame.loc[pd.Timestamp("2026-03-10"), "Open"] = 110.0
    frame.loc[pd.Timestamp("2026-03-11"), "Open"] = 90.0
    return frame


@pytest.mark.parametrize(
    ("market", "report_ts", "gap_pct"),
    [
        # Yahoo stamps every market in New York time. 09:15 JST, after
        # Tokyo's 09:00 open: Wednesday reacts.
        ("JP", "2026-03-09T20:15:00-04:00", -10.0),
        # 08:00 JST, before the open: Tuesday reacts.
        ("JP", "2026-03-09T19:00:00-04:00", 10.0),
        ("US", "2026-03-10T16:00:00-04:00", -10.0),
        ("US", "2026-03-10T07:00:00-04:00", 10.0),
    ],
    ids=["jp_0915_after_open", "jp_0800_before_open", "us_1600_after_close", "us_0700_before_open"],
)
def test_the_exchange_open_decides_which_session_reacts(db, monkeypatch, market, report_ts, gap_pct):
    service = _earnings_service(monkeypatch, last_completed=TODAY, bars=_reaction_bars())
    fundamentals = {
        "event_calendar_as_of_date": "2026-04-01",
        "recent_earnings": [
            {"report_ts": report_ts, "eps_estimate": 1.0, "reported_eps": 1.1, "surprise_pct": 10.0}
        ],
    }

    event_risk, _actions = service.build(
        db, symbol="7203.T", as_of_date=TODAY, fundamentals=fundamentals, market=market
    )

    assert event_risk["avg_post_earnings_gap_pct"] == gap_pct


def _cn_bars() -> pd.DataFrame:
    """Flat 100 bars, +10% gaps on Monday 2025-12-08 and Tuesday 2026-03-10, -10% on 03-11."""
    frame = pd.DataFrame(
        {"Open": 100.0, "High": 100.0, "Low": 100.0, "Close": 100.0},
        index=pd.bdate_range("2025-12-01", "2026-03-20"),
    )
    frame.loc[pd.Timestamp("2025-12-08"), "Open"] = 110.0
    frame.loc[pd.Timestamp("2026-03-10"), "Open"] = 110.0
    frame.loc[pd.Timestamp("2026-03-11"), "Open"] = 90.0
    return frame


# Saturday 2025-12-06 10:00 Shanghai, inside the XSHG schedule: Monday reacts.
CN_WEEKEND_REPORT = {
    "report_ts": "2025-12-05T21:00:00-05:00", "eps_estimate": 1.0, "reported_eps": 1.1, "surprise_pct": 10.0,
}
# Tuesday 2026-03-10 08:00 Shanghai, a trading day past the XSHG schedule's end.
CN_UNSCHEDULED_REPORT = {
    "report_ts": "2026-03-09T20:00:00-04:00", "eps_estimate": 1.0, "reported_eps": 1.2, "surprise_pct": 20.0,
}


def _cn_event_risk(db, monkeypatch, reports) -> dict:
    service = _earnings_service(monkeypatch, last_completed=TODAY, bars=_cn_bars())
    event_risk, _actions = service.build(
        db,
        symbol="600519.SS",
        as_of_date=TODAY,
        fundamentals={"event_calendar_as_of_date": "2026-04-01", "recent_earnings": reports},
        market="CN",
    )
    return event_risk


def test_a_cn_weekend_report_reacts_on_the_next_session(db, monkeypatch):
    event_risk = _cn_event_risk(db, monkeypatch, [CN_WEEKEND_REPORT])

    assert event_risk["avg_post_earnings_gap_pct"] == 10.0
    assert event_risk["avg_post_earnings_5s_return_pct"] == -9.0909


def test_a_report_past_the_exchange_schedule_is_left_out_of_drift(db, monkeypatch):
    event_risk = _cn_event_risk(db, monkeypatch, [CN_UNSCHEDULED_REPORT, CN_WEEKEND_REPORT])

    assert event_risk["beat_count_last_4"] == 2
    # Only the weekend report: guessing 03-11 for the 08:00 report would add -10.
    assert event_risk["avg_post_earnings_gap_pct"] == 10.0
    assert event_risk["avg_post_earnings_5s_return_pct"] == -9.0909
