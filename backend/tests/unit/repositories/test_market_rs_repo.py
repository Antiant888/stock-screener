from datetime import date, timedelta

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import sessionmaker

from app.domain.relative_strength import (
    BALANCED_RS_FORMULA_VERSION,
    StockRsPlacement,
    StockRsScore,
)
from app.infra.db.models.relative_strength import (
    MarketRsRun,
    StockRsPlacementSnapshot,
    StockRsSnapshot,
)
from app.infra.db.repositories.market_rs_repo import MarketRsRunRepository

AS_OF = date(2026, 4, 10)


def _run_kwargs(**overrides):
    values = {
        "market": "US",
        "as_of_date": AS_OF,
        "formula_version": BALANCED_RS_FORMULA_VERSION,
        "benchmark_symbol": "SPY",
        "benchmark_as_of_date": AS_OF,
        "universe_hash": "a" * 64,
        "expected_symbol_count": 2,
    }
    values.update(overrides)
    return values


def _score(symbol, value):
    return StockRsScore(
        symbol=symbol,
        overall_rs=value,
        rs_1d=value,
        rs_1w=value,
        rs_1m=value,
        rs_3m=value,
        rs_6m=value,
        rs_9m=value,
        rs_12m=value,
        weighted_composite=float(value),
        excess_return_1d=0.1,
        excess_return_1w=0.1,
        excess_return_1m=0.1,
        excess_return_3m=0.1,
        excess_return_6m=0.1,
        excess_return_9m=0.1,
        excess_return_12m=0.1,
    )


def test_completed_run_is_invisible_until_rows_and_status_commit(db_session):
    repo = MarketRsRunRepository()
    repo.start_or_restart(db_session, **_run_kwargs())
    db_session.commit()

    assert (
        repo.get_completed_exact(
            db_session,
            market="US",
            as_of_date=AS_OF,
            formula_version=BALANCED_RS_FORMULA_VERSION,
        )
        is None
    )


def test_completed_run_can_skip_snapshot_row_hydration(db_session):
    repo = MarketRsRunRepository()
    run = repo.start_or_restart(db_session, **_run_kwargs())
    repo.replace_rows(
        db_session,
        run,
        {"AAA": _score("AAA", 70), "BBB": _score("BBB", 80)},
    )
    repo.mark_completed(run, excluded_symbol_count=0, diagnostics={})
    db_session.commit()
    db_session.expunge_all()

    metadata = repo.get_completed_exact(
        db_session,
        market="US",
        as_of_date=AS_OF,
        formula_version=BALANCED_RS_FORMULA_VERSION,
        load_rows=False,
    )

    assert metadata is not None
    assert metadata.eligible_symbol_count == 2
    assert "rows" in inspect(metadata).unloaded

    db_session.expunge_all()
    hydrated = repo.get_completed_exact(
        db_session,
        market="US",
        as_of_date=AS_OF,
        formula_version=BALANCED_RS_FORMULA_VERSION,
        load_rows=True,
    )

    assert hydrated is not None
    assert "rows" not in inspect(hydrated).unloaded


def test_iter_stock_rows_for_run_returns_scalar_snapshots(db_session):
    repo = MarketRsRunRepository()
    run = repo.start_or_restart(db_session, **_run_kwargs())
    repo.replace_rows(
        db_session,
        run,
        {"AAA": _score("AAA", 70), "BBB": _score("BBB", 80)},
    )
    repo.mark_completed(run, excluded_symbol_count=0, diagnostics={})
    run_id = run.id
    db_session.commit()
    db_session.expunge_all()

    rows = list(repo.iter_stock_rows_for_run(db_session, run_id=run_id, batch_size=1))

    assert [row.symbol for row in rows] == ["AAA", "BBB"]
    assert rows[0].overall_rs == 70
    assert rows[0].rs_1d == 70
    assert rows[0].rs_1w == 70
    assert not isinstance(rows[0], StockRsSnapshot)


def test_list_completed_runs_filters_window_formula_and_status(db_session):
    repo = MarketRsRunRepository()
    db_session.add_all(
        [
            MarketRsRun(
                **_run_kwargs(as_of_date=AS_OF),
                status="completed",
                eligible_symbol_count=0,
                excluded_symbol_count=2,
                diagnostics_json={},
            ),
            MarketRsRun(
                **_run_kwargs(as_of_date=AS_OF - timedelta(days=1)),
                status="completed",
                eligible_symbol_count=0,
                excluded_symbol_count=2,
                diagnostics_json={},
            ),
            MarketRsRun(
                **_run_kwargs(as_of_date=AS_OF - timedelta(days=5)),
                status="completed",
                eligible_symbol_count=0,
                excluded_symbol_count=2,
                diagnostics_json={},
            ),
            MarketRsRun(
                **_run_kwargs(
                    as_of_date=AS_OF,
                    formula_version="legacy-linear-v1",
                ),
                status="completed",
                eligible_symbol_count=0,
                excluded_symbol_count=2,
                diagnostics_json={},
            ),
            MarketRsRun(
                **_run_kwargs(as_of_date=AS_OF - timedelta(days=2)),
                status="failed",
                eligible_symbol_count=0,
                excluded_symbol_count=2,
                diagnostics_json={},
            ),
        ]
    )
    db_session.commit()

    runs = repo.list_completed_runs(
        db_session,
        market="us",
        formula_version=BALANCED_RS_FORMULA_VERSION,
        start_date=AS_OF - timedelta(days=3),
        through_date=AS_OF,
    )

    assert [run.as_of_date for run in runs] == [
        AS_OF,
        AS_OF - timedelta(days=1),
    ]


def test_failed_restart_clears_partial_rows_but_completed_run_is_idempotent(
    db_session,
):
    repo = MarketRsRunRepository()
    run = MarketRsRun(
        **_run_kwargs(),
        status="failed",
        eligible_symbol_count=1,
        excluded_symbol_count=1,
        diagnostics_json={"error": "old"},
    )
    run.rows.append(
        StockRsSnapshot(
            symbol="PARTIAL",
            overall_rs=50,
            rs_1m=50,
            rs_3m=50,
            rs_6m=50,
            rs_9m=50,
            rs_12m=50,
            weighted_composite=50.0,
            excess_return_1m=0.0,
            excess_return_3m=0.0,
            excess_return_6m=0.0,
            excess_return_9m=0.0,
            excess_return_12m=0.0,
        )
    )
    db_session.add(run)
    db_session.commit()

    restarted = repo.start_or_restart(db_session, **_run_kwargs())

    assert restarted.id == run.id
    assert restarted.status == "running"
    assert restarted.rows == []

    repo.replace_rows(
        db_session,
        restarted,
        {"AAA": _score("AAA", 1), "BBB": _score("BBB", 99)},
    )
    repo.mark_completed(restarted, excluded_symbol_count=0, diagnostics={})
    db_session.commit()

    completed = repo.start_or_restart(db_session, **_run_kwargs(universe_hash="b" * 64))
    assert completed.id == restarted.id
    assert completed.status == "completed"
    assert completed.universe_hash == "a" * 64
    assert len(completed.rows) == 2


def test_two_sessions_share_one_completed_winner_and_never_read_partial_rows(
    db_session,
):
    repo = MarketRsRunRepository()
    Session = sessionmaker(bind=db_session.get_bind())
    first = Session()
    second = Session()
    try:
        run = repo.start_or_restart(first, **_run_kwargs())
        repo.replace_rows(
            first,
            run,
            {"AAA": _score("AAA", 1), "BBB": _score("BBB", 99)},
        )
        assert (
            repo.get_completed_exact(
                second,
                market="US",
                as_of_date=AS_OF,
                formula_version=BALANCED_RS_FORMULA_VERSION,
            )
            is None
        )
        repo.mark_completed(run, excluded_symbol_count=0, diagnostics={})
        first.commit()

        winner = repo.start_or_restart(second, **_run_kwargs(universe_hash="z" * 64))
        second.commit()

        assert winner.id == run.id
        assert winner.status == "completed"
        assert winner.universe_hash == "a" * 64
        assert second.query(MarketRsRun).count() == 1
        assert len(winner.rows) == 2
    finally:
        first.close()
        second.close()


# --- young-stock placements -------------------------------------------------


def _placement(symbol, **ratings):
    return StockRsPlacement(symbol, ratings, {horizon: 0.05 for horizon in ratings})


YNG = _placement("YNG", **{"1d": 99, "1w": 99, "1m": 66, "3m": 1})


def test_replace_rows_writes_placements_once_and_restart_resets_them(db_session):
    repo = MarketRsRunRepository()
    run = repo.start_or_restart(db_session, **_run_kwargs(expected_symbol_count=3))
    scores = {"AAA": _score("AAA", 70), "BBB": _score("BBB", 80)}

    repo.replace_rows(db_session, run, scores, placements={"YNG": YNG})
    repo.replace_rows(db_session, run, scores, placements={"YNG": YNG})

    rows = db_session.query(StockRsPlacementSnapshot).filter_by(run_id=run.id).all()
    assert [
        (r.symbol, r.rs_1d, r.rs_1w, r.rs_1m, r.rs_3m, r.rs_6m, r.rs_9m, r.rs_12m)
        for r in rows
    ] == [("YNG", 99, 99, 66, 1, None, None, None)]
    assert (rows[0].excess_return_1d, rows[0].excess_return_6m) == (0.05, None)
    assert run.placed_symbol_count == 1

    restarted = repo.start_or_restart(db_session, **_run_kwargs(expected_symbol_count=3))

    assert restarted.id == run.id
    assert restarted.placements == []
    assert restarted.placed_symbol_count is None

    repo.replace_rows(db_session, restarted, scores)

    assert restarted.placed_symbol_count is None
    assert db_session.query(StockRsPlacementSnapshot).count() == 0


def test_rebuilding_a_completed_run_drops_its_placements(db_session):
    repo = MarketRsRunRepository()
    run = repo.start_or_restart(db_session, **_run_kwargs(expected_symbol_count=3))
    repo.replace_rows(
        db_session,
        run,
        {"AAA": _score("AAA", 1), "BBB": _score("BBB", 99)},
        placements={"YNG": YNG},
    )
    repo.mark_completed(run, excluded_symbol_count=1, diagnostics={})
    db_session.commit()
    old_id = run.id
    assert run.placed_symbol_count == 1

    rebuilt = repo.start_or_restart(
        db_session, **_run_kwargs(expected_symbol_count=3), rebuild_completed=True
    )
    db_session.commit()

    assert rebuilt.id != old_id
    assert rebuilt.placed_symbol_count is None
    assert db_session.query(StockRsPlacementSnapshot).count() == 0


def test_mark_completed_rejects_a_symbol_that_is_both_eligible_and_placed(db_session):
    repo = MarketRsRunRepository()
    run = repo.start_or_restart(db_session, **_run_kwargs(expected_symbol_count=3))
    repo.replace_rows(
        db_session,
        run,
        {"AAA": _score("AAA", 70), "BBB": _score("BBB", 80)},
        placements={"AAA": _placement("AAA", **{"1d": 50})},
    )

    with pytest.raises(ValueError, match="AAA is both eligible and placed"):
        repo.mark_completed(run, excluded_symbol_count=1, diagnostics={})

    assert run.status == "running"


def test_mark_completed_rejects_a_placed_count_that_does_not_match_rows(db_session):
    repo = MarketRsRunRepository()
    run = repo.start_or_restart(db_session, **_run_kwargs(expected_symbol_count=3))
    repo.replace_rows(
        db_session,
        run,
        {"AAA": _score("AAA", 70), "BBB": _score("BBB", 80)},
        placements={"YNG": YNG},
    )
    run.placed_symbol_count = 2

    with pytest.raises(
        ValueError, match="placed symbol count does not match persisted placements"
    ):
        repo.mark_completed(run, excluded_symbol_count=1, diagnostics={})

    assert run.status == "running"


def test_mark_completed_rejects_more_placed_than_excluded_symbols(db_session):
    repo = MarketRsRunRepository()
    run = repo.start_or_restart(db_session, **_run_kwargs(expected_symbol_count=3))
    repo.replace_rows(
        db_session,
        run,
        {"AAA": _score("AAA", 70), "BBB": _score("BBB", 80)},
        placements={"YNG": YNG, "ZZZ": _placement("ZZZ", **{"1d": 40})},
    )

    with pytest.raises(ValueError, match="placed symbols exceed excluded symbols"):
        repo.mark_completed(run, excluded_symbol_count=1, diagnostics={})

    assert run.status == "running"


def test_mark_completed_rejects_a_placement_row_that_is_not_a_leading_prefix(db_session):
    repo = MarketRsRunRepository()
    run = repo.start_or_restart(db_session, **_run_kwargs(expected_symbol_count=4))
    repo.replace_rows(
        db_session,
        run,
        {"AAA": _score("AAA", 70), "BBB": _score("BBB", 80)},
        placements={"YNG": YNG},
    )
    run.placements.append(
        StockRsPlacementSnapshot(
            symbol="BAD", rs_1d=50, rs_1m=50, excess_return_1d=0.0, excess_return_1m=0.0
        )
    )
    run.placed_symbol_count = 2

    with pytest.raises(ValueError, match="BAD placement horizons"):
        repo.mark_completed(run, excluded_symbol_count=2, diagnostics={})

    assert run.status == "running"
    db_session.rollback()


def test_iter_placements_for_run_yields_domain_placements_in_symbol_order(db_session):
    repo = MarketRsRunRepository()
    run = repo.start_or_restart(db_session, **_run_kwargs(expected_symbol_count=4))
    repo.replace_rows(
        db_session,
        run,
        {"AAA": _score("AAA", 70), "BBB": _score("BBB", 80)},
        placements={"YNG": YNG, "ABC": _placement("ABC", **{"1d": 40})},
    )
    repo.mark_completed(run, excluded_symbol_count=2, diagnostics={})
    run_id = run.id
    db_session.commit()
    db_session.expunge_all()

    placements = list(repo.iter_placements_for_run(db_session, run_id=run_id, batch_size=1))

    assert placements == [_placement("ABC", **{"1d": 40}), YNG]
    assert placements[1].scanner_fields() == {"rs_rating_1m": 66, "rs_rating_3m": 1}
