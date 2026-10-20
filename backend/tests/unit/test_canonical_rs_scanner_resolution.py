from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.domain.relative_strength import BALANCED_RS_FORMULA_VERSION
from app.domain.scanning.ports import CanonicalStockRsSource, MarketRsResolution
from app.scanners.base_screener import StockData
from app.scanners.canslim_scanner import CANSLIMScanner
from app.scanners.custom_scanner import CustomScanner
from app.scanners.criteria.rs_resolution import (
    CanonicalStockRsUnavailable,
    resolve_stock_rs,
)
from app.scanners.minervini_scanner import MinerviniScanner
from app.scanners.data_preparation import DataPreparationLayer
from app.scanners.partial_history_metrics import partial_history_metrics
from app.scanners.scan_orchestrator import _build_precomputed_scan_context
from app.scanners.scan_orchestrator import ScanOrchestrator
from app.scanners.screener_registry import ScreenerRegistry
from app.scanners.setup_engine_screener import SetupEngineScanner

RATINGS = {
    "rs_rating": 87,
    "rs_rating_1m": 42,
    "rs_rating_3m": 91,
    "rs_rating_12m": 98,
}


def _stock_data(symbol: str = "TEST", market: str = "US") -> StockData:
    dates = pd.bdate_range(end="2026-07-17", periods=320)
    close = np.linspace(50.0, 160.0, len(dates))
    benchmark_close = np.linspace(300.0, 360.0, len(dates))
    return StockData(
        symbol=symbol,
        market=market,
        price_data=pd.DataFrame(
            {
                "Open": close,
                "High": close * 1.01,
                "Low": close * 0.99,
                "Close": close,
                "Volume": np.full(len(dates), 1_000_000),
            },
            index=dates,
        ),
        benchmark_data=pd.DataFrame(
            {
                "Open": benchmark_close,
                "High": benchmark_close * 1.01,
                "Low": benchmark_close * 0.99,
                "Close": benchmark_close,
                "Volume": np.full(len(dates), 10_000_000),
            },
            index=dates,
        ),
    )


def _canonical_stock_data() -> StockData:
    data = _stock_data()
    data.rs_source = CanonicalStockRsSource(
        formula_version=BALANCED_RS_FORMULA_VERSION,
        run_id=42,
        universe_size=5000,
        ratings=dict(RATINGS),
    )
    return data


def _ineligible_stock_data() -> StockData:
    data = _stock_data()
    data.rs_source = CanonicalStockRsSource(
        formula_version=BALANCED_RS_FORMULA_VERSION,
        run_id=42,
        universe_size=5000,
        ratings=None,
    )
    return data


def test_market_rs_resolution_creates_one_stock_source_per_symbol():
    canonical = MarketRsResolution.canonical(
        market="US",
        as_of_date=pd.Timestamp("2026-07-17").date(),
        formula_version=BALANCED_RS_FORMULA_VERSION,
        run_id=42,
        universe_size=5000,
        ratings_by_symbol={"TEST": {"rs_rating": 87}},
    )

    stock = canonical.stock_source("TEST")
    assert isinstance(stock, CanonicalStockRsSource)
    assert stock.ratings == {"rs_rating": 87}
    assert stock.audit_fields() == {
        "rs_formula_version": BALANCED_RS_FORMULA_VERSION,
        "market_rs_run_id": 42,
        "rs_universe_size": 5000,
    }
    assert canonical.stock_source("OTHER").ratings is None


def test_resolve_returns_the_canonical_ratings():
    data = _canonical_stock_data()

    assert resolve_stock_rs(data) == RATINGS


def test_resolve_fails_closed_for_stock_outside_eligible_set():
    with pytest.raises(CanonicalStockRsUnavailable, match="TEST"):
        resolve_stock_rs(_ineligible_stock_data())


def test_resolve_fails_closed_without_a_market_rs_resolution():
    with pytest.raises(CanonicalStockRsUnavailable, match="TEST"):
        resolve_stock_rs(_stock_data())


def test_precomputed_context_uses_canonical_balanced_values():
    data = _canonical_stock_data()

    context = _build_precomputed_scan_context(data)

    assert context is not None
    assert context.rs_ratings == RATINGS


def test_precomputed_context_rejects_balanced_ineligible_stock():
    with pytest.raises(CanonicalStockRsUnavailable, match="TEST"):
        _build_precomputed_scan_context(_ineligible_stock_data())


def test_precomputed_context_without_resolution_carries_no_ratings():
    context = _build_precomputed_scan_context(_stock_data())

    assert context.rs_ratings is None


def test_minervini_direct_path_uses_canonical_ratings():
    result = MinerviniScanner().scan_stock(
        "TEST", _canonical_stock_data(), criteria={"include_vcp": False}
    )

    assert result.rating != "Error"
    assert result.details["rs_rating"] == 87
    assert result.details["rs_rating_1m"] == 42
    assert result.details["rs_rating_3m"] == 91
    assert result.details["rs_rating_12m"] == 98


def test_minervini_without_resolution_is_insufficient_data():
    result = MinerviniScanner().scan_stock(
        "TEST", _stock_data(), criteria={"include_vcp": False}
    )

    assert result.rating == "Insufficient Data"
    assert "No Market RS resolution" in result.details["error"]


def test_canslim_direct_path_uses_canonical_ratings():
    result = CANSLIMScanner().scan_stock("TEST", _canonical_stock_data())

    assert result.rating != "Error"
    assert result.details["rs_rating"] == 87
    assert result.details["rs_rating_1m"] == 42
    assert result.details["rs_rating_3m"] == 91
    assert result.details["rs_rating_12m"] == 98
    assert result.breakdown["leader"] == 15


def test_custom_direct_path_uses_canonical_rating():
    result = CustomScanner().scan_stock(
        "TEST",
        _canonical_stock_data(),
        criteria={"custom_filters": {"rs_rating_min": 80}},
    )

    assert result.details["filter_results"]["rs_rating"]["rs_rating"] == 87


def test_custom_canonical_rs_does_not_require_benchmark_history():
    data = _canonical_stock_data()
    data.benchmark_data = pd.DataFrame()

    result = CustomScanner().scan_stock(
        "TEST",
        data,
        criteria={"custom_filters": {"rs_rating_min": 80}},
    )

    assert result.rating != "Insufficient Data"
    assert result.details["filter_results"]["rs_rating"]["rs_rating"] == 87


def test_custom_rs_filter_without_resolution_is_insufficient_data():
    result = CustomScanner().scan_stock(
        "TEST",
        _stock_data(),
        criteria={"custom_filters": {"rs_rating_min": 80}},
    )

    assert result.rating == "Insufficient Data"
    assert "No Market RS resolution" in result.details["reason"]


def test_setup_engine_direct_path_uses_canonical_rating():
    result = SetupEngineScanner().scan_stock("TEST", _canonical_stock_data())

    assert result.rating != "Error"
    assert result.details["setup_engine"]["rs_rating"] == 87


def test_setup_engine_without_resolution_has_no_rs_rating():
    result = SetupEngineScanner().scan_stock("TEST", _stock_data())

    assert result.rating != "Error"
    assert result.details["setup_engine"]["rs_rating"] is None


def test_partial_history_uses_all_canonical_rating_fields():
    metrics = partial_history_metrics(_canonical_stock_data())

    assert metrics["rs_rating"] == 87
    assert metrics["rs_rating_1m"] == 42
    assert metrics["rs_rating_3m"] == 91
    assert metrics["rs_rating_12m"] == 98


def test_partial_history_without_resolution_leaves_every_rating_empty():
    metrics = partial_history_metrics(_stock_data())

    assert metrics["rs_rating"] is None
    assert metrics["rs_rating_1m"] is None
    assert metrics["rs_rating_3m"] is None
    assert metrics["rs_rating_12m"] is None
    assert metrics["rs_sparkline_data"] is not None


class _DirectProvider:
    def __init__(self, data: StockData) -> None:
        self.data = data

    def prepare_data(self, symbol, requirements, *, allow_partial=True):
        return self.data

    def prepare_data_bulk(self, symbols, requirements, **kwargs):
        return {symbol: self.data for symbol in symbols}

    def apply_market_rs_resolution(self, results, resolution) -> None:
        for item in results.values():
            item.rs_source = resolution.stock_source(item.symbol)


class _CanonicalReader:
    def __init__(self, *, include_symbol: bool = True) -> None:
        self.include_symbol = include_symbol
        self.calls: list[dict] = []

    def get(self, **kwargs) -> MarketRsResolution:
        self.calls.append(kwargs)
        ratings = {"TEST": dict(RATINGS)} if self.include_symbol else {}
        return MarketRsResolution.canonical(
            market="US",
            as_of_date=pd.Timestamp("2026-07-17").date(),
            formula_version=BALANCED_RS_FORMULA_VERSION,
            run_id=42,
            universe_size=5000,
            ratings_by_symbol=ratings,
        )


def _minervini_registry() -> ScreenerRegistry:
    registry = ScreenerRegistry()
    registry.register(MinerviniScanner)
    return registry


def test_single_stock_orchestrator_hydrates_latest_canonical_rs():
    reader = _CanonicalReader()
    orchestrator = ScanOrchestrator(
        data_provider=_DirectProvider(_stock_data()),
        registry=_minervini_registry(),
        market_rs_reader=reader,
    )

    result = orchestrator.scan_stock_multi(
        "TEST",
        ["minervini"],
        criteria={"include_vcp": False},
    )

    assert reader.calls == [
        {
            "market": "US",
            "symbols": ("TEST",),
            "as_of_date": None,
            "formula_version": None,
        }
    ]
    assert result["rs_rating"] == 87
    assert result["rs_rating_1m"] == 42
    assert result["rs_rating_3m"] == 91
    assert result["rs_rating_12m"] == 98
    assert result["rs_formula_version"] == BALANCED_RS_FORMULA_VERSION
    assert result["market_rs_run_id"] == 42
    assert result["rs_universe_size"] == 5000
    assert result["details"]["rs_formula_version"] == BALANCED_RS_FORMULA_VERSION


def test_orchestrator_maps_balanced_ineligible_stock_to_insufficient_history():
    orchestrator = ScanOrchestrator(
        data_provider=_DirectProvider(_stock_data()),
        registry=_minervini_registry(),
        market_rs_reader=_CanonicalReader(include_symbol=False),
    )

    result = orchestrator.scan_stock_multi("TEST", ["minervini"])

    assert result["rating"] == "Insufficient Data"
    assert result["result_status"] == "insufficient_history"
    assert result["rs_formula_version"] == BALANCED_RS_FORMULA_VERSION
    assert "eligible universe" in result["reason"]


def test_apply_market_rs_resolution_hydrates_only_the_resolutions_market():
    layer = DataPreparationLayer.__new__(DataPreparationLayer)
    us = _stock_data("TEST", "US")
    hk = _stock_data("0700.HK", "HK")
    resolution = _CanonicalReader().get(
        market="US",
        symbols=("TEST",),
        as_of_date=None,
        formula_version=None,
    )

    layer.apply_market_rs_resolution({"TEST": us, "0700.HK": hk}, resolution)

    assert isinstance(us.rs_source, CanonicalStockRsSource)
    assert us.rs_source.ratings["rs_rating"] == 87
    assert hk.rs_source is None
