from datetime import date, timedelta

import pytest

from app.domain.relative_strength import (
    BALANCED_RS_FORMULA_VERSION,
    BALANCED_RS_PRICE_BASIS,
    BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
    HORIZONS,
)
from app.infra.db.models.relative_strength import MarketRsRun, StockRsSnapshot
from app.infra.db.repositories.market_rs_repo import MarketRsRunRepository
from app.models.industry import IBDIndustryGroup
from app.services.canonical_group_ranking_service import CanonicalGroupRankingService
from app.services.market_rs_inputs import MarketRsInputs, MarketRsInputUnavailable
from app.services.market_rs_snapshot_service import (
    MarketRsSnapshotIncompatible,
    MarketRsSnapshotService,
)

AS_OF = date(2026, 4, 10)


def _complete_inputs():
    return MarketRsInputs(
        market="US",
        as_of_date=AS_OF,
        benchmark_symbol="SPY",
        benchmark_as_of_date=AS_OF,
        universe_hash="u" * 64,
        expected_symbols=("AAA", "BBB", "CCC"),
        excess_returns_by_symbol={
            "AAA": {
                "1d": 0.3,
                "1w": 0.3,
                "1m": 0.3,
                "3m": 0.3,
                "6m": 0.3,
                "9m": 0.3,
                "12m": 0.3,
            },
            "BBB": {
                "1d": 0.2,
                "1w": 0.2,
                "1m": 0.2,
                "3m": 0.2,
                "6m": 0.2,
                "9m": 0.2,
                "12m": 0.2,
            },
            "CCC": {
                "1d": 0.1,
                "1w": 0.1,
                "1m": 0.1,
                "3m": 0.1,
                "6m": 0.1,
                "9m": 0.1,
                "12m": 0.1,
            },
        },
        exclusions={},
        current_price_coverage=1.0,
    )


class _FakeInputLoader:
    def __init__(self, inputs=None, error=None):
        self.inputs = inputs
        self.error = error
        self.calls = []

    def load(self, _db, *, market, as_of_date):
        self.calls.append((market, as_of_date))
        if self.error:
            raise self.error
        return self.inputs


def test_snapshot_service_publishes_all_rows_and_run_atomically(db_session):
    service = MarketRsSnapshotService(
        input_loader=_FakeInputLoader(_complete_inputs()),
        repository=MarketRsRunRepository(),
    )

    run = service.calculate(db_session, market="US", as_of_date=AS_OF)

    assert run.status == "completed"
    assert run.eligible_symbol_count == 3
    assert len(run.rows) == 3
    assert run.diagnostics_json["price_basis"] == BALANCED_RS_PRICE_BASIS
    assert all(1 <= row.overall_rs <= 99 for row in run.rows)
    assert (
        db_session.query(MarketRsRun).filter(MarketRsRun.status == "completed").count()
        == 1
    )


def test_snapshot_input_failure_keeps_previous_completed_date_readable(db_session):
    repository = MarketRsRunRepository()
    previous_service = MarketRsSnapshotService(
        input_loader=_FakeInputLoader(_complete_inputs()),
        repository=repository,
    )
    previous = previous_service.calculate(
        db_session, market="US", as_of_date=date(2026, 4, 9)
    )
    error = MarketRsInputUnavailable(
        "benchmark missing",
        reason_code="benchmark_anchor_missing",
        diagnostics={"missing_offsets": [252]},
        benchmark_symbol="SPY",
        universe_hash="v" * 64,
        expected_symbol_count=3,
    )
    service = MarketRsSnapshotService(
        input_loader=_FakeInputLoader(error=error),
        repository=repository,
    )

    with pytest.raises(MarketRsInputUnavailable):
        service.calculate(db_session, market="US", as_of_date=AS_OF)

    latest = repository.get_latest_completed(
        db_session,
        market="US",
        formula_version=BALANCED_RS_FORMULA_VERSION,
    )
    assert latest.id == previous.id
    failed = db_session.query(MarketRsRun).filter(MarketRsRun.as_of_date == AS_OF).one()
    assert failed.status == "failed"
    assert failed.diagnostics_json["reason_code"] == "benchmark_anchor_missing"


def test_snapshot_service_rejects_other_formulas_without_loading_or_writing(db_session):
    loader = _FakeInputLoader(_complete_inputs())
    service = MarketRsSnapshotService(
        input_loader=loader,
        repository=MarketRsRunRepository(),
    )

    with pytest.raises(ValueError, match="legacy-linear-v1"):
        service.calculate(
            db_session,
            market="US",
            as_of_date=AS_OF,
            formula_version="legacy-linear-v1",
        )

    assert loader.calls == []
    assert db_session.query(MarketRsRun).count() == 0


def test_explicit_rebuild_replaces_incompatible_completed_run(db_session):
    old = MarketRsRun(
        market="US",
        as_of_date=AS_OF,
        formula_version=BALANCED_RS_FORMULA_VERSION,
        status="completed",
        benchmark_symbol="SPY",
        benchmark_as_of_date=AS_OF,
        universe_hash="old",
        expected_symbol_count=0,
        eligible_symbol_count=0,
        excluded_symbol_count=0,
        diagnostics_json={},
    )
    db_session.add(old)
    db_session.commit()
    old_id = old.id
    service = MarketRsSnapshotService(
        input_loader=_FakeInputLoader(_complete_inputs()),
        repository=MarketRsRunRepository(),
    )

    with pytest.raises(MarketRsSnapshotIncompatible):
        service.calculate(db_session, market="US", as_of_date=AS_OF)

    rebuilt = service.calculate(
        db_session,
        market="US",
        as_of_date=AS_OF,
        rebuild_incompatible=True,
    )
    assert rebuilt.id != old_id
    assert rebuilt.diagnostics_json["price_basis"] == BALANCED_RS_PRICE_BASIS


def test_completed_run_without_current_snapshot_schema_is_incompatible(db_session):
    old = MarketRsRun(
        market="US",
        as_of_date=AS_OF,
        formula_version=BALANCED_RS_FORMULA_VERSION,
        status="completed",
        benchmark_symbol="SPY",
        benchmark_as_of_date=AS_OF,
        universe_hash="old",
        expected_symbol_count=0,
        eligible_symbol_count=0,
        excluded_symbol_count=0,
        diagnostics_json={"price_basis": BALANCED_RS_PRICE_BASIS},
    )
    db_session.add(old)
    db_session.commit()
    old_id = old.id
    service = MarketRsSnapshotService(
        input_loader=_FakeInputLoader(_complete_inputs()),
        repository=MarketRsRunRepository(),
    )

    with pytest.raises(MarketRsSnapshotIncompatible):
        service.calculate(db_session, market="US", as_of_date=AS_OF)

    rebuilt = service.calculate(
        db_session,
        market="US",
        as_of_date=AS_OF,
        rebuild_incompatible=True,
    )
    assert rebuilt.id != old_id
    assert (
        rebuilt.diagnostics_json["rs_snapshot_schema_version"]
        == BALANCED_RS_SNAPSHOT_SCHEMA_VERSION
    )


YNG = {"1d": 0.2, "1w": 0.2, "1m": 0.05, "3m": -0.2}


def _inputs_with_young(young):
    return MarketRsInputs(
        market="US",
        as_of_date=AS_OF,
        benchmark_symbol="SPY",
        benchmark_as_of_date=AS_OF,
        universe_hash="u" * 64,
        expected_symbols=("A", "B", "C", "GAP", "YNG"),
        excess_returns_by_symbol={
            symbol: {horizon: value for horizon in HORIZONS}
            for symbol, value in (("A", -0.1), ("B", 0.0), ("C", 0.1))
        },
        exclusions={
            "YNG": "missing_adjusted_126_session_anchor",
            "GAP": "missing_adjusted_63_session_anchor",
        },
        current_price_coverage=1.0,
        young_excess_returns_by_symbol=young,
    )


def _eligible_rows(run):
    columns = [name for name in StockRsSnapshot.__table__.columns.keys() if name != "run_id"]
    return [
        tuple(getattr(row, name) for name in columns)
        for row in sorted(run.rows, key=lambda row: row.symbol)
    ]


def test_young_stock_placement_leaves_eligible_rows_and_group_ranks_unchanged(
    db_session,
):
    repository = MarketRsRunRepository()
    db_session.add_all(
        IBDIndustryGroup(symbol=symbol, industry_group="Software", market="US", source="manual")
        for symbol in ("A", "B", "C", "YNG")
    )
    db_session.commit()
    earlier = AS_OF - timedelta(days=1)

    without = MarketRsSnapshotService(
        input_loader=_FakeInputLoader(_inputs_with_young({})),
        repository=repository,
    ).calculate(db_session, market="US", as_of_date=earlier)
    with_young = MarketRsSnapshotService(
        input_loader=_FakeInputLoader(_inputs_with_young({"YNG": YNG})),
        repository=repository,
    ).calculate(db_session, market="US", as_of_date=AS_OF)

    assert _eligible_rows(with_young) == _eligible_rows(without)
    assert [
        (row.symbol, row.overall_rs, row.rs_1d, row.rs_1m, row.rs_12m)
        for row in sorted(without.rows, key=lambda row: row.symbol)
    ] == [("A", 1, 1, 1, 1), ("B", 50, 50, 50, 50), ("C", 99, 99, 99, 99)]
    for run in (without, with_young):
        assert (run.eligible_symbol_count, run.excluded_symbol_count) == (3, 2)
        assert run.diagnostics_json["rs_snapshot_schema_version"] == 2
    assert (without.placed_symbol_count, without.placements) == (0, [])
    assert with_young.placed_symbol_count == 1
    assert [
        (row.symbol, row.rs_1d, row.rs_1w, row.rs_1m, row.rs_3m, row.rs_6m, row.rs_12m)
        for row in with_young.placements
    ] == [("YNG", 99, 99, 66, 1, None, None)]

    groups = CanonicalGroupRankingService(repository=repository)

    def ranking(as_of_date):
        rows = groups.calculate_and_stage(
            db_session,
            market="US",
            as_of_date=as_of_date,
            formula_version=BALANCED_RS_FORMULA_VERSION,
        )
        return [
            {key: value for key, value in row.items() if key not in ("date", "market_rs_run_id")}
            for row in rows
        ]

    assert ranking(AS_OF) == ranking(earlier)
    assert [(row["industry_group"], row["avg_rs_rating"], row["num_stocks"]) for row in ranking(AS_OF)] == [
        ("Software", 50.0, 3)
    ]
