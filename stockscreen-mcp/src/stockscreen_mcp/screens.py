"""Screen evaluation, ported from frontend/src/features/scan/filterExpressionEvaluator.js.

Kept line-for-line equivalent so a preset matches exactly the rows the static
site shows for it.
"""

from __future__ import annotations

import math
from typing import Any

# frontend/src/features/scan/scanFilterFields.js: OPPORTUNITY_STATE_FIELDS
OPPORTUNITY_STATE_FIELDS = frozenset({"correction_survivor", "resilience_score", "action_state"})


def _first_present(row: dict[str, Any], field: str, fallback: str) -> Any:
    value = row.get(field)
    return row.get(fallback) if value is None else value


def row_value(row: dict[str, Any], field: str) -> Any:
    if field == "price":
        return _first_present(row, "price", "current_price")
    if field == "listing_search":
        return f"{row.get('symbol') or ''} {row.get('company_name') or ''}".strip()
    if field == "listing_aware_volume":
        return math.inf if row.get("scan_mode") == "listing_only" else row.get("volume")
    if field == "price_change_1d":
        return _first_present(row, "price_change_1d", "pct_day")
    if field == "perf_week":
        return _first_present(row, "perf_week", "pct_week")
    if field == "perf_month":
        return _first_present(row, "perf_month", "pct_month")
    if field == "market_cap_usd":
        if row.get("market_cap_usd") is not None:
            return row["market_cap_usd"]
        usd = str(row.get("currency") or "").upper() == "USD" or row.get("market") == "US"
        return row.get("market_cap") if usd else None
    return row.get(field)


def _less(a: Any, b: Any) -> bool:
    try:
        return a < b
    except TypeError:  # JS: "n/a" < 70 is false (NaN), so the bound is not violated
        return False


def _range(row: dict[str, Any], condition: dict[str, Any]) -> bool:
    value = row_value(row, condition["field"])
    if value is None:
        return False
    if condition.get("min") is not None and _less(value, condition["min"]):
        return False
    if condition.get("max") is not None and _less(condition["max"], value):
        return False
    return True


def _categorical(row: dict[str, Any], condition: dict[str, Any]) -> bool:
    value = row_value(row, condition["field"])
    included = value is not None and value in condition.get("values", [])
    if condition.get("mode") == "exclude":
        return value is None or not included
    return included


def _boolean(row: dict[str, Any], condition: dict[str, Any]) -> bool:
    value = row_value(row, condition["field"])
    return isinstance(value, bool) and value == condition.get("value")


def _text(row: dict[str, Any], condition: dict[str, Any]) -> bool:
    value = row_value(row, condition["field"])
    return value is not None and str(condition.get("pattern", "")).lower() in str(value).lower()


_EVALUATORS = {"range": _range, "categorical": _categorical, "boolean": _boolean, "text": _text}


def evaluate_condition(row: dict[str, Any], condition: dict[str, Any]) -> bool:
    evaluator = _EVALUATORS.get(condition.get("kind"))
    return bool(evaluator and evaluator(row, condition))


def evaluate_group(row: dict[str, Any], group: dict[str, Any]) -> bool:
    if group.get("enabled") is False:
        return False
    matches = [evaluate_condition(row, c) for c in group.get("conditions") or []]
    return any(matches) if group.get("match") == "any" else all(matches)


def evaluate_expression(row: dict[str, Any], expression: dict[str, Any]) -> bool:
    if not evaluate_group(row, expression["required"]):
        return False
    groups = [g for g in expression.get("groups") or [] if g.get("enabled") is not False]
    if not groups:
        return True
    matches = [evaluate_group(row, g) for g in groups]
    return all(matches) if expression.get("group_join") == "all" else any(matches)


def expression_fields(expression: dict[str, Any]) -> list[str]:
    groups = [expression["required"], *(expression.get("groups") or [])]
    return [c["field"] for g in groups for c in g.get("conditions") or []]


def preset_requires_opportunity_state(preset: dict[str, Any]) -> bool:
    """opportunityCapabilityPolicy.js: hidden where the market lacks opportunity_state."""
    fields = {*expression_fields(preset["filter_expression"]), preset.get("sort_by")}
    return bool(fields & OPPORTUNITY_STATE_FIELDS)


def filters_to_conditions(filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tool-facing {field, min, max, equals} filters as frontend expression conditions."""
    conditions = []
    for f in filters:
        equals = f.get("equals")
        if isinstance(equals, bool):
            conditions.append({"field": f["field"], "kind": "boolean", "value": equals})
        elif isinstance(equals, str):
            conditions.append({"field": f["field"], "kind": "categorical", "values": [equals], "mode": "include"})
        elif equals is not None:
            conditions.append({"field": f["field"], "kind": "range", "min": equals, "max": equals})
        else:
            conditions.append({"field": f["field"], "kind": "range", "min": f.get("min"), "max": f.get("max")})
    return conditions


def sort_rows(rows: list[dict[str, Any]], field: str, *, descending: bool) -> list[dict[str, Any]]:
    """Sort by a field with missing values last in either direction."""
    present = [r for r in rows if row_value(r, field) is not None]
    missing = [r for r in rows if row_value(r, field) is None]
    present.sort(key=lambda r: row_value(r, field), reverse=descending)
    return present + missing
