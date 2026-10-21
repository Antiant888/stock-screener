from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.domain.relative_strength import HORIZON_SESSIONS
from app.models.stock import StockPrice
from app.models.stock_universe import StockUniverse
from app.services.bulk_data_fetcher import BulkDataFetcher
from app.services.static_daily_price_refresh_service import (
    STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
    STATIC_DAILY_PRICE_REFRESH_BATCH_SIZE,
    STATIC_DAILY_PRICE_REFRESH_PERIOD,
    STATIC_RATE_LIMITED_RETRY_BATCH_SIZE,
    STATIC_RATE_LIMITED_RETRY_WAIT_SECONDS,
    STATIC_SESSION_REPAIR_WAIT_SECONDS,
    StaticDailyPriceRefreshService,
    static_daily_price_refresh_batch_size,
)

IN_KEY_MARKET_PRICE_SYMBOLS = ["^NSEI", "NIFTYBEES.NS", "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS"]
HK_KEY_MARKET_PRICE_SYMBOLS = ["^HSI", "2800.HK", "0700.HK", "3690.HK", "0941.HK"]
US_KEY_MARKET_PRICE_SYMBOLS = [
    "SPY",
    "QQQ",
    "IWM",
    "DX-Y.NYB",
    "SGD=X",
    "BTC-USD",
    "GLD",
    "TLT",
    "^VIX",
]
DE_KEY_MARKET_PRICE_SYMBOLS = ["^GDAXI", "EXS1.DE", "SIE.DE", "ALV.DE"]


class _RRGStartupCalendar:
    @staticmethod
    def trading_days(market, start, end):
        assert market == "IN"
        assert start < end
        return [date(2026, 3, 2), end]

    @staticmethod
    def session_anchors(market, as_of_date, *, offsets):
        assert market == "IN"
        assert tuple(offsets) == tuple(HORIZON_SESSIONS.values())
        return {
            0: as_of_date,
            1: as_of_date - timedelta(days=1),
            5: as_of_date - timedelta(days=7),
            21: date(2026, 1, 30),
            63: date(2025, 12, 1),
            126: date(2025, 9, 1),
            189: date(2025, 6, 2),
            252: date(2025, 3, 3),
        }


class _CompleteBreadthHistoryCoverage:
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
            incomplete_symbols=(),
            history_incomplete_symbols=(),
            missing_through_date_symbols=(),
            required_price_date_count=2,
        )


class _CompleteGroupHistoryCoverage:
    @staticmethod
    def required_anchor_dates(*, market, through_date):
        return (through_date,)

    @staticmethod
    def classify(db, *, market, through_date, symbols, required_anchor_dates):
        return SimpleNamespace(incomplete_symbols=())


class _CurrentSessionGapBreadthHistoryCoverage:
    @staticmethod
    def classify(db, *, market, through_date, symbols):
        return SimpleNamespace(
            incomplete_symbols=tuple(symbols),
            history_incomplete_symbols=(),
            missing_through_date_symbols=tuple(symbols),
            required_price_date_count=70,
        )


class _RecordingFetcher:
    def __init__(self, calls: list[dict[str, object]]) -> None:
        self.calls = calls

    def fetch_prices_in_batches(self, symbols, period="2y", start_batch_size=None, market=None):
        self.calls.append(
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


def test_static_daily_price_refresh_service_fetches_stale_and_no_history_groups() -> None:
    session_factory = _sqlite_session_factory()

    with session_factory() as db:
        db.add_all(
            [
                StockUniverse(symbol="OLD.NS", market="IN", is_active=True, market_cap=100.0),
                StockUniverse(symbol="NEW.NS", market="IN", is_active=True, market_cap=90.0),
            ]
        )
        db.add(
            StockPrice(
                symbol="OLD.NS",
                date=date(2026, 6, 3),
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=1000,
            )
        )
        db.commit()

    fetch_calls: list[dict] = []
    stored_batches: list[dict] = []

    service = StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(
            store_batch_in_cache=lambda payload, also_store_db=True, market=None: stored_batches.append(
                {
                    "symbols": sorted(payload.keys()),
                    "also_store_db": also_store_db,
                    "market": market,
                }
            )
        ),
        fetcher=_RecordingFetcher(fetch_calls),
        batch_size_for_market=lambda market: 25,
        sleep=lambda _seconds: None,
    )

    result = service.refresh(as_of_date=date(2026, 6, 4), market="IN")

    # Benchmark candidates first, as their own call, then the universe groups.
    assert fetch_calls == [
        {
            "symbols": ["^NSEI", "NIFTYBEES.NS"],
            "period": STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
            "start_batch_size": 25,
            "market": "IN",
        },
        {
            "symbols": ["OLD.NS"],
            "period": STATIC_DAILY_PRICE_REFRESH_PERIOD,
            "start_batch_size": 25,
            "market": "IN",
        },
        {
            "symbols": ["NEW.NS", "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS"],
            "period": STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
            "start_batch_size": 25,
            "market": "IN",
        },
    ]
    assert stored_batches == [
        {"symbols": ["NIFTYBEES.NS", "^NSEI"], "also_store_db": True, "market": "IN"},
        {"symbols": ["OLD.NS"], "also_store_db": True, "market": "IN"},
        {
            "symbols": ["HDFCBANK.NS", "NEW.NS", "RELIANCE.NS", "TCS.NS"],
            "also_store_db": True,
            "market": "IN",
        },
    ]
    assert result["stale_symbols"] == 1
    assert result["key_market_symbols"] == len(IN_KEY_MARKET_PRICE_SYMBOLS)
    assert result["no_history_symbols"] == 6
    assert result["yahoo_fetched_symbols"] == 7


def test_static_daily_price_refresh_refetches_fresh_rows_with_missing_or_zero_volume() -> None:
    session_factory = _sqlite_session_factory()

    with session_factory() as db:
        db.add_all(
            [
                StockUniverse(symbol="GOOD", market="US", is_active=True, market_cap=100.0),
                StockUniverse(symbol="ZERO", market="US", is_active=True, market_cap=90.0),
                StockUniverse(symbol="MISSING", market="US", is_active=True, market_cap=80.0),
            ]
        )
        db.add_all(
            [
                StockPrice(
                    symbol="GOOD",
                    date=date(2026, 6, 8),
                    open=1.0,
                    high=1.0,
                    low=1.0,
                    close=1.0,
                    volume=1000,
                ),
                StockPrice(
                    symbol="ZERO",
                    date=date(2026, 6, 8),
                    open=1.0,
                    high=1.0,
                    low=1.0,
                    close=1.0,
                    volume=0,
                ),
                StockPrice(
                    symbol="MISSING",
                    date=date(2026, 6, 8),
                    open=1.0,
                    high=1.0,
                    low=1.0,
                    close=1.0,
                    volume=None,
                ),
            ]
        )
        db.commit()

    fetch_calls: list[dict] = []
    stored_batches: list[dict] = []

    service = StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(
            store_batch_in_cache=lambda payload, also_store_db=True, market=None: stored_batches.append(
                {
                    "symbols": sorted(payload.keys()),
                    "also_store_db": also_store_db,
                    "market": market,
                }
            )
        ),
        fetcher=_RecordingFetcher(fetch_calls),
        batch_size_for_market=lambda _market: 25,
        sleep=lambda _seconds: None,
    )

    result = service.refresh(as_of_date=date(2026, 6, 8), market="US")

    assert fetch_calls[1] == {
        "symbols": ["ZERO", "MISSING"],
        "period": STATIC_DAILY_PRICE_REFRESH_PERIOD,
        "start_batch_size": 25,
        "market": "US",
    }
    assert "GOOD" not in [symbol for call in fetch_calls for symbol in call["symbols"]]
    assert stored_batches[1] == {"symbols": ["MISSING", "ZERO"], "also_store_db": True, "market": "US"}
    assert result["db_fresh_symbols"] == 1
    assert result["stale_symbols"] == 2


_DE_STALE_TOP_UP_FETCHES = [
    {
        "symbols": ["^GDAXI", "EXS1.DE"],
        "period": STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
        "start_batch_size": 25,
        "market": "DE",
    },
    {
        "symbols": ["SAP.DE"],
        "period": STATIC_DAILY_PRICE_REFRESH_PERIOD,
        "start_batch_size": 25,
        "market": "DE",
    },
    {
        "symbols": ["SIE.DE", "ALV.DE"],
        "period": STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
        "start_batch_size": 25,
        "market": "DE",
    },
]


@pytest.mark.parametrize(
    ("price_rows", "expected"),
    [
        pytest.param(
            [dict(date=date(2026, 6, 3), open=1.0, high=1.0, low=1.0, close=1.0, volume=1000)],
            {
                "stale_symbols": 1,
                "no_history_symbols": len(DE_KEY_MARKET_PRICE_SYMBOLS),
                "history_incomplete_symbols": 0,
                "breadth_history_incomplete_symbols": 1,
                "breadth_history_missing_through_date_symbols": 1,
            },
            id="keeps_breadth_current_gap_on_stale_top_up",
        ),
        pytest.param(
            [
                dict(date=date(2026, 6, 3), open=1.0, high=1.0, low=1.0, close=1.0, volume=1000),
                dict(date=date(2026, 6, 4), open=None, high=2.0, low=2.0, close=2.0, volume=1000),
            ],
            {
                "db_fresh_symbols": 1,
                "stale_symbols": 1,
                "no_history_symbols": len(DE_KEY_MARKET_PRICE_SYMBOLS),
                "history_incomplete_symbols": 0,
                "breadth_history_incomplete_symbols": 1,
                "breadth_history_missing_through_date_symbols": 1,
            },
            id="top_up_breadth_only_current_gap",
        ),
    ],
)
def test_static_daily_price_refresh_tops_up_breadth_current_session_gap(price_rows, expected) -> None:
    session_factory = _sqlite_session_factory()

    with session_factory() as db:
        db.add(StockUniverse(symbol="SAP.DE", market="DE", is_active=True, market_cap=100.0))
        db.add_all([StockPrice(symbol="SAP.DE", **row) for row in price_rows])
        db.commit()

    fetch_calls: list[dict] = []

    service = StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *_args, **_kwargs: None),
        fetcher=_RecordingFetcher(fetch_calls),
        batch_size_for_market=lambda _market: 25,
        group_history_price_coverage=_CompleteGroupHistoryCoverage(),
        breadth_history_price_coverage=_CurrentSessionGapBreadthHistoryCoverage(),
        sleep=lambda _seconds: None,
    )

    result = service.refresh(
        as_of_date=date(2026, 6, 4),
        market="DE",
        ensure_static_history=True,
    )

    assert fetch_calls == _DE_STALE_TOP_UP_FETCHES
    assert {key: result[key] for key in expected} == expected


def test_static_daily_price_refresh_service_filters_to_selected_market() -> None:
    session_factory = _sqlite_session_factory()

    with session_factory() as db:
        db.add_all(
            [
                StockUniverse(symbol="0700.HK", market="HK", is_active=True, market_cap=100.0),
                StockUniverse(symbol="9988.HK", market="HK", is_active=True, market_cap=90.0),
                StockUniverse(symbol="AAPL", market="US", is_active=True, market_cap=120.0),
                StockUniverse(symbol="BAD-W", market="HK", is_active=True, market_cap=80.0),
            ]
        )
        for symbol in ("0700.HK", "AAPL"):
            db.add(
                StockPrice(
                    symbol=symbol,
                    date=date(2026, 4, 1),
                    open=1.0,
                    high=1.0,
                    low=1.0,
                    close=1.0,
                    volume=1000,
                )
            )
        db.commit()

    fetch_calls: list[dict] = []
    stored_batches: list[dict] = []

    service = StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(
            store_batch_in_cache=lambda payload, also_store_db=True, market=None: stored_batches.append(
                {
                    "symbols": sorted(payload.keys()),
                    "also_store_db": also_store_db,
                    "market": market,
                }
            )
        ),
        fetcher=_RecordingFetcher(fetch_calls),
        batch_size_for_market=lambda _market: 25,
        sleep=lambda _seconds: None,
    )

    result = service.refresh(as_of_date=date(2026, 4, 2), market="HK")

    assert result["market"] == "HK"
    assert result["total_active_symbols"] == 3
    assert result["supported_symbols"] == 6
    assert result["key_market_symbols"] == len(HK_KEY_MARKET_PRICE_SYMBOLS)
    assert result["skipped_unsupported_symbols"] == 1
    assert fetch_calls == [
        {
            "symbols": ["^HSI", "2800.HK"],
            "period": STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
            "start_batch_size": 25,
            "market": "HK",
        },
        {
            "symbols": ["0700.HK"],
            "period": STATIC_DAILY_PRICE_REFRESH_PERIOD,
            "start_batch_size": 25,
            "market": "HK",
        },
        {
            "symbols": ["9988.HK", "3690.HK", "0941.HK"],
            "period": STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
            "start_batch_size": 25,
            "market": "HK",
        },
    ]
    assert stored_batches == [
        {"symbols": ["2800.HK", "^HSI"], "also_store_db": True, "market": "HK"},
        {"symbols": ["0700.HK"], "also_store_db": True, "market": "HK"},
        {
            "symbols": ["0941.HK", "3690.HK", "9988.HK"],
            "also_store_db": True,
            "market": "HK",
        },
    ]


def test_static_daily_price_refresh_batch_size_uses_market_policy(monkeypatch) -> None:
    import app.services.rate_budget_policy as rate_budget_policy

    calls = []

    class _FakePolicy:
        def get_batch_size(self, provider, market):
            calls.append((provider, market))
            return 31

    monkeypatch.setattr(rate_budget_policy, "get_rate_budget_policy", lambda: _FakePolicy())

    assert static_daily_price_refresh_batch_size("IN") == 31
    assert calls == [("yfinance", "IN")]
    assert static_daily_price_refresh_batch_size(None) == STATIC_DAILY_PRICE_REFRESH_BATCH_SIZE


def test_static_daily_price_refresh_includes_us_key_market_data_symbols() -> None:
    session_factory = _sqlite_session_factory()

    fetch_calls: list[dict] = []
    stored_batches: list[dict] = []

    service = StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(
            store_batch_in_cache=lambda payload, also_store_db=True, market=None: stored_batches.append(
                {
                    "symbols": sorted(payload.keys()),
                    "also_store_db": also_store_db,
                    "market": market,
                }
            )
        ),
        fetcher=_RecordingFetcher(fetch_calls),
        batch_size_for_market=lambda _market: 25,
        sleep=lambda _seconds: None,
    )

    result = service.refresh(as_of_date=date(2026, 6, 4), market="US")

    assert fetch_calls == [
        {
            "symbols": ["SPY", "IVV", "^VIX"],
            "period": STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
            "start_batch_size": 25,
            "market": "US",
        },
        {
            "symbols": ["QQQ", "IWM", "DX-Y.NYB", "SGD=X", "BTC-USD", "GLD", "TLT"],
            "period": STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
            "start_batch_size": 25,
            "market": "US",
        },
    ]
    assert stored_batches == [
        {"symbols": ["IVV", "SPY", "^VIX"], "also_store_db": True, "market": "US"},
        {
            "symbols": ["BTC-USD", "DX-Y.NYB", "GLD", "IWM", "QQQ", "SGD=X", "TLT"],
            "also_store_db": True,
            "market": "US",
        },
    ]
    assert result["total_active_symbols"] == 0
    assert result["key_market_symbols"] == len(US_KEY_MARKET_PRICE_SYMBOLS)
    assert result["no_history_symbols"] == 10
    assert result["yahoo_fetched_symbols"] == 10


@pytest.mark.parametrize(
    ("symbol", "price_rows", "expected"),
    [
        pytest.param(
            "OLD.NS",
            [dict(date=date(2026, 6, 4), open=1.0, high=1.0, low=1.0, close=1.0, volume=1000)],
            {
                "db_fresh_symbols": 1,
                "stale_symbols": 0,
                "no_history_symbols": len(IN_KEY_MARKET_PRICE_SYMBOLS),
                "history_incomplete_symbols": 1,
                "rrg_history_coverage_status": "verified",
                "breadth_history_coverage_status": "verified",
                "yahoo_fetched_symbols": 6,
            },
            id="short_history",
        ),
        pytest.param(
            "SPARSE.NS",
            [
                dict(date=date(2025, 1, 2), open=1.0, high=1.0, low=1.0, close=1.0, adj_close=1.0, volume=1000),
                dict(date=date(2026, 6, 4), open=2.0, high=2.0, low=2.0, close=2.0, adj_close=2.0, volume=1000),
            ],
            {
                "db_fresh_symbols": 1,
                "history_incomplete_symbols": 1,
                "rrg_history_coverage_status": "verified",
                "breadth_history_coverage_status": "verified",
            },
            id="sparse_old_rows",
        ),
    ],
)
def test_static_daily_price_refresh_hydrates_history_for_rrg_startup(symbol, price_rows, expected) -> None:
    session_factory = _sqlite_session_factory()

    with session_factory() as db:
        db.add(StockUniverse(symbol=symbol, market="IN", is_active=True, market_cap=100.0))
        db.add_all([StockPrice(symbol=symbol, **row) for row in price_rows])
        db.commit()

    fetch_calls: list[dict] = []

    breadth_coverage = _CompleteBreadthHistoryCoverage()
    service = StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *_args, **_kwargs: None),
        fetcher=_RecordingFetcher(fetch_calls),
        batch_size_for_market=lambda _market: 25,
        calendar_service=_RRGStartupCalendar(),
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
            "symbols": [symbol, "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS"],
            "period": STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
            "start_batch_size": 25,
            "market": "IN",
        },
    ]
    assert {key: result[key] for key in expected} == expected
    assert breadth_coverage.calls == [
        {
            "market": "IN",
            "through_date": date(2026, 6, 4),
            "symbols": (symbol,),
        }
    ]


def test_static_daily_price_refresh_continues_when_rrg_calendar_lookup_fails() -> None:
    session_factory = _sqlite_session_factory()

    with session_factory() as db:
        db.add(
            StockUniverse(symbol="OLD.NS", market="IN", is_active=True, market_cap=100.0)
        )
        db.add(
            StockPrice(
                symbol="OLD.NS",
                date=date(2026, 6, 4),
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=1000,
            )
        )
        db.commit()

    fetch_calls: list[dict] = []

    class _FailingCalendar:
        @staticmethod
        def trading_days(_market, _start, _end):
            raise RuntimeError("calendar unavailable")

    service = StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *_args, **_kwargs: None),
        fetcher=_RecordingFetcher(fetch_calls),
        batch_size_for_market=lambda _market: 25,
        calendar_service=_FailingCalendar(),
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
            "symbols": ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS"],
            "period": STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
            "start_batch_size": 25,
            "market": "IN",
        },
    ]
    assert result["db_fresh_symbols"] == 1
    assert result["history_incomplete_symbols"] == 0
    assert result["rrg_history_coverage_status"] == "unverified"
    assert result["rrg_history_coverage_error"] == "calendar unavailable"
    assert result["yahoo_fetched_symbols"] == len(IN_KEY_MARKET_PRICE_SYMBOLS)


def test_static_daily_price_refresh_uses_date_only_freshness_for_key_market_symbols() -> None:
    session_factory = _sqlite_session_factory()

    with session_factory() as db:
        db.add_all(
            [
                StockPrice(
                    symbol="SPY",
                    date=date(2026, 6, 8),
                    open=1.0,
                    high=1.0,
                    low=1.0,
                    close=1.0,
                    volume=0,
                ),
                StockPrice(
                    symbol="SGD=X",
                    date=date(2026, 6, 8),
                    open=1.0,
                    high=1.0,
                    low=1.0,
                    close=1.0,
                    volume=None,
                ),
            ]
        )
        db.commit()

    fetch_calls: list[dict] = []

    service = StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *_args, **_kwargs: None),
        fetcher=_RecordingFetcher(fetch_calls),
        batch_size_for_market=lambda _market: 25,
        sleep=lambda _seconds: None,
    )

    result = service.refresh(as_of_date=date(2026, 6, 8), market="US")

    assert fetch_calls == [
        {
            "symbols": ["IVV", "^VIX"],
            "period": STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
            "start_batch_size": 25,
            "market": "US",
        },
        {
            "symbols": ["QQQ", "IWM", "DX-Y.NYB", "BTC-USD", "GLD", "TLT"],
            "period": STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
            "start_batch_size": 25,
            "market": "US",
        },
    ]
    assert result["db_fresh_symbols"] == 2
    assert result["stale_symbols"] == 0
    assert result["no_history_symbols"] == 8


def test_static_daily_price_refresh_classifies_all_supported_symbols_with_volume_policy(
    monkeypatch,
) -> None:
    import app.services.static_daily_price_refresh_service as service_module
    from app.services.price_history_coverage import PriceHistoryCoverage

    session_factory = _sqlite_session_factory()

    with session_factory() as db:
        db.add(StockUniverse(symbol="GOOD", market="US", is_active=True, market_cap=100.0))
        db.commit()

    classify_calls: list[dict] = []
    fetch_calls: list[dict] = []

    def _fake_classify_price_history(
        db,
        *,
        symbols,
        as_of_date,
        require_positive_volume=False,
        symbols_requiring_positive_volume=(),
    ):
        classify_calls.append(
            {
                "symbols": list(symbols),
                "as_of_date": as_of_date,
                "require_positive_volume": require_positive_volume,
                "symbols_requiring_positive_volume": list(symbols_requiring_positive_volume),
            }
        )
        return PriceHistoryCoverage(no_history=tuple(symbols))

    monkeypatch.setattr(
        service_module,
        "classify_price_history",
        _fake_classify_price_history,
    )

    service = StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *_args, **_kwargs: None),
        fetcher=_RecordingFetcher(fetch_calls),
        batch_size_for_market=lambda _market: 25,
        sleep=lambda _seconds: None,
    )

    service.refresh(as_of_date=date(2026, 6, 8), market="US")

    assert classify_calls == [
        {
            "symbols": ["GOOD", *US_KEY_MARKET_PRICE_SYMBOLS, "IVV"],
            "as_of_date": date(2026, 6, 8),
            "require_positive_volume": False,
            "symbols_requiring_positive_volume": ["GOOD"],
        }
    ]
    assert [call["symbols"] for call in fetch_calls] == [
        ["SPY", "IVV", "^VIX"],
        ["GOOD", "QQQ", "IWM", "DX-Y.NYB", "SGD=X", "BTC-USD", "GLD", "TLT"],
    ]


def _seed_in_universe(session_factory) -> None:
    with session_factory() as db:
        db.add_all(
            [
                StockUniverse(symbol="RELIANCE.NS", market="IN", is_active=True, market_cap=300.0),
                StockUniverse(symbol="TCS.NS", market="IN", is_active=True, market_cap=200.0),
                StockUniverse(symbol="INFY.NS", market="IN", is_active=True, market_cap=100.0),
            ]
        )
        for symbol in ("RELIANCE.NS", "TCS.NS", "INFY.NS"):
            db.add(
                StockPrice(
                    symbol=symbol,
                    date=date(2026, 4, 1),
                    open=1.0,
                    high=1.0,
                    low=1.0,
                    close=1.0,
                    volume=1000,
                )
            )
        db.commit()


def test_static_daily_price_refresh_retries_rate_limited_failures() -> None:
    session_factory = _sqlite_session_factory()
    _seed_in_universe(session_factory)

    fetch_calls: list[dict] = []
    stored_batches: list[dict] = []
    sleeps: list[float] = []

    class _FakeFetcher:
        def fetch_prices_in_batches(self, symbols, period="2y", start_batch_size=None, market=None):
            fetch_calls.append(
                {
                    "symbols": list(symbols),
                    "period": period,
                    "start_batch_size": start_batch_size,
                    "market": market,
                }
            )
            results = {
                symbol: {"price_data": SimpleNamespace(empty=False), "has_error": False}
                for symbol in symbols
            }
            tcs_requests = sum("TCS.NS" in call["symbols"] for call in fetch_calls)
            if "TCS.NS" in results and tcs_requests == 1:
                results["TCS.NS"] = {
                    "price_data": None,
                    "has_error": True,
                    "error": "Too Many Requests (429)",
                }
            if "INFY.NS" in results:
                results["INFY.NS"] = {
                    "price_data": None,
                    "has_error": True,
                    "error": "delisted: no price data",
                }
            return results

    service = StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(
            store_batch_in_cache=lambda payload, also_store_db=True, market=None: stored_batches.append(
                {
                    "symbols": sorted(payload.keys()),
                    "also_store_db": also_store_db,
                    "market": market,
                }
            )
        ),
        fetcher=_FakeFetcher(),
        sleep=lambda seconds: sleeps.append(seconds),
    )

    result = service.refresh(as_of_date=date(2026, 4, 2), market="IN")

    assert sleeps == [STATIC_RATE_LIMITED_RETRY_WAIT_SECONDS]
    assert [call["symbols"] for call in fetch_calls] == [
        ["^NSEI", "NIFTYBEES.NS"],
        ["RELIANCE.NS", "TCS.NS", "INFY.NS"],
        ["HDFCBANK.NS"],
        ["TCS.NS"],
    ]
    assert fetch_calls[3]["start_batch_size"] == STATIC_RATE_LIMITED_RETRY_BATCH_SIZE
    assert fetch_calls[3]["market"] == "IN"
    stored_symbols = {symbol for batch in stored_batches for symbol in batch["symbols"]}
    assert stored_symbols == {"RELIANCE.NS", "TCS.NS", "^NSEI", "NIFTYBEES.NS", "HDFCBANK.NS"}
    assert result["rate_limited_retry"] == {
        "attempted": 1,
        "recovered": 1,
        "still_failed": 0,
        "skipped": 0,
        "skip_reason": None,
        "wait_seconds": STATIC_RATE_LIMITED_RETRY_WAIT_SECONDS,
        "batch_size": STATIC_RATE_LIMITED_RETRY_BATCH_SIZE,
    }
    assert result["key_market_symbols"] == len(IN_KEY_MARKET_PRICE_SYMBOLS)
    assert result["yahoo_fetched_symbols"] == 5
    assert result["yahoo_failed_symbols"] == 1


def test_static_daily_price_refresh_retries_no_history_rate_limits_with_bootstrap_period() -> None:
    session_factory = _sqlite_session_factory()

    with session_factory() as db:
        db.add(StockUniverse(symbol="NEW.NS", market="IN", is_active=True, market_cap=100.0))
        db.commit()

    fetch_calls: list[dict] = []
    stored_batches: list[dict] = []
    sleeps: list[float] = []

    class _FakeFetcher:
        def fetch_prices_in_batches(self, symbols, period="2y", start_batch_size=None, market=None):
            fetch_calls.append(
                {
                    "symbols": list(symbols),
                    "period": period,
                    "start_batch_size": start_batch_size,
                    "market": market,
                }
            )
            results = {
                symbol: {"price_data": SimpleNamespace(empty=False), "has_error": False}
                for symbol in symbols
            }
            if sum("NEW.NS" in call["symbols"] for call in fetch_calls) == 1:
                results["NEW.NS"] = {
                    "price_data": None,
                    "has_error": True,
                    "error": "Too Many Requests (429)",
                }
            return results

    service = StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(
            store_batch_in_cache=lambda payload, also_store_db=True, market=None: stored_batches.append(
                {
                    "symbols": sorted(payload.keys()),
                    "also_store_db": also_store_db,
                    "market": market,
                }
            )
        ),
        fetcher=_FakeFetcher(),
        batch_size_for_market=lambda _market: 25,
        sleep=lambda seconds: sleeps.append(seconds),
    )

    result = service.refresh(as_of_date=date(2026, 6, 4), market="IN")

    assert sleeps == [STATIC_RATE_LIMITED_RETRY_WAIT_SECONDS]
    assert fetch_calls == [
        {
            "symbols": ["^NSEI", "NIFTYBEES.NS"],
            "period": STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
            "start_batch_size": 25,
            "market": "IN",
        },
        {
            "symbols": ["NEW.NS", "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS"],
            "period": STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
            "start_batch_size": 25,
            "market": "IN",
        },
        {
            "symbols": ["NEW.NS"],
            "period": STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
            "start_batch_size": STATIC_RATE_LIMITED_RETRY_BATCH_SIZE,
            "market": "IN",
        },
    ]
    assert stored_batches == [
        {"symbols": ["NIFTYBEES.NS", "^NSEI"], "also_store_db": True, "market": "IN"},
        {
            "symbols": ["HDFCBANK.NS", "RELIANCE.NS", "TCS.NS"],
            "also_store_db": True,
            "market": "IN",
        },
        {"symbols": ["NEW.NS"], "also_store_db": True, "market": "IN"},
    ]
    assert result["key_market_symbols"] == len(IN_KEY_MARKET_PRICE_SYMBOLS)
    assert result["no_history_symbols"] == 6
    assert result["rate_limited_retry"]["recovered"] == 1
    assert result["yahoo_fetched_symbols"] == 6
    assert result["yahoo_failed_symbols"] == 0


def test_static_daily_price_refresh_skips_retry_for_non_in_markets() -> None:
    session_factory = _sqlite_session_factory()

    with session_factory() as db:
        db.add(StockUniverse(symbol="0700.HK", market="HK", is_active=True, market_cap=100.0))
        db.add(
            StockPrice(
                symbol="0700.HK",
                date=date(2026, 4, 1),
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=1000,
            )
        )
        db.commit()

    fetch_calls: list[dict] = []
    sleeps: list[float] = []

    class _FakeFetcher:
        def fetch_prices_in_batches(self, symbols, period="2y", start_batch_size=None, market=None):
            fetch_calls.append({"symbols": list(symbols), "market": market})
            return {
                symbol: {
                    "price_data": None,
                    "has_error": True,
                    "error": "429 rate limited",
                }
                for symbol in symbols
            }

    service = StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *_args, **_kwargs: None),
        fetcher=_FakeFetcher(),
        sleep=lambda seconds: sleeps.append(seconds),
    )

    result = service.refresh(as_of_date=date(2026, 4, 2), market="HK")

    assert fetch_calls == [
        {"symbols": ["^HSI", "2800.HK"], "market": "HK"},
        {"symbols": ["0700.HK"], "market": "HK"},
        {"symbols": ["3690.HK", "0941.HK"], "market": "HK"},
        {"symbols": ["^HSI"], "market": "HK"},
    ]
    assert sleeps == []
    assert result["rate_limited_retry"] == {
        "attempted": 0,
        "recovered": 0,
        "still_failed": 0,
        "skipped": 5,
        "skip_reason": "market_not_in_retry_allowlist",
        "wait_seconds": 0,
        "batch_size": STATIC_RATE_LIMITED_RETRY_BATCH_SIZE,
    }
    assert result["primary_benchmark_retry"] == {
        "symbol": "^HSI",
        "attempted": True,
        "recovered": False,
        "skipped_reason": None,
        "quote_repair_seed": "plan_disallows_quotes",
        "primary_current": False,
    }
    assert result["yahoo_failed_symbols"] == 5


def test_static_daily_price_refresh_skips_retry_when_no_rate_limited_failures() -> None:
    session_factory = _sqlite_session_factory()
    _seed_in_universe(session_factory)

    fetch_calls: list[dict] = []
    sleeps: list[float] = []

    class _FakeFetcher:
        def fetch_prices_in_batches(self, symbols, period="2y", start_batch_size=None, market=None):
            fetch_calls.append({"symbols": list(symbols)})
            return {
                symbol: {
                    "price_data": None,
                    "has_error": True,
                    "error": "delisted: no price data",
                }
                for symbol in symbols
            }

    service = StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *_args, **_kwargs: None),
        fetcher=_FakeFetcher(),
        sleep=lambda seconds: sleeps.append(seconds),
    )

    result = service.refresh(as_of_date=date(2026, 4, 2), market="IN")

    assert [call["symbols"] for call in fetch_calls] == [
        ["^NSEI", "NIFTYBEES.NS"],
        ["RELIANCE.NS", "TCS.NS", "INFY.NS"],
        ["HDFCBANK.NS"],
        ["^NSEI"],
    ]
    assert sleeps == []
    assert result["rate_limited_retry"] == {
        "attempted": 0,
        "recovered": 0,
        "still_failed": 0,
        "skipped": 0,
        "skip_reason": None,
        "wait_seconds": 0,
        "batch_size": STATIC_RATE_LIMITED_RETRY_BATCH_SIZE,
    }
    assert result["primary_benchmark_retry"] == {
        "symbol": "^NSEI",
        "attempted": True,
        "recovered": False,
        "skipped_reason": None,
        "quote_repair_seed": "plan_disallows_quotes",
        "primary_current": False,
    }
    assert result["key_market_symbols"] == len(IN_KEY_MARKET_PRICE_SYMBOLS)
    assert result["yahoo_failed_symbols"] == 6


_RRG_STARTUP_SEEDED_DATES = (
    date(2026, 6, 3),
    date(2026, 5, 28),
    date(2026, 3, 2),
    date(2026, 3, 1),
    date(2026, 2, 23),
    date(2026, 1, 30),
    date(2025, 12, 1),
    date(2025, 9, 1),
    date(2025, 6, 2),
    date(2025, 3, 3),
)


def _seed_rrg_startup_history(session_factory, symbols) -> None:
    """Seed every _RRGStartupCalendar anchor except the as-of session itself."""
    with session_factory() as db:
        for rank, symbol in enumerate(symbols):
            db.add(
                StockUniverse(
                    symbol=symbol, market="IN", is_active=True, market_cap=100.0 - rank
                )
            )
            db.add_all(
                StockPrice(
                    symbol=symbol,
                    date=seeded_date,
                    open=1.0,
                    high=1.0,
                    low=1.0,
                    close=1.0,
                    adj_close=1.0,
                    volume=1000,
                )
                for seeded_date in _RRG_STARTUP_SEEDED_DATES
            )
        db.commit()


def _price_frame(dates, adj_close: float):
    import pandas as pd

    size = len(dates)
    return pd.DataFrame(
        {
            "Open": [1.0] * size,
            "High": [1.0] * size,
            "Low": [1.0] * size,
            "Close": [adj_close] * size,
            "Adj Close": [adj_close] * size,
            "Volume": [1000] * size,
        },
        index=pd.to_datetime(list(dates)),
    )


def _top_up_frame(adj_close: float):
    return _price_frame([date(2026, 6, 3), date(2026, 6, 4)], adj_close)


def _persisting_store(session_factory, stored: list[list[str]]):
    """Store through the real StockPrice persistence (latest-row update) policy."""
    from app.services.price_row_normalization import stock_price_row_from_ohlcv
    from app.services.stock_price_persistence import persist_stock_price_mappings

    def store(payload, also_store_db=True, market=None):
        stored.append(sorted(payload))
        with session_factory() as db:
            persist_stock_price_mappings(
                db,
                {
                    symbol: [
                        stock_price_row_from_ohlcv(
                            symbol=symbol, row_date=stamp.date(), row=row
                        )
                        for stamp, row in frame.iterrows()
                    ]
                    for symbol, frame in payload.items()
                },
            )
            db.commit()

    return store


def _adj_closes(session_factory, symbol: str) -> set[float]:
    with session_factory() as db:
        return {
            adj_close
            for (adj_close,) in db.query(StockPrice.adj_close).filter(
                StockPrice.symbol == symbol
            )
        }


def _rrg_startup_service(session_factory, fetcher, stored: list[list[str]]):
    return StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(
            store_batch_in_cache=_persisting_store(session_factory, stored)
        ),
        fetcher=fetcher,
        batch_size_for_market=lambda _market: 25,
        calendar_service=_RRGStartupCalendar(),
        breadth_history_price_coverage=_CompleteBreadthHistoryCoverage(),
        sleep=lambda _seconds: None,
    )


def test_static_daily_price_refresh_tops_up_group_history_missing_only_the_current_session() -> None:
    session_factory = _sqlite_session_factory()
    _seed_rrg_startup_history(session_factory, ["OLD.NS"])
    fetch_calls: list[tuple[tuple[str, ...], str]] = []

    class _FakeFetcher:
        def fetch_prices_in_batches(self, symbols, period="2y", start_batch_size=None, market=None):
            fetch_calls.append((tuple(symbols), period))
            return {
                symbol: {"price_data": _top_up_frame(1.0), "has_error": False}
                for symbol in symbols
            }

    result = _rrg_startup_service(session_factory, _FakeFetcher(), []).refresh(
        as_of_date=date(2026, 6, 4),
        market="IN",
        ensure_static_history=True,
    )

    assert fetch_calls == [
        (("^NSEI", "NIFTYBEES.NS"), STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD),
        (("OLD.NS",), STATIC_DAILY_PRICE_REFRESH_PERIOD),
        (("RELIANCE.NS", "TCS.NS", "HDFCBANK.NS"), STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD),
    ]
    assert result["stale_symbols"] == 1
    assert result["rrg_history_incomplete_symbols"] == 0


def test_static_daily_price_refresh_rebootstraps_symbols_whose_history_was_readjusted() -> None:
    session_factory = _sqlite_session_factory()
    _seed_rrg_startup_history(session_factory, ["OLD.NS", "SPLIT.NS"])
    fetch_calls: list[tuple[tuple[str, ...], str]] = []
    stored: list[list[str]] = []

    class _FakeFetcher:
        def fetch_prices_in_batches(self, symbols, period="2y", start_batch_size=None, market=None):
            fetch_calls.append((tuple(symbols), period))
            if period == STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD and symbols == ["SPLIT.NS"]:
                full_history = [*_RRG_STARTUP_SEEDED_DATES, date(2026, 6, 4)]
                return {"SPLIT.NS": {"price_data": _price_frame(full_history, 0.5), "has_error": False}}
            return {
                symbol: {
                    # A 2:1 split halves Yahoo's back-adjusted closes, including
                    # the 2026-06-03 bar the seed already stored at 1.0.
                    "price_data": _top_up_frame(0.5 if symbol == "SPLIT.NS" else 1.0),
                    "has_error": False,
                }
                for symbol in symbols
            }

    result = _rrg_startup_service(session_factory, _FakeFetcher(), stored).refresh(
        as_of_date=date(2026, 6, 4),
        market="IN",
        ensure_static_history=True,
    )

    assert fetch_calls == [
        (("^NSEI", "NIFTYBEES.NS"), STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD),
        (("OLD.NS", "SPLIT.NS"), STATIC_DAILY_PRICE_REFRESH_PERIOD),
        (("RELIANCE.NS", "TCS.NS", "HDFCBANK.NS"), STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD),
        (("SPLIT.NS",), STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD),
    ]
    assert stored[1] == ["OLD.NS"]
    # The old-scale history is replaced, not just the latest row.
    assert _adj_closes(session_factory, "SPLIT.NS") == {0.5}
    assert _adj_closes(session_factory, "OLD.NS") == {1.0}
    assert result["readjusted_symbols"] == 1
    assert result["yahoo_fetched_symbols"] == 2 + len(IN_KEY_MARKET_PRICE_SYMBOLS)


def test_static_daily_price_refresh_tops_up_fresh_symbol_with_a_tail_anchor_gap() -> None:
    session_factory = _sqlite_session_factory()
    with session_factory() as db:
        db.add(StockUniverse(symbol="GAP.NS", market="IN", is_active=True, market_cap=100.0))
        # Current as of 2026-06-04, but missing the 2026-06-03 tail anchor.
        db.add_all(
            StockPrice(
                symbol="GAP.NS",
                date=row_date,
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                adj_close=1.0,
                volume=1000,
            )
            for row_date in (
                date(2026, 6, 4),
                *(d for d in _RRG_STARTUP_SEEDED_DATES if d != date(2026, 6, 3)),
            )
        )
        db.commit()
    fetch_calls: list[tuple[tuple[str, ...], str]] = []

    class _FakeFetcher:
        def fetch_prices_in_batches(self, symbols, period="2y", start_batch_size=None, market=None):
            fetch_calls.append((tuple(symbols), period))
            return {
                symbol: {"price_data": _top_up_frame(1.0), "has_error": False}
                for symbol in symbols
            }

    result = _rrg_startup_service(session_factory, _FakeFetcher(), []).refresh(
        as_of_date=date(2026, 6, 4),
        market="IN",
        ensure_static_history=True,
    )

    assert fetch_calls[1] == (("GAP.NS",), STATIC_DAILY_PRICE_REFRESH_PERIOD)
    assert result["db_fresh_symbols"] == 1
    assert result["rrg_history_tail_gap_symbols"] == 1


def test_static_daily_price_refresh_rebootstraps_readjusted_symbols_recovered_by_rate_limit_retry() -> None:
    session_factory = _sqlite_session_factory()
    seeded_dates = (date(2026, 6, 2), date(2026, 6, 3))
    with session_factory() as db:
        db.add(StockUniverse(symbol="SPLIT.NS", market="IN", is_active=True, market_cap=100.0))
        db.add_all(
            StockPrice(
                symbol="SPLIT.NS",
                date=seeded_date,
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                adj_close=1.0,
                volume=1000,
            )
            for seeded_date in seeded_dates
        )
        db.commit()
    fetch_calls: list[tuple[tuple[str, ...], str]] = []
    stored: list[list[str]] = []

    class _FakeFetcher:
        def fetch_prices_in_batches(self, symbols, period="2y", start_batch_size=None, market=None):
            fetch_calls.append((tuple(symbols), period))
            if "SPLIT.NS" in symbols and sum("SPLIT.NS" in call for call, _ in fetch_calls) == 1:
                return {
                    "SPLIT.NS": {
                        "price_data": None,
                        "has_error": True,
                        "error": "Too Many Requests (429)",
                    }
                }
            if symbols == ["SPLIT.NS"] and period == STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD:
                full_history = [*seeded_dates, date(2026, 6, 4)]
                return {"SPLIT.NS": {"price_data": _price_frame(full_history, 0.5), "has_error": False}}
            return {
                symbol: {"price_data": _top_up_frame(0.5), "has_error": False}
                for symbol in symbols
            }

    service = StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(
            store_batch_in_cache=_persisting_store(session_factory, stored)
        ),
        fetcher=_FakeFetcher(),
        batch_size_for_market=lambda _market: 25,
        sleep=lambda _seconds: None,
    )

    result = service.refresh(as_of_date=date(2026, 6, 4), market="IN")

    assert fetch_calls[-2:] == [
        (("SPLIT.NS",), STATIC_DAILY_PRICE_REFRESH_PERIOD),
        (("SPLIT.NS",), STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD),
    ]
    assert _adj_closes(session_factory, "SPLIT.NS") == {0.5}
    assert result["readjusted_symbols"] == 1
    # The throttled first attempt is not left counted as a failure.
    assert result["yahoo_failed_symbols"] == 0
    assert result["yahoo_fetched_symbols"] == 1 + len(IN_KEY_MARKET_PRICE_SYMBOLS)


def _run_readjusted_split(session_factory, split_history):
    _seed_rrg_startup_history(session_factory, ["SPLIT.NS"])

    class _FakeFetcher:
        def fetch_prices_in_batches(self, symbols, period="2y", start_batch_size=None, market=None):
            if period == STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD and symbols == ["SPLIT.NS"]:
                return {"SPLIT.NS": {"price_data": _price_frame(split_history, 0.5), "has_error": False}}
            return {
                symbol: {"price_data": _top_up_frame(0.5), "has_error": False}
                for symbol in symbols
            }

    return _rrg_startup_service(session_factory, _FakeFetcher(), []).refresh(
        as_of_date=date(2026, 6, 4),
        market="IN",
        ensure_static_history=True,
    )


def test_static_daily_price_refresh_keeps_old_history_when_replacement_write_fails(monkeypatch) -> None:
    import app.services.static_daily_price_refresh_service as module

    def _failing_persist(*_args, **_kwargs):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(module, "persist_stock_price_mappings", _failing_persist)
    session_factory = _sqlite_session_factory()

    result = _run_readjusted_split(
        session_factory, [*_RRG_STARTUP_SEEDED_DATES, date(2026, 6, 4)]
    )

    # The delete rolled back with the failed insert, and the symbol is a failure.
    assert _adj_closes(session_factory, "SPLIT.NS") == {1.0}
    assert result["yahoo_failed_symbols"] == 1


def test_static_daily_price_refresh_rejects_truncated_replacement_history() -> None:
    session_factory = _sqlite_session_factory()

    # A truncated 2y response that stops before the stored 2026-06-03 row.
    truncated = [d for d in _RRG_STARTUP_SEEDED_DATES if d < date(2026, 6, 3)]
    result = _run_readjusted_split(session_factory, truncated)

    with session_factory() as db:
        newest = (
            db.query(StockPrice.adj_close)
            .filter(StockPrice.symbol == "SPLIT.NS", StockPrice.date == date(2026, 6, 3))
            .scalar()
        )
    # Rejected whole: the newer row survives and no mixed-scale series is left.
    assert newest == 1.0
    assert _adj_closes(session_factory, "SPLIT.NS") == {1.0}
    assert result["yahoo_failed_symbols"] == 1


def test_static_daily_price_refresh_rejects_sparse_replacement_history() -> None:
    session_factory = _sqlite_session_factory()

    # Yahoo's 2y response is missing a stored interior date (2025-12-01).
    sparse = [
        *(d for d in _RRG_STARTUP_SEEDED_DATES if d != date(2025, 12, 1)),
        date(2026, 6, 4),
    ]
    result = _run_readjusted_split(session_factory, sparse)

    # Replacing would leave a gap or an old-scale row, so nothing changes.
    assert _adj_closes(session_factory, "SPLIT.NS") == {1.0}
    with session_factory() as db:
        assert db.query(StockPrice).filter(StockPrice.symbol == "SPLIT.NS").count() == len(
            _RRG_STARTUP_SEEDED_DATES
        )
    assert result["yahoo_failed_symbols"] == 1


def test_static_daily_price_refresh_rejects_replacement_missing_the_discarded_top_up_bar() -> None:
    session_factory = _sqlite_session_factory()

    # The drift-triggering 7d frame carried 2026-06-04; the 2y refetch stops at
    # 2026-06-03, the previous stored latest date.
    result = _run_readjusted_split(session_factory, list(_RRG_STARTUP_SEEDED_DATES))

    assert _adj_closes(session_factory, "SPLIT.NS") == {1.0}
    assert result["yahoo_failed_symbols"] == 1


def test_static_daily_price_batch_line_counts_repaired_and_missing_sessions(capsys) -> None:
    as_of = date(2026, 6, 4)
    current = _top_up_frame(1.0)
    stale = _price_frame([date(2026, 6, 3)], 1.0)

    class _FakeFetcher:
        def fetch_prices_in_batches(self, symbols, period="2y", start_batch_size=None, market=None):
            return {
                "FULL": {"price_data": current, "has_error": False},
                "QUOTED": {"price_data": current, "has_error": False, "repaired_by": "yahoo_quote"},
                "BEHIND": {"price_data": stale, "has_error": False},
            }

    service = StaticDailyPriceRefreshService(
        session_factory=_sqlite_session_factory(),
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *args, **kwargs: None),
        fetcher=_FakeFetcher(),
    )

    service._fetch_and_store(
        ["FULL", "QUOTED", "BEHIND"],
        period=STATIC_DAILY_PRICE_REFRESH_PERIOD,
        batch_size=25,
        market="US",
        as_of_date=as_of,
    )

    assert (
        "Batch 1/1 complete: 3/3 processed, 3 refreshed, 0 failed, "
        "1 repaired, 1 missing 2026-06-04."
    ) in capsys.readouterr().out


def test_static_daily_price_fetch_keeps_frames_still_missing_the_session() -> None:
    as_of = date(2026, 6, 4)
    current = _top_up_frame(1.0)
    stale = _price_frame([date(2026, 6, 3)], 1.0)

    class _FakeFetcher:
        def fetch_prices_in_batches(self, symbols, period="2y", start_batch_size=None, market=None):
            return {
                "FULL": {"price_data": current, "has_error": False},
                "BEHIND": {"price_data": stale, "has_error": False},
            }

    service = StaticDailyPriceRefreshService(
        session_factory=_sqlite_session_factory(),
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *args, **kwargs: None),
        fetcher=_FakeFetcher(),
    )
    # A later pass that stored FULL with the session drops it from the map.
    missing = {"FULL": stale}

    service._fetch_and_store(
        ["FULL", "BEHIND"],
        period=STATIC_DAILY_PRICE_REFRESH_PERIOD,
        batch_size=25,
        market="US",
        as_of_date=as_of,
        missing_session_frames=missing,
    )

    assert list(missing) == ["BEHIND"]


def test_static_daily_price_rate_limit_retry_keeps_frames_missing_the_session() -> None:
    as_of = date(2026, 6, 4)
    stale = _price_frame([date(2026, 6, 3)], 1.0)

    class _FakeFetcher:
        def fetch_prices_in_batches(self, symbols, period="2y", start_batch_size=None, market=None):
            return {symbol: {"price_data": stale, "has_error": False} for symbol in symbols}

    service = StaticDailyPriceRefreshService(
        session_factory=_sqlite_session_factory(),
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *args, **kwargs: None),
        fetcher=_FakeFetcher(),
        sleep=lambda seconds: None,
    )
    missing: dict = {}

    service._retry_rate_limited_failures(
        market="IN",
        rate_limited_symbols_by_period={STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD: ["TCS.NS"]},
        as_of_date=as_of,
        missing_session_frames=missing,
    )

    assert list(missing) == ["TCS.NS"]


def _closing_quote(symbol: str, session_close_utc: datetime) -> dict:
    return {
        "symbol": symbol,
        "marketState": "CLOSED",
        "regularMarketTime": int(session_close_utc.timestamp()),
        "regularMarketOpen": 1.0,
        "regularMarketDayHigh": 1.2,
        "regularMarketDayLow": 0.9,
        "regularMarketPrice": 1.1,
        "regularMarketVolume": 500,
    }


def test_static_daily_price_repairs_missing_sessions_from_quotes_after_a_wait() -> None:
    as_of = date(2026, 6, 4)
    stored: list[dict] = []
    sleeps: list[float] = []
    service = StaticDailyPriceRefreshService(
        session_factory=_sqlite_session_factory(),
        price_cache=SimpleNamespace(
            store_batch_in_cache=lambda frames, **kwargs: stored.append(dict(frames))
        ),
        fetcher=SimpleNamespace(),
        sleep=sleeps.append,
        # 16:00 ET close of the as-of session.
        fetch_quotes=lambda symbols: [
            _closing_quote(symbol, datetime(2026, 6, 4, 20, 0, tzinfo=timezone.utc))
            for symbol in symbols
        ],
    )

    stats = service._repair_missing_sessions(
        market="US",
        as_of_date=as_of,
        frames={"BEHIND": _price_frame([date(2026, 6, 3)], 1.0)},
    )

    assert sleeps == [STATIC_SESSION_REPAIR_WAIT_SECONDS]
    assert stats == {
        "attempted": 1,
        "repaired": 1,
        "wait_seconds": STATIC_SESSION_REPAIR_WAIT_SECONDS,
    }
    [frames] = stored
    assert frames["BEHIND"].index[-1].date() == as_of
    assert float(frames["BEHIND"]["Close"].iloc[-1]) == 1.1


def test_static_daily_price_skips_session_repair_without_a_quote_plan() -> None:
    sleeps: list[float] = []
    service = StaticDailyPriceRefreshService(
        session_factory=_sqlite_session_factory(),
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *args, **kwargs: None),
        fetcher=SimpleNamespace(),
        sleep=sleeps.append,
        fetch_quotes=lambda symbols: pytest.fail("HK prices have no quote repair"),
    )

    stats = service._repair_missing_sessions(
        market="HK",
        as_of_date=date(2026, 6, 4),
        frames={"0700.HK": _price_frame([date(2026, 6, 3)], 1.0)},
    )

    assert sleeps == []
    assert stats == {"attempted": 0, "repaired": 0, "wait_seconds": 0}


class _TickingFetcher:
    """Fetcher whose every provider call advances a fake monotonic clock."""

    def __init__(self, *, tick: float = 0.0, responses=None) -> None:
        self.now = 0.0
        self.tick = tick
        self.calls: list[list[str]] = []
        self._responses = responses

    def clock(self) -> float:
        return self.now

    def fetch_prices_in_batches(self, symbols, period="2y", start_batch_size=None, market=None):
        self.calls.append(list(symbols))
        self.now += self.tick
        if self._responses is not None:
            return self._responses(len(self.calls), list(symbols))
        return {
            symbol: {"price_data": SimpleNamespace(empty=False), "has_error": False}
            for symbol in symbols
        }


def test_static_daily_price_refresh_stops_before_the_deadline_with_durable_batches() -> None:
    session_factory = _sqlite_session_factory()
    _seed_in_universe(session_factory)
    fetcher = _TickingFetcher(tick=10.0)
    stored: list[list[str]] = []
    sleeps: list[float] = []
    service = StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(
            store_batch_in_cache=lambda payload, **_kwargs: stored.append(sorted(payload))
        ),
        fetcher=fetcher,
        batch_size_for_market=lambda _market: 1,
        sleep=sleeps.append,
        deadline=25.0,
        clock=fetcher.clock,
    )

    result = service.refresh(as_of_date=date(2026, 4, 2), market="IN")

    # Batches start at t=0, 10 and 20; none starts at t=30, past the deadline.
    assert fetcher.calls == [["^NSEI"], ["NIFTYBEES.NS"], ["RELIANCE.NS"]]
    assert stored == [["^NSEI"], ["NIFTYBEES.NS"], ["RELIANCE.NS"]]
    assert sleeps == []
    assert result["status"] == "resumable"
    assert result["reason"] == "price_stage_deadline"
    assert result["market"] == "IN"
    assert result["as_of_date"] == "2026-04-02"


def test_static_daily_price_refresh_does_not_start_a_retry_wait_past_the_deadline(capsys) -> None:
    session_factory = _sqlite_session_factory()
    _seed_in_universe(session_factory)

    def responses(call_number, symbols):
        if "TCS.NS" in symbols:
            return {
                "TCS.NS": {
                    "price_data": None,
                    "has_error": True,
                    "error": "Too Many Requests (429)",
                }
            }
        return {
            symbol: {"price_data": SimpleNamespace(empty=False), "has_error": False}
            for symbol in symbols
        }

    fetcher = _TickingFetcher(responses=responses)
    sleeps: list[float] = []
    service = StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *_args, **_kwargs: None),
        fetcher=fetcher,
        sleep=sleeps.append,
        deadline=STATIC_RATE_LIMITED_RETRY_WAIT_SECONDS - 1.0,
        clock=fetcher.clock,
    )

    result = service.refresh(as_of_date=date(2026, 4, 2), market="IN")

    assert len(fetcher.calls) == 3  # critical, stale and bootstrap groups, no retry
    assert sleeps == []
    assert result["status"] == "resumable"
    assert "then retrying with batch size" not in capsys.readouterr().out


def test_static_daily_price_session_repair_wait_respects_the_deadline() -> None:
    from app.services.static_daily_price_refresh_service import PriceStageDeadlineReached

    sleeps: list[float] = []
    service = StaticDailyPriceRefreshService(
        session_factory=_sqlite_session_factory(),
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *args, **kwargs: None),
        fetcher=SimpleNamespace(),
        sleep=sleeps.append,
        fetch_quotes=lambda symbols: pytest.fail("no repair past the deadline"),
        deadline=STATIC_SESSION_REPAIR_WAIT_SECONDS - 1.0,
        clock=lambda: 0.0,
    )

    with pytest.raises(PriceStageDeadlineReached):
        service._repair_missing_sessions(
            market="US",
            as_of_date=date(2026, 6, 4),
            frames={"BEHIND": _price_frame([date(2026, 6, 3)], 1.0)},
        )
    assert sleeps == []


def test_rate_limited_retry_checks_the_deadline_between_chunks_and_keeps_finished_ones() -> None:
    # #544 review: one fetch call for the whole retry group ran past the deadline.
    from app.services.static_daily_price_refresh_service import PriceStageDeadlineReached

    clock = {"now": 0.0}
    fetched: list[list[str]] = []
    stored: list[list[str]] = []

    class _Fetcher:
        def fetch_prices_in_batches(self, symbols, period="2y", start_batch_size=None, market=None):
            fetched.append(list(symbols))
            clock["now"] += 1000.0
            return {s: {"price_data": _price_frame([date(2026, 6, 4)], 1.0), "has_error": False} for s in symbols}

    service = StaticDailyPriceRefreshService(
        session_factory=_sqlite_session_factory(),
        price_cache=SimpleNamespace(
            store_batch_in_cache=lambda payload, **_kwargs: stored.append(sorted(payload))
        ),
        fetcher=_Fetcher(),
        sleep=lambda _seconds: None,
        deadline=STATIC_RATE_LIMITED_RETRY_WAIT_SECONDS + 700.0,
        clock=lambda: clock["now"],
    )
    symbols = [f"S{index:02d}.NS" for index in range(STATIC_RATE_LIMITED_RETRY_BATCH_SIZE + 5)]

    with pytest.raises(PriceStageDeadlineReached):
        service._retry_rate_limited_failures(
            market="IN",
            rate_limited_symbols_by_period={STATIC_DAILY_PRICE_REFRESH_PERIOD: symbols},
        )

    assert fetched == [symbols[:STATIC_RATE_LIMITED_RETRY_BATCH_SIZE]]
    assert stored == [symbols[:STATIC_RATE_LIMITED_RETRY_BATCH_SIZE]]


def test_session_repair_rechecks_the_deadline_after_its_wait() -> None:
    from app.services.static_daily_price_refresh_service import PriceStageDeadlineReached

    clock = {"now": 0.0}

    def sleep(seconds):
        clock["now"] += seconds + 30.0  # the wait overran

    service = StaticDailyPriceRefreshService(
        session_factory=_sqlite_session_factory(),
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *args, **kwargs: None),
        fetcher=SimpleNamespace(),
        sleep=sleep,
        fetch_quotes=lambda symbols: pytest.fail("no quote repair past the deadline"),
        deadline=STATIC_SESSION_REPAIR_WAIT_SECONDS + 10.0,
        clock=lambda: clock["now"],
    )

    with pytest.raises(PriceStageDeadlineReached):
        service._repair_missing_sessions(
            market="US",
            as_of_date=date(2026, 6, 4),
            frames={"BEHIND": _price_frame([date(2026, 6, 3)], 1.0)},
        )


def test_session_repair_checks_the_deadline_between_quote_batches() -> None:
    # #544 review: one repair call quoted every stale symbol past the deadline.
    from app.services.static_daily_price_refresh_service import PriceStageDeadlineReached
    from app.services.yahoo_quote_price_repair import YAHOO_QUOTE_BATCH_SIZE

    clock = {"now": 0.0}
    quoted: list[list[str]] = []

    def fetch_quotes(symbols):
        quoted.append(list(symbols))
        clock["now"] += 1000.0
        return []

    service = StaticDailyPriceRefreshService(
        session_factory=_sqlite_session_factory(),
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *args, **kwargs: None),
        fetcher=SimpleNamespace(),
        sleep=lambda _seconds: None,
        fetch_quotes=fetch_quotes,
        deadline=STATIC_SESSION_REPAIR_WAIT_SECONDS + 500.0,
        clock=lambda: clock["now"],
    )
    frames = {
        f"S{index:03d}": _price_frame([date(2026, 6, 3)], 1.0)
        for index in range(YAHOO_QUOTE_BATCH_SIZE + 20)
    }

    with pytest.raises(PriceStageDeadlineReached):
        service._repair_missing_sessions(market="US", as_of_date=date(2026, 6, 4), frames=frames)

    # Every quote request (including the helper's retries) was for the first
    # batch; the second batch never started.
    first_batch = sorted(frames)[:YAHOO_QUOTE_BATCH_SIZE]
    assert quoted and all(sorted(batch) == first_batch for batch in quoted)


def test_static_daily_price_refresh_fetches_critical_symbols_before_the_universe() -> None:
    session_factory = _sqlite_session_factory()
    with session_factory() as db:
        db.add(StockUniverse(symbol="AAPL", market="US", is_active=True, market_cap=100.0))
        for symbol in ("AAPL", "SPY"):
            db.add(
                StockPrice(
                    symbol=symbol,
                    date=date(2026, 6, 3),
                    open=1.0,
                    high=1.0,
                    low=1.0,
                    close=1.0,
                    volume=1000,
                )
            )
        db.commit()
    fetch_calls: list[dict] = []
    service = StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *_args, **_kwargs: None),
        fetcher=_RecordingFetcher(fetch_calls),
        batch_size_for_market=lambda _market: 25,
        sleep=lambda _seconds: None,
    )

    result = service.refresh(as_of_date=date(2026, 6, 4), market="US")

    # The stale primary benchmark alone, then the no-history fallback and ^VIX
    # (IVV is in neither the universe nor the key-market list), then the rest.
    assert [(call["symbols"], call["period"]) for call in fetch_calls] == [
        (["SPY"], STATIC_DAILY_PRICE_REFRESH_PERIOD),
        (["IVV", "^VIX"], STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD),
        (["AAPL"], STATIC_DAILY_PRICE_REFRESH_PERIOD),
        (
            ["QQQ", "IWM", "DX-Y.NYB", "SGD=X", "BTC-USD", "GLD", "TLT"],
            STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
        ),
    ]
    assert result["stale_symbols"] == 2
    assert result["no_history_symbols"] == 9
    assert result["yahoo_fetched_symbols"] == 11
    assert result["primary_benchmark_retry"] == {
        "symbol": "SPY",
        "attempted": False,
        "recovered": False,
        "skipped_reason": "already_current",
        "quote_repair_seed": "not_needed",
        "primary_current": True,
    }


class _UsFetcher:
    """US fake: every symbol gets a current top-up frame unless ``fail`` says otherwise."""

    def __init__(self, fail, *, top_up_dates=(date(2026, 6, 3), date(2026, 6, 4))) -> None:
        self.calls: list[dict] = []
        self._fail = fail
        self._top_up_dates = top_up_dates

    def price_batch_backoff_schedule(self, market):
        return (30, 60, 120)

    def fetch_prices_in_batches(self, symbols, period="2y", start_batch_size=None, market=None):
        self.calls.append(
            {
                "symbols": list(symbols),
                "period": period,
                "start_batch_size": start_batch_size,
                "market": market,
            }
        )
        return {
            symbol: self._fail(symbol, sum(symbol in call["symbols"] for call in self.calls))
            or {"price_data": _price_frame(self._top_up_dates, 1.0), "has_error": False}
            for symbol in symbols
        }


def _throttled():
    return {"price_data": None, "has_error": True, "error": "Too Many Requests (429)"}


def test_static_daily_price_refresh_refetches_a_throttled_primary_benchmark_once_late(capsys) -> None:
    fetcher = _UsFetcher(lambda symbol, attempt: _throttled() if symbol == "SPY" and attempt == 1 else None)
    stored_batches: list[list[str]] = []
    sleeps: list[float] = []
    service = StaticDailyPriceRefreshService(
        session_factory=_sqlite_session_factory(),
        price_cache=SimpleNamespace(
            store_batch_in_cache=lambda payload, **_kwargs: stored_batches.append(sorted(payload))
        ),
        fetcher=fetcher,
        batch_size_for_market=lambda _market: 25,
        sleep=sleeps.append,
    )

    result = service.refresh(as_of_date=date(2026, 6, 4), market="US")

    assert [call["symbols"] for call in fetcher.calls] == [
        ["SPY", "IVV", "^VIX"],
        ["QQQ", "IWM", "DX-Y.NYB", "SGD=X", "BTC-USD", "GLD", "TLT"],
        ["SPY"],
    ]
    assert fetcher.calls[-1] == {
        "symbols": ["SPY"],
        "period": STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD,
        "start_batch_size": 1,
        "market": "US",
    }
    assert sleeps == []
    assert stored_batches[-1] == ["SPY"]
    assert result["primary_benchmark_retry"] == {
        "symbol": "SPY",
        "attempted": True,
        "recovered": True,
        "skipped_reason": None,
        "quote_repair_seed": "not_needed",
        "primary_current": True,
    }
    assert result["rate_limited_retry"] == {
        "attempted": 0,
        "recovered": 0,
        "still_failed": 0,
        "skipped": 1,
        "skip_reason": "market_not_in_retry_allowlist",
        "wait_seconds": 0,
        "batch_size": STATIC_RATE_LIMITED_RETRY_BATCH_SIZE,
    }
    assert result["yahoo_fetched_symbols"] == 10
    assert result["yahoo_failed_symbols"] == 0
    assert result["latest_session_repair"]["attempted"] == 0
    assert (
        "[static-daily prices:US] Primary benchmark SPY recovered 2026-06-04 "
        "after its late single-symbol refetch."
    ) in capsys.readouterr().out


def test_static_daily_price_late_refetch_leaves_a_frame_without_the_session_to_the_quote_repair(
    capsys,
) -> None:
    behind = {"price_data": _price_frame([date(2026, 6, 3)], 1.0), "has_error": False}
    fetcher = _UsFetcher(lambda symbol, attempt: behind if symbol == "SPY" else None)
    quote_requests: list[list[str]] = []
    sleeps: list[float] = []
    service = StaticDailyPriceRefreshService(
        session_factory=_sqlite_session_factory(),
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *_args, **_kwargs: None),
        fetcher=fetcher,
        batch_size_for_market=lambda _market: 25,
        sleep=sleeps.append,
        fetch_quotes=lambda symbols: quote_requests.append(list(symbols))
        or [
            _closing_quote(symbol, datetime(2026, 6, 4, 20, 0, tzinfo=timezone.utc))
            for symbol in symbols
        ],
    )

    result = service.refresh(as_of_date=date(2026, 6, 4), market="US")

    assert [call["symbols"] for call in fetcher.calls][2:] == [["SPY"]]
    assert sleeps == [STATIC_SESSION_REPAIR_WAIT_SECONDS]
    assert result["primary_benchmark_retry"] == {
        "symbol": "SPY",
        "attempted": True,
        "recovered": False,
        "skipped_reason": None,
        "quote_repair_seed": "not_needed",
        "primary_current": True,
    }
    assert quote_requests == [["SPY"]]
    assert result["latest_session_repair"] == {
        "attempted": 1,
        "repaired": 1,
        "wait_seconds": STATIC_SESSION_REPAIR_WAIT_SECONDS,
    }
    assert (
        "[static-daily prices:US] Primary benchmark SPY still missing 2026-06-04 "
        "after its late single-symbol refetch."
    ) in capsys.readouterr().out


@pytest.mark.parametrize(
    ("seconds_left", "attempted"),
    [
        # Reserve: two single-symbol fetches, each with the fetcher's 30/60/120
        # backoff and the quote repair's 15/45 backoff (540s), plus the 60s
        # latest-session repair wait.
        pytest.param(599.0, False, id="one_second_short"),
        pytest.param(600.0, True, id="exactly_the_reserve"),
    ],
)
def test_static_daily_price_late_refetch_skips_quietly_without_deadline_headroom(
    seconds_left, attempted
) -> None:
    fetcher = _UsFetcher(
        lambda symbol, attempt: (
            {"price_data": None, "has_error": True, "error": "connection reset"}
            if symbol == "SPY"
            else None
        )
    )
    sleeps: list[float] = []
    service = StaticDailyPriceRefreshService(
        session_factory=_sqlite_session_factory(),
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *_args, **_kwargs: None),
        fetcher=fetcher,
        batch_size_for_market=lambda _market: 25,
        sleep=sleeps.append,
        deadline=seconds_left,
        clock=lambda: 0.0,
    )

    result = service.refresh(as_of_date=date(2026, 6, 4), market="US")

    assert [call["symbols"] for call in fetcher.calls][2:] == ([["SPY"]] if attempted else [])
    assert sleeps == []
    assert result["status"] == "completed"
    assert result["primary_benchmark_retry"] == {
        "symbol": "SPY",
        "attempted": attempted,
        "recovered": False,
        "skipped_reason": None if attempted else "deadline",
        "quote_repair_seed": "no_stored_history",
        "primary_current": False,
    }
    assert result["yahoo_failed_symbols"] == 1


@pytest.mark.parametrize(
    ("market", "primary", "seconds_left", "attempted"),
    [
        # US: two fetches, each 30+60+120 batch backoff plus 15+45 quote-repair
        # backoff, plus the 60s latest-session repair wait.
        pytest.param("US", "SPY", 599.0, False, id="us_one_second_short"),
        pytest.param("US", "SPY", 600.0, True, id="us_exactly_the_reserve"),
        # IN: 60/120/240 batch backoff and no Yahoo quote step in its price plan.
        pytest.param("IN", "^NSEI", 839.0, False, id="in_one_second_short"),
        pytest.param("IN", "^NSEI", 840.0, True, id="in_exactly_the_reserve"),
    ],
)
def test_static_daily_price_late_refetch_reserve_follows_the_real_fetcher_schedule(
    market, primary, seconds_left, attempted
) -> None:
    scripted = _UsFetcher(
        lambda symbol, attempt: (
            {"price_data": None, "has_error": True, "error": "connection reset"}
            if symbol == primary
            else None
        )
    )
    fetcher = BulkDataFetcher()
    fetcher.fetch_prices_in_batches = scripted.fetch_prices_in_batches
    service = StaticDailyPriceRefreshService(
        session_factory=_sqlite_session_factory(),
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *_args, **_kwargs: None),
        fetcher=fetcher,
        batch_size_for_market=lambda _market: 25,
        sleep=lambda _seconds: None,
        deadline=seconds_left,
        clock=lambda: 0.0,
    )

    result = service.refresh(as_of_date=date(2026, 6, 4), market=market)

    assert ([primary] in [call["symbols"] for call in scripted.calls][1:]) is attempted
    assert result["status"] == "completed"
    assert result["primary_benchmark_retry"]["attempted"] is attempted
    assert result["primary_benchmark_retry"]["skipped_reason"] == (None if attempted else "deadline")


def test_static_daily_price_late_refetch_reports_a_yahoo_circuit_open_on_its_own_call(capsys) -> None:
    circuit_open = {"price_data": None, "has_error": True, "error": "circuit_open", "error_kind": "circuit_open"}
    # Throttled in its group, then the circuit is open by the time of the late call.
    fetcher = _UsFetcher(
        lambda symbol, attempt: (
            [_throttled(), circuit_open][attempt - 1] if symbol == "SPY" else None
        )
    )
    service = StaticDailyPriceRefreshService(
        session_factory=_sqlite_session_factory(),
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *_args, **_kwargs: None),
        fetcher=fetcher,
        batch_size_for_market=lambda _market: 25,
        sleep=lambda _seconds: None,
    )

    result = service.refresh(as_of_date=date(2026, 6, 4), market="US")

    assert [call["symbols"] for call in fetcher.calls] == [
        ["SPY", "IVV", "^VIX"],
        ["QQQ", "IWM", "DX-Y.NYB", "SGD=X", "BTC-USD", "GLD", "TLT"],
        ["SPY"],
    ]
    assert result["primary_benchmark_retry"] == {
        "symbol": "SPY",
        "attempted": False,
        "recovered": False,
        "skipped_reason": "circuit_open",
        "quote_repair_seed": "no_stored_history",
        "primary_current": False,
    }
    assert result["yahoo_fetched_symbols"] == 9
    assert result["yahoo_failed_symbols"] == 1
    assert (
        "[static-daily prices:US] Late refetch of SPY found the Yahoo circuit open for US; "
        "not retried."
    ) in capsys.readouterr().out


def test_static_daily_price_late_refetch_retries_a_primary_whose_first_call_opened_the_circuit(
    capsys,
) -> None:
    circuit_open = {"price_data": None, "has_error": True, "error": "circuit_open", "error_kind": "circuit_open"}
    # The circuit was open for the group call only; the late call finds it closed.
    fetcher = _UsFetcher(
        lambda symbol, attempt: circuit_open if symbol == "SPY" and attempt == 1 else None
    )
    service = StaticDailyPriceRefreshService(
        session_factory=_sqlite_session_factory(),
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *_args, **_kwargs: None),
        fetcher=fetcher,
        batch_size_for_market=lambda _market: 25,
        sleep=lambda _seconds: None,
    )

    result = service.refresh(as_of_date=date(2026, 6, 4), market="US")

    assert [call["symbols"] for call in fetcher.calls][2:] == [["SPY"]]
    assert result["primary_benchmark_retry"] == {
        "symbol": "SPY",
        "attempted": True,
        "recovered": True,
        "skipped_reason": None,
        "quote_repair_seed": "not_needed",
        "primary_current": True,
    }
    assert result["yahoo_fetched_symbols"] == 10
    assert result["yahoo_failed_symbols"] == 0
    assert "circuit" not in capsys.readouterr().out


def test_static_daily_price_unsupported_primary_benchmark_is_not_a_late_refetch_candidate(
    monkeypatch,
) -> None:
    import app.services.static_daily_price_refresh_service as module

    monkeypatch.setattr(
        module,
        "split_supported_price_symbols",
        lambda symbols: (
            [symbol for symbol in symbols if symbol != "SPY"],
            [symbol for symbol in symbols if symbol == "SPY"],
        ),
    )
    fetcher = _UsFetcher(lambda symbol, attempt: None)
    service = StaticDailyPriceRefreshService(
        session_factory=_sqlite_session_factory(),
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *_args, **_kwargs: None),
        fetcher=fetcher,
        batch_size_for_market=lambda _market: 25,
        sleep=lambda _seconds: None,
    )

    result = service.refresh(as_of_date=date(2026, 6, 4), market="US")

    assert [call["symbols"] for call in fetcher.calls] == [
        ["IVV", "^VIX"],
        ["QQQ", "IWM", "DX-Y.NYB", "SGD=X", "BTC-USD", "GLD", "TLT"],
    ]
    assert result["skipped_unsupported_symbols"] == 1
    assert result["primary_benchmark_retry"] == {
        "symbol": "SPY",
        "attempted": False,
        "recovered": False,
        "skipped_reason": "not_a_fetch_candidate",
        "quote_repair_seed": "not_needed",
        "primary_current": False,
    }


def test_static_daily_price_failed_fallback_and_vix_alone_trigger_no_late_refetch() -> None:
    fetcher = _UsFetcher(lambda symbol, attempt: _throttled() if symbol in {"IVV", "^VIX"} else None)
    service = StaticDailyPriceRefreshService(
        session_factory=_sqlite_session_factory(),
        price_cache=SimpleNamespace(store_batch_in_cache=lambda *_args, **_kwargs: None),
        fetcher=fetcher,
        batch_size_for_market=lambda _market: 25,
        sleep=lambda _seconds: None,
    )

    result = service.refresh(as_of_date=date(2026, 6, 4), market="US")

    assert [call["symbols"] for call in fetcher.calls] == [
        ["SPY", "IVV", "^VIX"],
        ["QQQ", "IWM", "DX-Y.NYB", "SGD=X", "BTC-USD", "GLD", "TLT"],
    ]
    assert result["primary_benchmark_retry"]["skipped_reason"] == "already_current"
    assert result["yahoo_fetched_symbols"] == 8
    assert result["yahoo_failed_symbols"] == 2


def test_static_daily_price_late_refetch_replaces_history_when_it_finds_drift() -> None:
    session_factory = _sqlite_session_factory()
    seeded_dates = (date(2026, 6, 2), date(2026, 6, 3))
    with session_factory() as db:
        db.add_all(
            StockPrice(
                symbol="SPY",
                date=seeded_date,
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                adj_close=1.0,
                volume=1000,
            )
            for seeded_date in seeded_dates
        )
        db.commit()
    full_history = {
        "price_data": _price_frame([*seeded_dates, date(2026, 6, 4)], 0.5),
        "has_error": False,
    }

    def fail(symbol, attempt):
        if symbol != "SPY":
            return None
        # Throttled in its group; the late top-up shows a 2:1 split against
        # the stored 1.0 bars; the replacement refetch brings full history.
        return [_throttled(), {"price_data": _top_up_frame(0.5), "has_error": False}, full_history][
            attempt - 1
        ]

    fetcher = _UsFetcher(fail)
    stored: list[list[str]] = []
    service = StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(store_batch_in_cache=_persisting_store(session_factory, stored)),
        fetcher=fetcher,
        batch_size_for_market=lambda _market: 25,
        sleep=lambda _seconds: None,
    )

    result = service.refresh(as_of_date=date(2026, 6, 4), market="US")

    assert [(call["symbols"], call["period"], call["start_batch_size"]) for call in fetcher.calls] == [
        (["SPY"], STATIC_DAILY_PRICE_REFRESH_PERIOD, 25),
        (["IVV", "^VIX"], STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD, 25),
        (["QQQ", "IWM", "DX-Y.NYB", "SGD=X", "BTC-USD", "GLD", "TLT"], STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD, 25),
        (["SPY"], STATIC_DAILY_PRICE_REFRESH_PERIOD, 1),
        (["SPY"], STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD, 1),
    ]
    assert stored[-1] == ["SPY"]
    assert _adj_closes(session_factory, "SPY") == {0.5}
    assert result["primary_benchmark_retry"] == {
        "symbol": "SPY",
        "attempted": True,
        "recovered": True,
        "skipped_reason": None,
        "quote_repair_seed": "not_needed",
        "primary_current": True,
    }
    assert result["readjusted_symbols"] == 1
    assert result["yahoo_fetched_symbols"] == 10
    assert result["yahoo_failed_symbols"] == 0


def _seed_prices(session_factory, symbol: str, dates, *, adj_close: float | None = 1.0) -> None:
    with session_factory() as db:
        db.add_all(
            StockPrice(
                symbol=symbol,
                date=seeded_date,
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                adj_close=adj_close,
                volume=1000,
            )
            for seeded_date in dates
        )
        db.commit()


def _stored_dates(session_factory, symbol: str) -> list[date]:
    with session_factory() as db:
        return [
            stored_date
            for (stored_date,) in db.query(StockPrice.date)
            .filter(StockPrice.symbol == symbol)
            .order_by(StockPrice.date.asc())
        ]


# The 2026-10-20 US session: every other symbol's top-up already holds it.
_OCTOBER_AS_OF = date(2026, 10, 20)
_OCTOBER_TOP_UP_DATES = (date(2026, 10, 19), _OCTOBER_AS_OF)
_OCTOBER_US_UNIVERSE_CALL = ["QQQ", "IWM", "DX-Y.NYB", "SGD=X", "BTC-USD", "GLD", "TLT"]


def _quote_seed_service(
    session_factory,
    fetcher,
    *,
    quote_requests,
    sleeps,
    stored,
    as_of: date = _OCTOBER_AS_OF,
    **service_kwargs,
):
    # Quotes report the 16:00 ET close of the as-of session.
    session_close_utc = datetime.combine(as_of, time(20, 0), tzinfo=timezone.utc)
    return StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(store_batch_in_cache=_persisting_store(session_factory, stored)),
        fetcher=fetcher,
        batch_size_for_market=lambda _market: 25,
        sleep=sleeps.append,
        fetch_quotes=lambda symbols: quote_requests.append(list(symbols))
        or [_closing_quote(symbol, session_close_utc) for symbol in symbols],
        **service_kwargs,
    )


def test_static_daily_price_quote_repairs_a_primary_benchmark_whose_every_fetch_failed(
    capsys,
) -> None:
    session_factory = _sqlite_session_factory()
    _seed_prices(session_factory, "SPY", [date(2026, 10, 16), date(2026, 10, 19)])
    fetcher = _UsFetcher(
        lambda symbol, attempt: _throttled() if symbol == "SPY" else None,
        top_up_dates=_OCTOBER_TOP_UP_DATES,
    )
    quote_requests: list[list[str]] = []
    sleeps: list[float] = []
    stored: list[list[str]] = []
    service = _quote_seed_service(
        session_factory, fetcher, quote_requests=quote_requests, sleeps=sleeps, stored=stored
    )

    result = service.refresh(as_of_date=_OCTOBER_AS_OF, market="US")

    assert [call["symbols"] for call in fetcher.calls] == [
        ["SPY"],
        ["IVV", "^VIX"],
        _OCTOBER_US_UNIVERSE_CALL,
        ["SPY"],
    ]
    assert quote_requests == [["SPY"]]
    assert sleeps == [STATIC_SESSION_REPAIR_WAIT_SECONDS]
    assert stored[-1] == ["SPY"]
    assert _stored_dates(session_factory, "SPY") == [
        date(2026, 10, 16),
        date(2026, 10, 19),
        _OCTOBER_AS_OF,
    ]
    with session_factory() as db:
        repaired = (
            db.query(StockPrice)
            .filter(StockPrice.symbol == "SPY", StockPrice.date == _OCTOBER_AS_OF)
            .one()
        )
    assert (
        repaired.open,
        repaired.high,
        repaired.low,
        repaired.close,
        repaired.adj_close,
        repaired.volume,
    ) == (1.0, 1.2, 0.9, 1.1, 1.1, 500)
    assert result["primary_benchmark_retry"] == {
        "symbol": "SPY",
        "attempted": True,
        "recovered": False,
        "skipped_reason": None,
        "quote_repair_seed": "seeded",
        "primary_current": True,
    }
    assert result["latest_session_repair"] == {
        "attempted": 1,
        "repaired": 1,
        "wait_seconds": STATIC_SESSION_REPAIR_WAIT_SECONDS,
    }
    assert result["yahoo_fetched_symbols"] == 9
    assert result["yahoo_failed_symbols"] == 1
    assert (
        "[static-daily prices:US] Quote-repair seed for primary benchmark SPY: seeded "
        "(stored history through 2026-10-19)."
    ) in capsys.readouterr().out


def test_static_daily_price_quote_repair_seed_refuses_a_stored_frame_two_sessions_behind(
    capsys,
) -> None:
    session_factory = _sqlite_session_factory()
    _seed_prices(session_factory, "SPY", [date(2026, 10, 15), date(2026, 10, 16)])
    fetcher = _UsFetcher(
        lambda symbol, attempt: _throttled() if symbol == "SPY" else None,
        top_up_dates=_OCTOBER_TOP_UP_DATES,
    )
    quote_requests: list[list[str]] = []
    sleeps: list[float] = []
    stored: list[list[str]] = []
    service = _quote_seed_service(
        session_factory, fetcher, quote_requests=quote_requests, sleeps=sleeps, stored=stored
    )

    result = service.refresh(as_of_date=_OCTOBER_AS_OF, market="US")

    assert [call["symbols"] for call in fetcher.calls][3:] == [["SPY"]]
    assert quote_requests == []
    assert sleeps == []
    assert stored == [["IVV", "^VIX"], sorted(_OCTOBER_US_UNIVERSE_CALL)]
    assert _stored_dates(session_factory, "SPY") == [date(2026, 10, 15), date(2026, 10, 16)]
    assert result["primary_benchmark_retry"] == {
        "symbol": "SPY",
        "attempted": True,
        "recovered": False,
        "skipped_reason": None,
        "quote_repair_seed": "gap_too_large",
        "primary_current": False,
    }
    assert result["latest_session_repair"] == {"attempted": 0, "repaired": 0, "wait_seconds": 0}
    assert result["yahoo_failed_symbols"] == 1
    assert (
        "[static-daily prices:US] Quote-repair seed for primary benchmark SPY: gap_too_large "
        "(stored history through 2026-10-16)."
    ) in capsys.readouterr().out


def test_static_daily_price_quote_repair_seed_needs_stored_history(capsys) -> None:
    session_factory = _sqlite_session_factory()
    fetcher = _UsFetcher(
        lambda symbol, attempt: _throttled() if symbol == "SPY" else None,
        top_up_dates=_OCTOBER_TOP_UP_DATES,
    )
    quote_requests: list[list[str]] = []
    sleeps: list[float] = []
    stored: list[list[str]] = []
    service = _quote_seed_service(
        session_factory, fetcher, quote_requests=quote_requests, sleeps=sleeps, stored=stored
    )

    result = service.refresh(as_of_date=_OCTOBER_AS_OF, market="US")

    assert [call["symbols"] for call in fetcher.calls] == [
        ["SPY", "IVV", "^VIX"],
        _OCTOBER_US_UNIVERSE_CALL,
        ["SPY"],
    ]
    assert quote_requests == []
    assert sleeps == []
    assert _stored_dates(session_factory, "SPY") == []
    assert result["primary_benchmark_retry"] == {
        "symbol": "SPY",
        "attempted": True,
        "recovered": False,
        "skipped_reason": None,
        "quote_repair_seed": "no_stored_history",
        "primary_current": False,
    }
    assert result["latest_session_repair"] == {"attempted": 0, "repaired": 0, "wait_seconds": 0}
    assert (
        "[static-daily prices:US] Quote-repair seed for primary benchmark SPY: no_stored_history."
    ) in capsys.readouterr().out


def test_static_daily_price_quote_repair_seed_follows_the_market_price_plan() -> None:
    # HK prices are repaired from Sina inside the fetcher, never from Yahoo quotes.
    session_factory = _sqlite_session_factory()
    _seed_prices(session_factory, "^HSI", [date(2026, 10, 15), date(2026, 10, 16)])
    fetcher = _UsFetcher(
        lambda symbol, attempt: _throttled() if symbol == "^HSI" else None,
        top_up_dates=(date(2026, 10, 16), _OCTOBER_AS_OF),
    )
    sleeps: list[float] = []
    stored: list[list[str]] = []
    service = StaticDailyPriceRefreshService(
        session_factory=session_factory,
        price_cache=SimpleNamespace(store_batch_in_cache=_persisting_store(session_factory, stored)),
        fetcher=fetcher,
        batch_size_for_market=lambda _market: 25,
        sleep=sleeps.append,
        fetch_quotes=lambda symbols: pytest.fail("HK prices have no quote repair"),
    )

    result = service.refresh(as_of_date=_OCTOBER_AS_OF, market="HK")

    assert [call["symbols"] for call in fetcher.calls] == [
        ["^HSI"],
        ["2800.HK"],
        ["0700.HK", "3690.HK", "0941.HK"],
        ["^HSI"],
    ]
    assert sleeps == []
    assert _stored_dates(session_factory, "^HSI") == [date(2026, 10, 15), date(2026, 10, 16)]
    assert result["primary_benchmark_retry"] == {
        "symbol": "^HSI",
        "attempted": True,
        "recovered": False,
        "skipped_reason": None,
        "quote_repair_seed": "plan_disallows_quotes",
        "primary_current": False,
    }
    assert result["latest_session_repair"] == {"attempted": 0, "repaired": 0, "wait_seconds": 0}
    assert result["yahoo_failed_symbols"] == 1


@pytest.mark.parametrize(
    "seconds_left",
    [
        # Under the 60s latest-session repair wait: a seeded frame would make
        # that repair raise and turn a completed run into a resumable one.
        pytest.param(59.0, id="under_the_repair_wait"),
        # The reserve is that wait plus the quote repair's 15/45 backoff.
        pytest.param(119.0, id="one_second_short"),
    ],
)
def test_static_daily_price_quote_repair_seed_skips_without_deadline_headroom(
    seconds_left, capsys
) -> None:
    session_factory = _sqlite_session_factory()
    _seed_prices(session_factory, "SPY", [date(2026, 10, 16), date(2026, 10, 19)])
    fetcher = _UsFetcher(
        lambda symbol, attempt: _throttled() if symbol == "SPY" else None,
        top_up_dates=_OCTOBER_TOP_UP_DATES,
    )
    quote_requests: list[list[str]] = []
    sleeps: list[float] = []
    stored: list[list[str]] = []
    service = _quote_seed_service(
        session_factory,
        fetcher,
        quote_requests=quote_requests,
        sleeps=sleeps,
        stored=stored,
        deadline=seconds_left,
        clock=lambda: 0.0,
    )

    result = service.refresh(as_of_date=_OCTOBER_AS_OF, market="US")

    assert result["status"] == "completed"
    assert quote_requests == []
    assert sleeps == []
    assert _stored_dates(session_factory, "SPY") == [date(2026, 10, 16), date(2026, 10, 19)]
    # Below the late refetch's own 600s reserve too, so the seed is the only
    # path left and it declines as well.
    assert result["primary_benchmark_retry"] == {
        "symbol": "SPY",
        "attempted": False,
        "recovered": False,
        "skipped_reason": "deadline",
        "quote_repair_seed": "deadline",
        "primary_current": False,
    }
    assert result["latest_session_repair"] == {"attempted": 0, "repaired": 0, "wait_seconds": 0}
    assert (
        "[static-daily prices:US] Quote-repair seed for primary benchmark SPY: deadline "
        f"({seconds_left:.0f}s left before the price-stage deadline, 120s needed)."
    ) in capsys.readouterr().out


def test_static_daily_price_quote_repair_seed_runs_with_exactly_its_reserve() -> None:
    session_factory = _sqlite_session_factory()
    _seed_prices(session_factory, "SPY", [date(2026, 10, 16), date(2026, 10, 19)])
    fetcher = _UsFetcher(
        lambda symbol, attempt: _throttled() if symbol == "SPY" else None,
        top_up_dates=_OCTOBER_TOP_UP_DATES,
    )
    quote_requests: list[list[str]] = []
    sleeps: list[float] = []
    stored: list[list[str]] = []
    service = _quote_seed_service(
        session_factory,
        fetcher,
        quote_requests=quote_requests,
        sleeps=sleeps,
        stored=stored,
        deadline=120.0,
        clock=lambda: 0.0,
    )

    result = service.refresh(as_of_date=_OCTOBER_AS_OF, market="US")

    assert result["status"] == "completed"
    assert quote_requests == [["SPY"]]
    assert sleeps == [STATIC_SESSION_REPAIR_WAIT_SECONDS]
    assert _stored_dates(session_factory, "SPY") == [
        date(2026, 10, 16),
        date(2026, 10, 19),
        _OCTOBER_AS_OF,
    ]
    assert result["primary_benchmark_retry"] == {
        "symbol": "SPY",
        "attempted": False,
        "recovered": False,
        "skipped_reason": "deadline",
        "quote_repair_seed": "seeded",
        "primary_current": True,
    }
    assert result["latest_session_repair"] == {
        "attempted": 1,
        "repaired": 1,
        "wait_seconds": STATIC_SESSION_REPAIR_WAIT_SECONDS,
    }


def test_static_daily_price_quote_repair_seed_refuses_readjusted_history(capsys) -> None:
    session_factory = _sqlite_session_factory()
    _seed_prices(session_factory, "SPY", [date(2026, 10, 16), date(2026, 10, 19)])
    # The top-up shows an ex-dividend back-adjustment against the stored 1.0
    # bars; the 2y replacement refetch and its late retry are both throttled,
    # so the stored history is on the old basis and must not take the quote.
    fetcher = _UsFetcher(
        lambda symbol, attempt: (
            None
            if symbol != "SPY"
            else {"price_data": _price_frame(_OCTOBER_TOP_UP_DATES, 0.99), "has_error": False}
            if attempt == 1
            else _throttled()
        ),
        top_up_dates=_OCTOBER_TOP_UP_DATES,
    )
    quote_requests: list[list[str]] = []
    sleeps: list[float] = []
    stored: list[list[str]] = []
    service = _quote_seed_service(
        session_factory, fetcher, quote_requests=quote_requests, sleeps=sleeps, stored=stored
    )

    result = service.refresh(as_of_date=_OCTOBER_AS_OF, market="US")

    assert [(call["symbols"], call["period"]) for call in fetcher.calls] == [
        (["SPY"], STATIC_DAILY_PRICE_REFRESH_PERIOD),
        (["IVV", "^VIX"], STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD),
        (_OCTOBER_US_UNIVERSE_CALL, STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD),
        (["SPY"], STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD),
        (["SPY"], STATIC_DAILY_PRICE_BOOTSTRAP_PERIOD),
    ]
    assert quote_requests == []
    assert sleeps == []
    assert _stored_dates(session_factory, "SPY") == [date(2026, 10, 16), date(2026, 10, 19)]
    assert _adj_closes(session_factory, "SPY") == {1.0}
    assert result["primary_benchmark_retry"] == {
        "symbol": "SPY",
        "attempted": True,
        "recovered": False,
        "skipped_reason": None,
        "quote_repair_seed": "history_readjusted",
        "primary_current": False,
    }
    assert result["readjusted_symbols"] == 1
    assert result["latest_session_repair"] == {"attempted": 0, "repaired": 0, "wait_seconds": 0}
    assert (
        "[static-daily prices:US] Quote-repair seed for primary benchmark SPY: history_readjusted."
    ) in capsys.readouterr().out


@pytest.mark.parametrize(
    ("as_of", "stored_through"),
    [
        pytest.param(date(2026, 10, 19), date(2026, 10, 16), id="weekend"),
        # Labor Day 2026 (09-07) closes the US market between the two.
        pytest.param(date(2026, 9, 8), date(2026, 9, 4), id="holiday"),
    ],
)
def test_static_daily_price_quote_repair_seed_finds_the_previous_session_across_a_closure(
    as_of, stored_through
) -> None:
    session_factory = _sqlite_session_factory()
    _seed_prices(session_factory, "SPY", [stored_through])
    fetcher = _UsFetcher(
        lambda symbol, attempt: _throttled() if symbol == "SPY" else None,
        top_up_dates=(stored_through, as_of),
    )
    quote_requests: list[list[str]] = []
    sleeps: list[float] = []
    stored: list[list[str]] = []
    service = _quote_seed_service(
        session_factory,
        fetcher,
        quote_requests=quote_requests,
        sleeps=sleeps,
        stored=stored,
        as_of=as_of,
    )

    result = service.refresh(as_of_date=as_of, market="US")

    assert quote_requests == [["SPY"]]
    assert _stored_dates(session_factory, "SPY") == [stored_through, as_of]
    assert result["primary_benchmark_retry"] == {
        "symbol": "SPY",
        "attempted": True,
        "recovered": False,
        "skipped_reason": None,
        "quote_repair_seed": "seeded",
        "primary_current": True,
    }
    assert result["latest_session_repair"] == {
        "attempted": 1,
        "repaired": 1,
        "wait_seconds": STATIC_SESSION_REPAIR_WAIT_SECONDS,
    }


def test_static_daily_price_quote_repaired_bar_gets_a_usable_adj_close_without_an_anchor() -> None:
    session_factory = _sqlite_session_factory()
    # Rows stored without an adjusted close, as older history can be.
    _seed_prices(
        session_factory, "SPY", [date(2026, 10, 16), date(2026, 10, 19)], adj_close=None
    )
    fetcher = _UsFetcher(
        lambda symbol, attempt: _throttled() if symbol == "SPY" else None,
        top_up_dates=_OCTOBER_TOP_UP_DATES,
    )
    quote_requests: list[list[str]] = []
    sleeps: list[float] = []
    stored: list[list[str]] = []
    service = _quote_seed_service(
        session_factory, fetcher, quote_requests=quote_requests, sleeps=sleeps, stored=stored
    )

    result = service.refresh(as_of_date=_OCTOBER_AS_OF, market="US")

    with session_factory() as db:
        repaired = (
            db.query(StockPrice)
            .filter(StockPrice.symbol == "SPY", StockPrice.date == _OCTOBER_AS_OF)
            .one()
        )
    # Market RS drops a bar whose adj_close is not positive, so the quote's
    # close stands in for it.
    assert (repaired.close, repaired.adj_close) == (1.1, 1.1)
    assert result["primary_benchmark_retry"] == {
        "symbol": "SPY",
        "attempted": True,
        "recovered": False,
        "skipped_reason": None,
        "quote_repair_seed": "seeded",
        "primary_current": True,
    }
    assert result["latest_session_repair"] == {
        "attempted": 1,
        "repaired": 1,
        "wait_seconds": STATIC_SESSION_REPAIR_WAIT_SECONDS,
    }
