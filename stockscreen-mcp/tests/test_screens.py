"""The evaluator mirrors frontend/src/features/scan/filterExpressionEvaluator.js."""

import pytest

from stockscreen_mcp.screens import (
    evaluate_condition,
    evaluate_expression,
    filters_to_conditions,
    preset_requires_opportunity_state,
    sort_rows,
)

from conftest import PRESETS, condition


@pytest.mark.parametrize(
    ("row", "cond", "expected"),
    [
        ({"rs_rating": 80}, condition("rs_rating", "range", min=70, max=None), True),
        ({"rs_rating": 70}, condition("rs_rating", "range", min=70, max=None), True),
        ({"rs_rating": 69}, condition("rs_rating", "range", min=70, max=None), False),
        ({"stage": 3}, condition("stage", "range", min=2, max=2), False),
        # Missing and null values never pass a range, even an open one.
        ({}, condition("rs_rating", "range", min=None, max=None), False),
        ({"rs_rating": None}, condition("rs_rating", "range", min=0, max=None), False),
        # JS compares a string to a number as NaN (false), so neither bound rejects it.
        ({"rs_rating": "n/a"}, condition("rs_rating", "range", min=70, max=99), True),
        # Booleans need a real boolean, not a truthy string.
        ({"ma_alignment": True}, condition("ma_alignment", "boolean", value=True), True),
        ({"ma_alignment": "true"}, condition("ma_alignment", "boolean", value=True), False),
        ({"ma_alignment": 1}, condition("ma_alignment", "boolean", value=True), False),
        # Categorical include / exclude; exclude lets missing values through.
        ({"p": "cup"}, condition("p", "categorical", values=["cup"], mode="include"), True),
        ({"p": None}, condition("p", "categorical", values=["cup"], mode="include"), False),
        ({"p": "cup"}, condition("p", "categorical", values=["cup"], mode="exclude"), False),
        ({}, condition("p", "categorical", values=["cup"], mode="exclude"), True),
        # Field aliases used by the published presets.
        ({"current_price": 12}, condition("price", "range", min=10, max=None), True),
        ({"pct_day": 5}, condition("price_change_1d", "range", min=4, max=None), True),
        ({"pct_week": 25}, condition("perf_week", "range", min=20, max=None), True),
        ({"pct_month": 25}, condition("perf_month", "range", min=20, max=None), True),
        ({"market_cap": 5e9, "currency": "USD"}, condition("market_cap_usd", "range", min=1e9, max=None), True),
        ({"market_cap": 5e9, "currency": "HKD", "market": "HK"}, condition("market_cap_usd", "range", min=1e9, max=None), False),
        ({"scan_mode": "listing_only"}, condition("listing_aware_volume", "range", min=1e8, max=None), True),
        # Unknown kinds never match.
        ({"x": 1}, condition("x", "weird"), False),
    ],
)
def test_condition_matches_the_frontend(row, cond, expected):
    assert evaluate_condition(row, cond) is expected


def test_expression_requires_all_required_conditions_and_any_group():
    expression = {
        "required": {"match": "all", "conditions": [condition("a", "range", min=1, max=None)]},
        "groups": [
            {"enabled": True, "match": "all", "conditions": [condition("b", "boolean", value=True)]},
            {"enabled": True, "match": "all", "conditions": [condition("c", "boolean", value=True)]},
        ],
        "group_join": "any",
    }
    assert evaluate_expression({"a": 1, "b": True}, expression)
    assert not evaluate_expression({"a": 1}, expression)
    assert not evaluate_expression({"a": 0, "b": True}, expression)
    assert evaluate_expression({"a": 1, "b": True, "c": True}, expression | {"group_join": "all"})
    assert not evaluate_expression({"a": 1, "b": True}, expression | {"group_join": "all"})


def test_presets_using_opportunity_state_fields_are_flagged():
    flagged = {p["id"] for p in PRESETS if preset_requires_opportunity_state(p)}
    assert flagged == {"correction_survivors"}
    assert preset_requires_opportunity_state(PRESETS[0] | {"sort_by": "resilience_score"})


def test_filters_become_frontend_conditions():
    conditions = filters_to_conditions([
        {"field": "rs_rating", "min": 90},
        {"field": "stage", "min": 2, "max": 2},
        {"field": "ma_alignment", "equals": True},
        {"field": "se_pattern_primary", "equals": "cup_handle"},
    ])
    assert conditions == [
        {"field": "rs_rating", "kind": "range", "min": 90, "max": None},
        {"field": "stage", "kind": "range", "min": 2, "max": 2},
        {"field": "ma_alignment", "kind": "boolean", "value": True},
        {"field": "se_pattern_primary", "kind": "categorical", "values": ["cup_handle"], "mode": "include"},
    ]


ROWS = [
    {"symbol": "D", "v": 2, "composite_score": 2},
    {"symbol": "A", "v": None, "composite_score": None},
    {"symbol": "C", "v": 2, "composite_score": 2},
    {"symbol": "B", "v": 5, "composite_score": 5},
    {"symbol": "E"},
]


def symbols(rows):
    return [r["symbol"] for r in rows]


def test_site_order_breaks_ties_by_symbol_in_both_directions():
    # sortStaticScanRows: the symbol tiebreak is not reversed for descending sorts.
    assert symbols(sort_rows(ROWS, "v", descending=True))[-2:] == ["C", "D"]
    assert symbols(sort_rows(ROWS, "v", descending=False))[:2] == ["C", "D"]


def test_site_order_puts_nulls_first_when_descending_except_for_score_fields():
    assert symbols(sort_rows(ROWS, "v", descending=True)) == ["A", "E", "B", "C", "D"]
    assert symbols(sort_rows(ROWS, "v", descending=False)) == ["C", "D", "B", "A", "E"]
    assert symbols(sort_rows(ROWS, "composite_score", descending=True)) == ["B", "C", "D", "A", "E"]
    assert symbols(sort_rows(ROWS, "composite_score", descending=False)) == ["C", "D", "B", "A", "E"]


def test_caller_sorts_keep_rows_without_the_field_last():
    assert symbols(sort_rows(ROWS, "v", descending=True, site_order=False)) == ["B", "C", "D", "A", "E"]


@pytest.mark.parametrize(
    ("row", "cond", "expected"),
    [
        # JS converts numeric strings for < and >: "50" < 70 rejects the row.
        ({"rs_rating": "50"}, condition("rs_rating", "range", min=70, max=None), False),
        ({"rs_rating": "80"}, condition("rs_rating", "range", min=70, max=None), True),
        # Strict equality: true is not 1 in includes() or ===.
        ({"p": 1}, condition("p", "categorical", values=[True], mode="include"), False),
        ({"p": True}, condition("p", "categorical", values=[1], mode="include"), False),
        ({"p": 1.0}, condition("p", "categorical", values=[1], mode="include"), True),
        ({"flag": True}, condition("flag", "boolean", value=1), False),
    ],
)
def test_js_coercion_and_strict_equality(row, cond, expected):
    assert evaluate_condition(row, cond) is expected
