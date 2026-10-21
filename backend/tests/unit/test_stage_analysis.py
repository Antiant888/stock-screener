"""Regression coverage for newest-first price inputs, without scanner bootstrap."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


_path = Path(__file__).resolve().parents[2] / "app/scanners/criteria/stage_analysis.py"
_spec = spec_from_file_location("stage_analysis", _path)
_module = module_from_spec(_spec)
_spec.loader.exec_module(_module)
WeinsteinstageAnalyzer = _module.WeinsteinstageAnalyzer


@pytest.mark.parametrize(
    "start,end,expected",
    [(80, 120, "uptrend"), (120, 80, "downtrend"), (100, 100, "sideways")],
)
def test_price_trend_uses_latest_window_in_chronological_order(start, end, expected):
    # Opposing old history also catches reversing the full series before slicing.
    old_history = np.linspace(end * 2, start, 260)
    recent = np.linspace(start, end, 60)
    prices = pd.Series(np.concatenate([old_history, recent])[::-1])
    assert WeinsteinstageAnalyzer().calculate_price_trend(prices) == expected


@pytest.mark.parametrize(
    "start,end,expected_stage,expected_trend",
    [(60, 120, 2, "uptrend"), (120, 60, 4, "downtrend"), (100, 100, 1, "sideways")],
)
def test_determine_stage_with_newest_first_inputs(start, end, expected_stage, expected_trend):
    closes = pd.Series(np.linspace(start, end, 320))
    ma_200 = closes.rolling(200).mean()
    result = WeinsteinstageAnalyzer().determine_stage(
        current_price=float(closes.iloc[-1]),
        ma_200=float(ma_200.iloc[-1]),
        ma_200_series=ma_200.iloc[::-1].reset_index(drop=True),
        price_series=closes.iloc[::-1].reset_index(drop=True),
    )
    assert result["price_trend"] == expected_trend
    assert result["stage"] == expected_stage
