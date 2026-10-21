"""Young listings scan through the real orchestrator with placed horizon ratings."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from app.analysis.patterns.policy import SetupEngineDataRequirements
from app.config import settings
from app.domain.relative_strength import BALANCED_RS_FORMULA_VERSION
from app.domain.scanning.defaults import get_default_scan_profile
from app.domain.scanning.ports import MarketRsResolution
from app.scanners.base_screener import StockData
from app.scanners.canslim_scanner import CANSLIMScanner
from app.scanners.custom_scanner import CustomScanner
from app.scanners.ipo_scanner import IPOScanner
from app.scanners.minervini_scanner import MinerviniScanner
from app.scanners.scan_orchestrator import ScanOrchestrator
from app.scanners.screener_registry import ScreenerRegistry
from app.scanners.setup_engine_screener import SetupEngineScanner
from app.scanners.volume_breakthrough_scanner import VolumeBreakthroughScanner

AAA_RATINGS = {"rs_rating": 80, "rs_rating_1m": 70, "rs_rating_3m": 75, "rs_rating_12m": 90}
NEWIPO_PLACED = {"rs_rating_1m": 66, "rs_rating_3m": 1}
NO_VCP = {"include_vcp": False}


def _stock(symbol: str, bars: int, end: str = "2026-10-16") -> StockData:
    dates = pd.bdate_range(end=end, periods=bars)
    close = np.linspace(20.0, 60.0, bars)
    bench = np.linspace(500.0, 540.0, bars)

    def frame(values, volume):
        return pd.DataFrame(
            {
                "Open": values,
                "High": values * 1.01,
                "Low": values * 0.99,
                "Close": values,
                "Volume": np.full(bars, volume),
            },
            index=dates,
        )

    return StockData(
        symbol=symbol,
        market="US",
        price_data=frame(close, 2_000_000),
        benchmark_data=frame(bench, 50_000_000),
    )


class _Provider:
    def __init__(self, data: StockData) -> None:
        self.data = data

    def prepare_data(self, symbol, requirements, *, allow_partial=True):
        return self.data

    def prepare_data_bulk(self, symbols, requirements, **kwargs):
        return {symbol: self.data for symbol in symbols}

    def apply_market_rs_resolution(self, results, resolution) -> None:
        for item in results.values():
            item.rs_source = resolution.stock_source(item.symbol)


class _Reader:
    @staticmethod
    def get(**kwargs) -> MarketRsResolution:
        return MarketRsResolution.canonical(
            market="US",
            as_of_date=date(2026, 10, 16),
            formula_version=BALANCED_RS_FORMULA_VERSION,
            run_id=1,
            universe_size=8000,
            ratings_by_symbol={"AAA": AAA_RATINGS},
            placed_ratings_by_symbol={"NEWIPO": NEWIPO_PLACED},
        )


def _scan(
    symbol: str,
    bars: int,
    screeners: list[str],
    criteria: dict | None = None,
    end: str = "2026-10-16",
) -> dict:
    registry = ScreenerRegistry()
    for screener in (
        IPOScanner,
        MinerviniScanner,
        CANSLIMScanner,
        CustomScanner,
        SetupEngineScanner,
        VolumeBreakthroughScanner,
    ):
        registry.register(screener)
    orchestrator = ScanOrchestrator(
        data_provider=_Provider(_stock(symbol, bars, end)),
        registry=registry,
        market_rs_reader=_Reader(),
    )
    return orchestrator.scan_stock_multi(symbol, screeners, criteria=criteria)


def _rs_fields(result: dict) -> tuple:
    return tuple(result.get(f) for f in ("rs_rating", "rs_rating_1m", "rs_rating_3m", "rs_rating_12m"))


def test_a_young_listing_is_rated_by_ipo_and_carries_its_placed_horizons():
    result = _scan("NEWIPO", 150, ["ipo", "minervini"], NO_VCP)

    assert result["result_status"] == "ok"
    assert result["rating"] == "Strong Buy"
    assert result["ipo_score"] == 78
    assert result["scan_mode"] == "ipo_weighted"
    assert result["screeners_run"] == ["ipo"]
    assert result["unavailable_screeners"] == ["minervini"]
    assert _rs_fields(result) == (None, 66, 1, None)
    assert result["rs_universe_size"] == 8000
    assert result["market_rs_run_id"] == 1


def test_minervini_and_canslim_are_gated_not_crashed_below_full_history():
    result = _scan("NEWIPO", 245, ["ipo", "minervini", "canslim"], NO_VCP)

    assert result["result_status"] == "ok"
    assert result["screeners_run"] == ["ipo"]
    assert result["unavailable_screeners"] == ["minervini", "canslim"]
    assert _rs_fields(result) == (None, 66, 1, None)


def test_without_the_gate_minervini_would_degrade_the_whole_row(monkeypatch):
    monkeypatch.setattr(MinerviniScanner, "needs_overall_rs", lambda self, criteria=None: False)

    result = _scan("NEWIPO", 245, ["ipo", "minervini"], NO_VCP)

    assert result["rating"] == "Insufficient Data"
    assert "minervini" in result["reason"]


def test_a_young_listing_with_only_gated_screeners_keeps_its_placed_horizons():
    result = _scan("NEWIPO", 245, ["minervini", "canslim"], NO_VCP)

    assert result["rating"] == "Insufficient Data"
    assert result["result_status"] == "insufficient_history"
    assert result["unavailable_screeners"] == ["minervini", "canslim"]
    assert _rs_fields(result) == (None, 66, 1, None)


def test_an_eligible_stock_too_short_for_its_screeners_keeps_its_ratings():
    result = _scan("AAA", 100, ["minervini"], NO_VCP)

    assert result["rating"] == "Insufficient Data"
    assert result["unavailable_screeners"] == ["minervini"]
    assert _rs_fields(result) == (80, 70, 75, 90)


def test_exactly_252_bars_without_the_252_anchor_still_shows_the_horizons():
    result = _scan("NEWIPO", 252, ["ipo", "minervini"], NO_VCP)

    assert result["result_status"] == "ok"
    assert result["scan_mode"] == "full"
    assert result["screeners_run"] == ["ipo"]
    assert result["unavailable_screeners"] == ["minervini"]
    assert _rs_fields(result) == (None, 66, 1, None)


def test_volume_breakthrough_runs_on_a_young_listing_at_252_bars():
    result = _scan("NEWIPO", 252, ["volume_breakthrough"])

    assert result["screeners_run"] == ["volume_breakthrough"]
    assert result["unavailable_screeners"] == []
    assert _rs_fields(result) == (None, 66, 1, None)


def test_setup_engine_needs_the_overall_rating(monkeypatch):
    monkeypatch.setattr(settings, "setup_engine_enabled", True)

    young = _scan("NEWIPO", 300, ["ipo", "setup_engine"])
    eligible = _scan("AAA", 300, ["ipo", "setup_engine"])

    assert young["result_status"] == "ok"
    assert young["screeners_run"] == ["ipo"]
    assert young["unavailable_screeners"] == ["setup_engine"]
    assert _rs_fields(young) == (None, 66, 1, None)
    assert eligible["screeners_run"] == ["ipo", "setup_engine"]
    assert eligible["setup_engine"]["rs_rating"] == 80.0


def test_custom_is_gated_only_when_it_filters_on_the_overall_rating():
    gated = _scan("NEWIPO", 210, ["custom"], {"custom_filters": {"rs_rating_min": 80}})
    open_ = _scan("NEWIPO", 210, ["custom"], {"custom_filters": {"price_min": 5}})

    assert gated["rating"] == "Insufficient Data"
    assert gated["unavailable_screeners"] == ["custom"]
    assert open_["result_status"] == "ok"
    assert open_["screeners_run"] == ["custom"]


def test_a_stock_absent_from_the_run_still_fails_closed():
    result = _scan("GAP", 300, ["minervini", "ipo"], NO_VCP)

    assert result["rating"] == "Insufficient Data"
    assert result["unavailable_screeners"] == ["minervini", "ipo"]
    assert "Canonical RS is unavailable" in result["reason"]
    assert _rs_fields(result) == (None, None, None, None)


def test_an_eligible_stock_keeps_its_full_ratings():
    result = _scan("AAA", 300, ["minervini", "ipo"], NO_VCP)

    assert result["result_status"] == "ok"
    assert result["scan_mode"] == "full"
    assert result["screeners_run"] == ["minervini", "ipo"]
    assert _rs_fields(result) == (80, 70, 75, 90)


def test_the_default_profile_scores_a_young_listing_with_ipo(monkeypatch):
    monkeypatch.setattr(settings, "setup_engine_enabled", True)
    profile = get_default_scan_profile("US")

    for bars in (150, 245):
        result = _scan("NEWIPO", bars, profile["screeners"], profile["criteria"])

        assert result["result_status"] == "ok"
        assert result["screeners_run"] == ["ipo"]
        assert result["ipo_score"] == 78
        assert result["unavailable_screeners"] == [
            "minervini",
            "canslim",
            "custom",
            "volume_breakthrough",
            "setup_engine",
        ]
        assert _rs_fields(result) == (None, 66, 1, None)


def _setup_engine_bar_check(bars: int, end: str) -> tuple[str, list[str]]:
    result = SetupEngineScanner().scan_stock("SE", _stock("SE", bars, end))
    reason = str(result.details.get("reason") or "")
    return result.rating, [
        check
        for check in ("insufficient_daily_bars", "insufficient_weekly_bars")
        if check in reason
    ]


def _assert_setup_engine_rates(bars: int, end: str) -> None:
    rating, failures = _setup_engine_bar_check(bars, end)
    assert failures == []
    assert rating not in ("Error", "Insufficient Data")


def test_setup_engines_partition_minimum_always_meets_its_bar_policy():
    minimum = SetupEngineDataRequirements().min_history_bars

    assert minimum == 260
    for end in ("2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"):
        _assert_setup_engine_rates(minimum, end)
    # A Thursday close leaves four bars in the excluded current week.
    assert _setup_engine_bar_check(minimum - 1, "2026-10-15")[1] == ["insufficient_weekly_bars"]
    _assert_setup_engine_rates(minimum - 1, "2026-10-16")


def test_an_eligible_stock_short_of_setup_engines_weekly_history_keeps_its_other_screeners(
    monkeypatch,
):
    monkeypatch.setattr(settings, "setup_engine_enabled", True)
    profile = get_default_scan_profile("US")

    result = _scan("AAA", 255, profile["screeners"], profile["criteria"])

    assert result["result_status"] == "ok"
    assert result["screeners_run"] == [
        "minervini",
        "canslim",
        "ipo",
        "custom",
        "volume_breakthrough",
    ]
    assert result["unavailable_screeners"] == ["setup_engine"]
    assert _rs_fields(result) == (80, 70, 75, 90)


def test_setup_engine_runs_on_a_history_short_of_260_bars_that_holds_52_completed_weeks(
    monkeypatch,
):
    monkeypatch.setattr(settings, "setup_engine_enabled", True)
    profile = get_default_scan_profile("US")

    friday = _scan("AAA", 259, profile["screeners"], profile["criteria"], end="2026-10-16")
    thursday = _scan("AAA", 259, profile["screeners"], profile["criteria"], end="2026-10-15")

    assert friday["result_status"] == "ok"
    assert "setup_engine" in friday["screeners_run"]
    assert friday["unavailable_screeners"] == []
    assert thursday["result_status"] == "ok"
    assert thursday["unavailable_screeners"] == ["setup_engine"]


def _evidence_rs(result: dict) -> tuple:
    metrics = result["opportunity_state"]["metrics"]
    return metrics["rs_rating_1m"], metrics["rs_rating_3m"]


def test_a_young_listings_opportunity_evidence_reads_its_placed_horizons():
    result = _scan("NEWIPO", 150, ["ipo"])

    assert _rs_fields(result) == (None, 66, 1, None)
    assert _evidence_rs(result) == (66.0, 1.0)


def test_an_eligible_stock_scanned_without_minervini_carries_its_ratings():
    result = _scan("AAA", 300, ["ipo"])

    assert result["screeners_run"] == ["ipo"]
    assert _rs_fields(result) == (80, 70, 75, 90)
    assert _evidence_rs(result) == (70.0, 75.0)
