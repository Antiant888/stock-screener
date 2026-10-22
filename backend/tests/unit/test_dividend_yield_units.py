"""dividendYield is already in percentage points, and beta must reach the payload.

Measured with the pinned ``yfinance==0.2.66`` on 2026-10-20 against
``dividendRate / price``: AAPL 0.31 vs 0.3157 %, MSFT 0.73 vs 0.7377 %,
KO 2.27 vs 2.2739 %, O 5.87 vs 5.7997 %. ``trailingAnnualDividendYield`` is the
decimal fraction (AAPL 0.00306). The x100 conversion therefore reported every
yield 100x too high - and the bulk path had the same defect.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import yfinance_service as ymod
from app.services.bulk_data_fetcher import BulkDataFetcher


class _FakeTicker:
    """Returns a fixed ``info`` payload for every symbol."""

    payload: dict = {}

    def __init__(self, symbol):
        self.symbol = symbol

    @property
    def info(self):
        return dict(self.payload)


def _service(monkeypatch) -> ymod.YFinanceService:
    """Build a service with a mocked provider and no rate limiting."""
    svc = ymod.YFinanceService(
        rate_limiter=SimpleNamespace(), eps_rating_service=SimpleNamespace()
    )
    monkeypatch.setattr(ymod.yf, "Ticker", _FakeTicker)
    monkeypatch.setattr(svc, "_wait_for_yfinance_rate_limit", lambda: None)
    monkeypatch.setattr(svc, "_extract_eps_rating_data", lambda ticker: {})
    return svc


@pytest.mark.parametrize("raw_value", [0.31, 0.73, 2.27, 5.87, 0.0])
def test_dividend_yield_is_not_scaled(monkeypatch, raw_value):
    """A percentage point value must arrive unchanged."""
    _FakeTicker.payload = {"dividendYield": raw_value}
    result = _service(monkeypatch).get_fundamentals("KO")
    assert result is not None
    assert result["dividend_yield"] == pytest.approx(raw_value)


def test_beta_is_surfaced(monkeypatch):
    """The provider beta was missing from the dict and stayed NULL in every record."""
    _FakeTicker.payload = {"beta": 1.23}
    result = _service(monkeypatch).get_fundamentals("MSFT")
    assert result is not None
    assert result["beta"] == 1.23


@pytest.mark.parametrize("raw_value", [None, "n/a", float("nan"), float("inf"), -0.5, 1e307])
def test_a_value_that_is_not_a_yield_stays_none(monkeypatch, raw_value):
    """A missing or nonsensical value must not look like a measurement."""
    _FakeTicker.payload = {"dividendYield": raw_value}
    result = _service(monkeypatch).get_fundamentals("MSFT")
    assert result is not None
    assert result["dividend_yield"] is None


def test_the_bulk_path_keeps_the_same_unit():
    """Both provider paths must report the yield in the same unit.

    ``returnOnEquity`` is a decimal fraction and keeps its x100 conversion -
    this also proves the fix did not turn the whole dict into a no-op.
    """
    fetcher = BulkDataFetcher.__new__(BulkDataFetcher)  # no __init__: no clients
    fetcher._extract_earnings_calendar = lambda ticker, *, symbol=None: {}
    info = {
        "marketCap": 1e12,
        "sharesOutstanding": 1e9,
        "currentPrice": 93.23,
        "dividendYield": 2.27,
        "returnOnEquity": 0.15,
    }

    fundamentals = fetcher._extract_fundamentals(None, info)

    assert fundamentals["dividend_yield"] == pytest.approx(2.27)
    assert fundamentals["roe"] == pytest.approx(15.0)


@pytest.mark.parametrize("raw_value", [float("nan"), float("inf"), -1.0, "n/a"])
def test_the_bulk_path_rejects_a_value_that_is_not_a_yield(raw_value):
    """The bulk path must not store a value the single-symbol path rejects."""
    fetcher = BulkDataFetcher.__new__(BulkDataFetcher)
    fetcher._extract_earnings_calendar = lambda ticker, *, symbol=None: {}
    info = {
        "marketCap": 1e12,
        "sharesOutstanding": 1e9,
        "currentPrice": 93.23,
        "dividendYield": raw_value,
    }

    fundamentals = fetcher._extract_fundamentals(None, info)

    assert "dividend_yield" not in fundamentals
