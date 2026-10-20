from datetime import date

import pytest
from sqlalchemy import event

from app.domain.relative_strength import (
    BALANCED_RS_FORMULA_VERSION,
    BALANCED_RS_PRICE_BASIS,
    BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
    GroupSnapshotIdentity,
    RsPublicationIdentity,
)
from app.infra.db.models.relative_strength import MarketRsRun
from app.models.industry import IBDGroupRank
from app.services.group_rank_snapshot_reader import (
    GroupRankSnapshotReader,
    GroupSnapshotIntegrityError,
)

AS_OF = date(2026, 4, 10)


def _run(db_session, *, run_id=41, as_of_date=AS_OF):
    row = MarketRsRun(
        id=run_id,
        market="US",
        as_of_date=as_of_date,
        formula_version=BALANCED_RS_FORMULA_VERSION,
        status="completed",
        benchmark_symbol="SPY",
        benchmark_as_of_date=as_of_date,
        universe_hash="reader-test",
        expected_symbol_count=3,
        eligible_symbol_count=3,
        excluded_symbol_count=0,
        diagnostics_json={
            "price_basis": BALANCED_RS_PRICE_BASIS,
            "rs_snapshot_schema_version": BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
        },
    )
    db_session.add(row)
    db_session.flush()
    return row


def _rank(
    db_session,
    *,
    formula,
    rank,
    run_id=None,
    group="Software",
    as_of_date=AS_OF,
):
    db_session.add(
        IBDGroupRank(
            market="US",
            industry_group=group,
            date=as_of_date,
            rank=rank,
            avg_rs_rating=88.0,
            num_stocks=3,
            num_stocks_rs_above_80=2,
            top_symbol="AAA",
            top_rs_rating=99.0,
            rs_formula_version=formula,
            market_rs_run_id=run_id,
        )
    )


def test_identity_normalizes_market_and_rejects_blank_formula():
    identity = GroupSnapshotIdentity(" hk ", AS_OF, BALANCED_RS_FORMULA_VERSION)
    assert identity.market == "HK"
    with pytest.raises(ValueError, match="formula_version"):
        GroupSnapshotIdentity("US", AS_OF, " ")


def test_load_exact_never_crosses_formula(db_session):
    run = _run(db_session)
    _rank(db_session, formula=BALANCED_RS_FORMULA_VERSION, rank=1, run_id=run.id)
    _rank(db_session, formula="legacy-linear-v1", rank=9, group="Legacy")
    db_session.commit()

    rows = GroupRankSnapshotReader().load_exact(
        db_session,
        identity=GroupSnapshotIdentity("US", AS_OF, BALANCED_RS_FORMULA_VERSION),
    )

    assert [row["industry_group"] for row in rows] == ["Software"]
    assert rows[0]["market_rs_run_id"] == run.id


def test_balanced_rows_must_share_the_exact_completed_run(db_session):
    first = _run(db_session, run_id=41)
    _run(db_session, run_id=42, as_of_date=date(2026, 4, 9))
    _rank(db_session, formula=BALANCED_RS_FORMULA_VERSION, rank=1, run_id=first.id)
    _rank(
        db_session,
        formula=BALANCED_RS_FORMULA_VERSION,
        rank=2,
        run_id=42,
        group="Hardware",
    )
    db_session.commit()

    with pytest.raises(GroupSnapshotIntegrityError, match="Market RS run"):
        GroupRankSnapshotReader().load_exact(
            db_session,
            identity=GroupSnapshotIdentity("US", AS_OF, BALANCED_RS_FORMULA_VERSION),
        )


def test_load_publication_rejects_a_different_market_rs_run(db_session):
    run = _run(db_session, run_id=41)
    _rank(db_session, formula=BALANCED_RS_FORMULA_VERSION, rank=1, run_id=run.id)
    db_session.commit()

    expected = RsPublicationIdentity(
        snapshot=GroupSnapshotIdentity(
            "US",
            AS_OF,
            BALANCED_RS_FORMULA_VERSION,
        ),
        market_rs_run_id=42,
        universe_size=3,
    )

    with pytest.raises(GroupSnapshotIntegrityError, match="expected Market RS run"):
        GroupRankSnapshotReader().load_publication(
            db_session,
            publication=expected,
        )


def test_available_dates_is_formula_scoped(db_session):
    _rank(db_session, formula="legacy-linear-v1", rank=1)
    db_session.commit()
    assert (
        GroupRankSnapshotReader().available_dates(
            db_session,
            market="US",
            formula_version=BALANCED_RS_FORMULA_VERSION,
            through_date=AS_OF,
        )
        == ()
    )


def test_load_window_uses_two_queries_independent_of_date_count(db_session):
    previous = date(2026, 4, 9)
    first = _run(db_session, run_id=41, as_of_date=previous)
    second = _run(db_session, run_id=42, as_of_date=AS_OF)
    _rank(
        db_session,
        formula=BALANCED_RS_FORMULA_VERSION,
        rank=1,
        run_id=first.id,
        as_of_date=previous,
    )
    _rank(
        db_session,
        formula=BALANCED_RS_FORMULA_VERSION,
        rank=1,
        run_id=second.id,
        as_of_date=AS_OF,
    )
    db_session.commit()
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        snapshots = GroupRankSnapshotReader().load_window(
            db_session,
            market="US",
            formula_version=BALANCED_RS_FORMULA_VERSION,
            dates=(previous, AS_OF),
            include_top_symbol_names=False,
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert list(snapshots) == [previous, AS_OF]
    assert [row["rank"] for row in snapshots[previous]] == [1]
    assert len(statements) == 2


def test_load_window_reports_invalid_dates_without_losing_valid_snapshots(db_session):
    previous = date(2026, 4, 9)
    valid_run = _run(db_session, run_id=41, as_of_date=previous)
    wrong_run = _run(db_session, run_id=42, as_of_date=date(2026, 4, 8))
    _rank(
        db_session,
        formula=BALANCED_RS_FORMULA_VERSION,
        rank=1,
        run_id=valid_run.id,
        as_of_date=previous,
    )
    _rank(
        db_session,
        formula=BALANCED_RS_FORMULA_VERSION,
        rank=1,
        run_id=wrong_run.id,
        as_of_date=AS_OF,
    )
    db_session.commit()

    result = GroupRankSnapshotReader().inspect_window(
        db_session,
        market="US",
        formula_version=BALANCED_RS_FORMULA_VERSION,
        dates=(previous, AS_OF),
        include_top_symbol_names=False,
    )

    assert list(result.snapshots) == [previous]
    assert list(result.errors) == [AS_OF]


def test_load_window_remains_strict_when_any_requested_date_is_invalid(db_session):
    from app.services.group_rank_snapshot_reader import (
        GroupSnapshotWindowIntegrityError,
    )

    wrong_run = _run(db_session, run_id=43, as_of_date=date(2026, 4, 8))
    _rank(
        db_session,
        formula=BALANCED_RS_FORMULA_VERSION,
        rank=1,
        run_id=wrong_run.id,
        as_of_date=AS_OF,
    )
    db_session.commit()

    with pytest.raises(GroupSnapshotWindowIntegrityError):
        GroupRankSnapshotReader().load_window(
            db_session,
            market="US",
            formula_version=BALANCED_RS_FORMULA_VERSION,
            dates=(AS_OF,),
            include_top_symbol_names=False,
        )
