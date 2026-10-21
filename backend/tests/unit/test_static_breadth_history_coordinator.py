from __future__ import annotations

from datetime import date
from types import MappingProxyType, SimpleNamespace

import pytest

from app.models.market_breadth import MarketBreadth
from app.models.stock_universe import StockUniverse
from app.scripts import export_static_site
from app.services.static_breadth_eligibility import StaticBreadthEligibility
from app.services.static_breadth_history_coordinator import (
    StaticBreadthHistoryCoordinator,
    StaticBreadthHistoryRequest,
)


def _unexpected(*_args, **_kwargs):
    raise AssertionError("dependency should not be called")


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _breadth_signature(calculation_date):
    return f"signature-{calculation_date.isoformat()}"


def _run_ensure_breadth_history(monkeypatch, *, session, trading_dates, calculator, **kwargs):
    monkeypatch.setattr(export_static_site, "SessionLocal", lambda: session())
    monkeypatch.setattr(export_static_site, "_generate_trading_dates", lambda *args, **kwargs: trading_dates)
    monkeypatch.setattr(export_static_site, "get_price_cache", lambda: object())
    monkeypatch.setattr(export_static_site, "BreadthCalculatorService", calculator)
    return export_static_site._ensure_breadth_history(**kwargs)


def _patch_breadth_eligibility(
    monkeypatch,
    eligible_counts_by_date,
    *,
    candidate_counts_by_date=None,
    policy="point_in_time",
):
    eligible_counts = dict(eligible_counts_by_date)
    candidate_counts = dict(candidate_counts_by_date or eligible_counts)

    def classify(_db, *, market, calculation_dates):
        del market
        eligible_symbols = {
            calculation_date: tuple(
                f"ELIGIBLE-{index}"
                for index in range(eligible_counts.get(calculation_date, 0))
            )
            for calculation_date in calculation_dates
        }
        return StaticBreadthEligibility(
            eligible_symbols_by_date=MappingProxyType(eligible_symbols),
            candidate_counts_by_date=MappingProxyType(
                {
                    calculation_date: candidate_counts.get(calculation_date, 0)
                    for calculation_date in calculation_dates
                }
            ),
            eligible_counts_by_date=MappingProxyType(
                {
                    calculation_date: eligible_counts.get(calculation_date, 0)
                    for calculation_date in calculation_dates
                }
            ),
            universe_policy_by_date=MappingProxyType(
                {calculation_date: policy for calculation_date in calculation_dates}
            ),
            eligibility_signatures_by_date=MappingProxyType(
                {
                    calculation_date: _breadth_signature(calculation_date)
                    for calculation_date in calculation_dates
                }
            ),
            unsupported_count=0,
            insufficient_history_count=0,
            exact_date_gap_count=0,
            unsupported_symbols=(),
            insufficient_history_symbols=(),
            exact_date_gap_symbols=(),
        )

    monkeypatch.setattr(
        export_static_site,
        "classify_static_breadth_eligibility",
        classify,
    )


def test_empty_trading_window_skips_without_opening_database_session():
    coordinator = StaticBreadthHistoryCoordinator(
        session_factory=_unexpected,
        trading_dates=lambda _start, _end, _market: (),
        eligibility_classifier=_unexpected,
        calculator_factory=_unexpected,
        price_cache_factory=_unexpected,
    )

    result = coordinator.ensure(
        StaticBreadthHistoryRequest(
            market="hk",
            as_of_date=date(2026, 3, 20),
            min_trading_days=20,
            lookback_days=90,
        )
    )

    assert result.as_dict() == {
        "status": "skipped",
        "market": "HK",
        "as_of_date": "2026-03-20",
        "lookback_start_date": "2025-12-20",
        "target_trading_days": 0,
        "recomputed_dates": 0,
    }

def test_ensure_breadth_history_marks_backfill_errors_not_completed(monkeypatch):
    as_of_date = date(2026, 7, 31)
    _patch_breadth_eligibility(monkeypatch, {as_of_date: 1})
    backfill_kwargs: dict[str, object] = {}

    class _FakeQuery:
        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return []

    class _FakeDb(_FakeSession):
        def query(self, *args, **kwargs):
            return _FakeQuery()

    class _FakeBreadthCalculator:
        def __init__(self, db, price_cache, *, market):
            self.market = market

        def backfill_range(self, **kwargs):
            backfill_kwargs.update(kwargs)
            return {
                "total_dates": 1,
                "processed": 0,
                "errors": 1,
                "error_dates": [as_of_date.isoformat()],
            }

    result = _run_ensure_breadth_history(
        monkeypatch,
        session=_FakeDb,
        trading_dates=[as_of_date],
        calculator=_FakeBreadthCalculator,
        as_of_date=as_of_date,
        market="HK",
        min_trading_days=0,
    )

    assert result["status"] == "errored"
    assert result["errors"] == 1
    assert result["error_dates"] == ["2026-07-31"]
    assert result["hard_error_dates"] == ["2026-07-31"]
    assert result["unclassified_error_count"] == 0
    assert "tolerated_error_dates" not in result
    assert result["error"] == (
        "Cache-only breadth backfill has hard date errors "
        "(dates=2026-07-31)"
    )
    assert backfill_kwargs["exclude_unsupported_price_symbols"] is True
    assert backfill_kwargs["required_as_of_date"] == as_of_date

def test_ensure_breadth_history_recomputes_ratio_window_after_historical_repair(
    monkeypatch,
):
    target_dates = [
        date(2026, 7, 16),
        date(2026, 7, 17),
        date(2026, 7, 20),
        date(2026, 7, 21),
        date(2026, 7, 22),
        date(2026, 7, 23),
        date(2026, 7, 24),
        date(2026, 7, 27),
        date(2026, 7, 28),
        date(2026, 7, 29),
        date(2026, 7, 30),
        date(2026, 7, 31),
    ]
    repair_date = target_dates[1]
    as_of_date = target_dates[-1]
    _patch_breadth_eligibility(
        monkeypatch, {calculation_date: 1 for calculation_date in target_dates}
    )
    backfill_kwargs: dict[str, object] = {}

    class _FakeQuery:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return self.rows

    class _FakeDb(_FakeSession):
        def query(self, entity, *args):
            if entity is MarketBreadth:
                return _FakeQuery([
                    SimpleNamespace(
                        date=calc_date,
                        total_stocks_scanned=1,
                        advance_decline_eligible_count=1,
                        eligibility_signature=_breadth_signature(calc_date),
                    )
                    for calc_date in target_dates
                    if calc_date != repair_date
                ])
            if entity is StockUniverse.symbol:
                return _FakeQuery([("AAA",)])
            return _FakeQuery([])

    class _FakeBreadthCalculator:
        def __init__(self, db, price_cache, *, market):
            self.market = market

        def backfill_range(self, **kwargs):
            backfill_kwargs.update(kwargs)
            return {
                "total_dates": len(kwargs["trading_dates"]),
                "processed": len(kwargs["trading_dates"]),
                "errors": 0,
                "error_dates": [],
                "target_symbols": 1,
                "symbols_with_cached_history": 1,
                "cache_miss_stocks": 0,
                "error_stocks": 0,
                "cache_coverage_ratio": 1.0,
                "scanned_stocks_by_date": {
                    calculation_date.isoformat(): 1
                    for calculation_date in kwargs["trading_dates"]
                },
                "broad_universe_stocks_by_date": {
                    calculation_date.isoformat(): 1
                    for calculation_date in kwargs["trading_dates"]
                },
            }

    result = _run_ensure_breadth_history(
        monkeypatch,
        session=_FakeDb,
        trading_dates=target_dates,
        calculator=_FakeBreadthCalculator,
        as_of_date=as_of_date,
        market="HK",
        min_trading_days=0,
    )

    assert result["status"] == "completed"
    assert result["recomputed_dates"] == 11
    assert backfill_kwargs["trading_dates"] == target_dates[1:]

def test_ensure_breadth_history_skips_validated_existing_rows(monkeypatch):
    as_of_date = date(2026, 7, 31)
    _patch_breadth_eligibility(monkeypatch, {as_of_date: 2})

    class _FakeQuery:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return self.rows

    class _FakeDb(_FakeSession):
        def query(self, entity, *args):
            if entity is MarketBreadth:
                return _FakeQuery([
                    SimpleNamespace(
                        date=as_of_date,
                        total_stocks_scanned=2,
                        advance_decline_eligible_count=2,
                        eligibility_signature=_breadth_signature(as_of_date),
                    )
                ])
            if entity is StockUniverse.symbol:
                return _FakeQuery([("AAA",), ("BBB",)])
            return _FakeQuery([])

    monkeypatch.setattr(export_static_site, "SessionLocal", lambda: _FakeDb())
    monkeypatch.setattr(
        export_static_site,
        "_generate_trading_dates",
        lambda *args, **kwargs: [as_of_date],
    )
    monkeypatch.setattr(
        export_static_site,
        "BreadthCalculatorService",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("validated existing breadth should not recompute")
        ),
    )

    result = export_static_site._ensure_breadth_history(
        as_of_date=as_of_date,
        market="HK",
        min_trading_days=0,
    )

    assert result["status"] == "skipped"
    assert result["validated_existing_dates"] == 1
    assert result["eligible_stocks_by_date"] == {"2026-07-31": 2}


def test_existing_signature_accepts_ad_denominator_below_broad_universe(monkeypatch):
    as_of_date = date(2026, 7, 31)
    _patch_breadth_eligibility(monkeypatch, {as_of_date: 2})

    class _FakeQuery:
        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return [
                SimpleNamespace(
                    date=as_of_date,
                    total_stocks_scanned=2,
                    advance_decline_eligible_count=1,
                    eligibility_signature=_breadth_signature(as_of_date),
                )
            ]

    class _FakeDb(_FakeSession):
        def query(self, *args, **kwargs):
            return _FakeQuery()

    monkeypatch.setattr(export_static_site, "SessionLocal", lambda: _FakeDb())
    monkeypatch.setattr(
        export_static_site,
        "_generate_trading_dates",
        lambda *args, **kwargs: [as_of_date],
    )
    monkeypatch.setattr(
        export_static_site,
        "BreadthCalculatorService",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("matching broad-universe signature must be reused")
        ),
    )

    result = export_static_site._ensure_breadth_history(
        as_of_date=as_of_date,
        market="US",
        min_trading_days=0,
    )

    assert result["status"] == "skipped"
    assert result["validated_existing_dates"] == 1


@pytest.mark.parametrize("market", ["US", "DE", "HK"])
def test_historical_breadth_reuses_rows_eligible_for_their_date(monkeypatch, market):
    as_of_date = date(2026, 7, 31)
    _patch_breadth_eligibility(
        monkeypatch,
        {as_of_date: 7},
        candidate_counts_by_date={as_of_date: 10},
    )

    class _FakeQuery:
        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return [
                SimpleNamespace(
                    date=as_of_date,
                    total_stocks_scanned=7,
                    advance_decline_eligible_count=7,
                    eligibility_signature=_breadth_signature(as_of_date),
                )
            ]

    class _FakeDb(_FakeSession):
        def query(self, *args, **kwargs):
            return _FakeQuery()

    monkeypatch.setattr(export_static_site, "SessionLocal", lambda: _FakeDb())
    monkeypatch.setattr(
        export_static_site,
        "_generate_trading_dates",
        lambda *args, **kwargs: [as_of_date],
    )
    monkeypatch.setattr(
        export_static_site,
        "BreadthCalculatorService",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("date-eligible existing breadth must be reused")
        ),
    )

    result = export_static_site._ensure_breadth_history(
        as_of_date=as_of_date,
        market=market,
        min_trading_days=0,
    )

    assert result["status"] == "skipped"
    assert result["candidate_stocks_by_date"] == {"2026-07-31": 10}
    assert result["eligible_stocks_by_date"] == {"2026-07-31": 7}

def test_legacy_breadth_row_with_larger_count_recomputes_without_matching_signature(
    monkeypatch,
):
    as_of_date = date(2026, 7, 31)
    _patch_breadth_eligibility(
        monkeypatch,
        {as_of_date: 7},
        candidate_counts_by_date={as_of_date: 10},
    )
    calls = []

    class _FakeQuery:
        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return [
                SimpleNamespace(
                    date=as_of_date,
                    total_stocks_scanned=10,
                    eligibility_signature=None,
                )
            ]

    class _FakeDb(_FakeSession):
        def query(self, *args, **kwargs):
            return _FakeQuery()

    class _FakeBreadthCalculator:
        def __init__(self, *args, **kwargs):
            pass

        def backfill_range(self, **kwargs):
            calls.append(kwargs)
            return {
                "total_dates": 1,
                "processed": 1,
                "errors": 0,
                "error_dates": [],
                "error_stocks": 0,
                "scanned_stocks_by_date": {as_of_date.isoformat(): 7},
                "broad_universe_stocks_by_date": {as_of_date.isoformat(): 7},
            }

    result = _run_ensure_breadth_history(
        monkeypatch,
        session=_FakeDb,
        trading_dates=[as_of_date],
        calculator=_FakeBreadthCalculator,
        as_of_date=as_of_date,
        market="US",
        min_trading_days=0,
    )

    assert result["status"] == "completed"
    assert result["incomplete_existing_dates"] == 1
    assert len(calls) == 1
    assert calls[0]["eligibility_signatures_by_date"] == {
        as_of_date: _breadth_signature(as_of_date)
    }

def test_ensure_breadth_history_skips_existing_rows_with_tolerated_historical_gaps(
    monkeypatch,
):
    previous_date = date(2026, 7, 30)
    as_of_date = date(2026, 7, 31)
    _patch_breadth_eligibility(
        monkeypatch,
        {previous_date: 9, as_of_date: 10},
    )

    class _FakeQuery:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return self.rows

    class _FakeDb(_FakeSession):
        def query(self, entity, *args):
            if entity is MarketBreadth:
                return _FakeQuery([
                    SimpleNamespace(
                        date=previous_date,
                        total_stocks_scanned=9,
                        advance_decline_eligible_count=9,
                        eligibility_signature=_breadth_signature(previous_date),
                    ),
                    SimpleNamespace(
                        date=as_of_date,
                        total_stocks_scanned=10,
                        advance_decline_eligible_count=10,
                        eligibility_signature=_breadth_signature(as_of_date),
                    ),
                ])
            if entity is StockUniverse.symbol:
                return _FakeQuery([
                    (f"AAA{i}",)
                    for i in range(10)
                ])
            return _FakeQuery([])

    monkeypatch.setattr(export_static_site, "SessionLocal", lambda: _FakeDb())
    monkeypatch.setattr(
        export_static_site,
        "_generate_trading_dates",
        lambda *args, **kwargs: [previous_date, as_of_date],
    )
    monkeypatch.setattr(
        export_static_site,
        "BreadthCalculatorService",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("accepted historical coverage should not recompute")
        ),
    )

    result = export_static_site._ensure_breadth_history(
        as_of_date=as_of_date,
        market="HK",
        min_trading_days=0,
    )

    assert result["status"] == "skipped"
    assert result["validated_existing_dates"] == 2
    assert result["eligible_stocks_by_date"] == {
        "2026-07-30": 9,
        "2026-07-31": 10,
    }

def test_ensure_breadth_history_marks_calculation_errors_not_completed(monkeypatch):
    as_of_date = date(2026, 7, 31)
    _patch_breadth_eligibility(monkeypatch, {as_of_date: 2})

    class _FakeQuery:
        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return []

    class _FakeDb(_FakeSession):
        def query(self, *args, **kwargs):
            return _FakeQuery()

    class _FakeBreadthCalculator:
        def __init__(self, db, price_cache, *, market):
            self.market = market

        def backfill_range(self, **kwargs):
            return {
                "total_dates": 1,
                "processed": 1,
                "errors": 0,
                "error_dates": [],
                "target_symbols": 2,
                "symbols_with_cached_history": 2,
                "cache_miss_stocks": 0,
                "error_stocks": 1,
                "cache_coverage_ratio": 1.0,
            }

    result = _run_ensure_breadth_history(
        monkeypatch,
        session=_FakeDb,
        trading_dates=[as_of_date],
        calculator=_FakeBreadthCalculator,
        as_of_date=as_of_date,
        market="HK",
        min_trading_days=0,
    )

    assert result["status"] == "errored"
    assert result["error_stocks"] == 1
    assert result["error"] == (
        "Cache-only breadth backfill has calculation errors "
        "(error_stocks=1)"
    )

def test_ensure_breadth_history_marks_undercovered_backfill_rows_not_completed(
    monkeypatch,
):
    as_of_date = date(2026, 7, 31)
    _patch_breadth_eligibility(
        monkeypatch,
        {as_of_date: 8},
        candidate_counts_by_date={as_of_date: 10},
    )
    breadth_rows: list[SimpleNamespace] = []

    class _FakeQuery:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return self.rows

    class _FakeDb(_FakeSession):
        def query(self, entity, *args):
            if entity is MarketBreadth:
                return _FakeQuery(breadth_rows)
            if entity is StockUniverse.symbol:
                return _FakeQuery([(f"AAA{i}",) for i in range(10)])
            return _FakeQuery([])

    class _FakeBreadthCalculator:
        def __init__(self, db, price_cache, *, market):
            self.market = market

        def backfill_range(self, **kwargs):
            breadth_rows.append(
                SimpleNamespace(
                    date=as_of_date,
                    total_stocks_scanned=1,
                    advance_decline_eligible_count=1,
                )
            )
            return {
                "total_dates": 1,
                "processed": 1,
                "errors": 0,
                "error_dates": [],
                "target_symbols": 10,
                "symbols_with_cached_history": 10,
                "cache_miss_stocks": 0,
                "error_stocks": 0,
                "cache_coverage_ratio": 1.0,
                "insufficient_history_observations": 9,
                "scanned_stocks_by_date": {as_of_date.isoformat(): 1},
                "broad_universe_stocks_by_date": {as_of_date.isoformat(): 1},
                "advance_decline_eligible_stocks_by_date": {
                    as_of_date.isoformat(): 8
                },
            }

    result = _run_ensure_breadth_history(
        monkeypatch,
        session=_FakeDb,
        trading_dates=[as_of_date],
        calculator=_FakeBreadthCalculator,
        as_of_date=as_of_date,
        market="HK",
        min_trading_days=0,
    )

    assert result["status"] == "errored"
    assert result["undercovered_dates"] == ["2026-07-31"]
    assert result["eligible_stocks_by_date"] == {"2026-07-31": 8}
    assert result["scanned_stocks_by_date"] == {"2026-07-31": 1}
    assert result["error"] == (
        "Cache-only breadth backfill has insufficient usable coverage "
        "(scanned/eligible=2026-07-31:1/8)"
    )

def test_ensure_breadth_history_accepts_smaller_historical_eligible_universe(
    monkeypatch,
):
    as_of_date = date(2026, 7, 31)
    _patch_breadth_eligibility(
        monkeypatch,
        {as_of_date: 7},
        candidate_counts_by_date={as_of_date: 10},
        policy="current_active_fallback_v1",
    )
    breadth_rows: list[SimpleNamespace] = []

    class _FakeQuery:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return self.rows

    class _FakeDb(_FakeSession):
        def query(self, entity, *args):
            if entity is MarketBreadth:
                return _FakeQuery(breadth_rows)
            if entity is StockUniverse.symbol:
                return _FakeQuery([(f"AAA{i}",) for i in range(10)])
            return _FakeQuery([])

    class _FakeBreadthCalculator:
        def __init__(self, db, price_cache, *, market):
            self.market = market

        def backfill_range(self, **kwargs):
            breadth_rows.append(
                SimpleNamespace(
                    date=as_of_date,
                    total_stocks_scanned=7,
                )
            )
            return {
                "total_dates": 1,
                "processed": 1,
                "errors": 0,
                "error_dates": [],
                "target_symbols": 10,
                "symbols_with_cached_history": 7,
                "cache_miss_stocks": 3,
                "error_stocks": 0,
                "cache_coverage_ratio": 0.7,
                "insufficient_history_observations": 3,
                "scanned_stocks_by_date": {as_of_date.isoformat(): 7},
                "broad_universe_stocks_by_date": {as_of_date.isoformat(): 7},
            }

    result = _run_ensure_breadth_history(
        monkeypatch,
        session=_FakeDb,
        trading_dates=[as_of_date],
        calculator=_FakeBreadthCalculator,
        as_of_date=as_of_date,
        market="CA",
        min_trading_days=0,
    )

    assert result["status"] == "completed"
    assert result["candidate_stocks_by_date"] == {"2026-07-31": 10}
    assert result["eligible_stocks_by_date"] == {"2026-07-31": 7}
    assert result["scanned_stocks_by_date"] == {"2026-07-31": 7}
    assert result["universe_policy_by_date"] == {
        "2026-07-31": "current_active_fallback_v1"
    }
    assert "undercovered_dates" not in result
    assert "error" not in result


def test_ensure_breadth_history_accepts_metric_specific_history_gaps(monkeypatch):
    as_of_date = date(2026, 7, 31)
    _patch_breadth_eligibility(monkeypatch, {as_of_date: 8})
    breadth_rows: list[SimpleNamespace] = []

    class _FakeQuery:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return self.rows

    class _FakeDb(_FakeSession):
        def query(self, entity, *args):
            if entity is MarketBreadth:
                return _FakeQuery(breadth_rows)
            return _FakeQuery([])

    class _FakeBreadthCalculator:
        def __init__(self, db, price_cache, *, market):
            self.market = market

        def backfill_range(self, **kwargs):
            return {
                "total_dates": 1,
                "processed": 1,
                "errors": 0,
                "error_dates": [],
                "error_stocks": 0,
                "scanned_stocks_by_date": {as_of_date.isoformat(): 1},
                "broad_universe_stocks_by_date": {as_of_date.isoformat(): 8},
            }

    result = _run_ensure_breadth_history(
        monkeypatch,
        session=_FakeDb,
        trading_dates=[as_of_date],
        calculator=_FakeBreadthCalculator,
        as_of_date=as_of_date,
        market="US",
        min_trading_days=0,
    )

    assert result["status"] == "completed"
    assert result["scanned_stocks_by_date"] == {"2026-07-31": 1}
    assert "undercovered_dates" not in result
