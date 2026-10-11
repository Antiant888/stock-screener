"""PostgreSQL plans for feature-store table pages.

A table page sorts a whole run and keeps one page of it. Anything the SELECT
list computes below that ``Sort`` runs once per row of the run: converting each
row's ``details_json`` (~64 kB on a live US run) to ``jsonb`` there took a
10,050-row run from 13 ms to over 10 s. These tests read the plan PostgreSQL
actually chooses for the statements the repository emits.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, insert, null, update

from app.database import engine
from app.domain.common.query import (
    BooleanFilter,
    PageSpec,
    RangeFilter,
    SortOrder,
    SortSpec,
)
from app.domain.scanning.filter_expression_model import FilterExpression, QuerySpec
from app.infra.db.models.feature_store import FeatureRun, StockFeatureDaily
from app.infra.db.repositories.feature_store_repo import SqlFeatureStoreRepository
from app.infra.query.feature_store_query import _FIELD_BINDINGS

pytestmark = pytest.mark.skipif(
    engine.dialect.name != "postgresql", reason="reads PostgreSQL query plans"
)

AS_OF = date(2026, 2, 17)
TABLE_DETAILS_MIGRATION = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "20261011_0064_feature_table_details.py"
)


def _forget_table_details(db_session) -> None:
    """Make the run look like one written before 20261011_0064."""
    db_session.execute(update(StockFeatureDaily).values(table_details_json=null()))
    db_session.flush()


def _seed_run(db_session, *, rows: int = 60) -> FeatureRun:
    run = FeatureRun(as_of_date=AS_OF, run_type="daily_snapshot", status="published")
    db_session.add(run)
    db_session.flush()
    for i in range(rows):
        db_session.add(
            StockFeatureDaily(
                run_id=run.id,
                symbol=f"S{i:03d}",
                as_of_date=AS_OF,
                composite_score=float(i),
                overall_rating=3,
                passes_count=1,
                details_json={
                    "rating": "Watch",
                    "rs_rating": i,
                    "ibd_industry_group": "Software" if i % 2 else "Banks",
                    "setup_engine": {
                        "setup_score": float(i),
                        "explain": {"summary": "x" * 200},
                        "candidates": [{"pattern": "base", "score": i}],
                    },
                },
            )
        )
    db_session.flush()
    return run


def _capture_statements(run_query):
    captured: list[tuple[str, object]] = []

    def capture(_conn, _cursor, statement, parameters, *_args):
        captured.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = run_query()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    return result, captured


def _page_statement(captured):
    pages = [
        (statement, parameters)
        for statement, parameters in captured
        if "FROM stock_feature_daily" in statement and "ORDER BY" in statement
    ]
    assert len(pages) == 1, [statement for statement, _ in captured]
    return pages[0]


def _plan(db_session, statement, parameters) -> dict:
    [(plan_json,)] = db_session.connection().exec_driver_sql(
        "EXPLAIN (VERBOSE, FORMAT JSON) " + statement, parameters
    ).fetchall()
    return plan_json[0]["Plan"]


def _subtree(node: dict):
    yield node
    for child in node.get("Plans", ()):
        yield from _subtree(child)


def _sort_subtree_outputs(plan: dict) -> list[str]:
    sorts = [node for node in _subtree(plan) if node["Node Type"] == "Sort"]
    assert sorts, "expected the page to sort the run; the plan no longer does"
    return [output for sort in sorts for node in _subtree(sort) for output in node.get("Output", ())]


@pytest.mark.parametrize("run_state", ["table_details", "legacy"])
def test_table_page_sorts_the_run_before_reading_details(db_session, run_state):
    run = _seed_run(db_session)
    if run_state == "legacy":
        _forget_table_details(db_session)
    repo = SqlFeatureStoreRepository(db_session)

    page, captured = _capture_statements(
        lambda: repo.query_run_as_scan_results(
            run.id,
            QuerySpec(
                sort=SortSpec(field="composite_score", order=SortOrder.DESC),
                page=PageSpec(page=2, per_page=10),
            ),
            include_setup_payload=False,
        )
    )

    # A bare column only passes its TOAST pointer through the Sort; any
    # expression over it there is evaluated, and detoasted, per row of the run.
    outputs = _sort_subtree_outputs(_plan(db_session, *_page_statement(captured)))
    computed = [
        output
        for output in outputs
        if "details_json" in output
        and output
        not in (
            "stock_feature_daily.details_json",
            "stock_feature_daily.table_details_json",
        )
    ]
    assert computed == [], outputs

    assert [item.symbol for item in page.items] == [
        "S049", "S048", "S047", "S046", "S045", "S044", "S043", "S042", "S041", "S040",
    ]
    assert page.total == 60
    first = page.items[0].extended_fields
    assert first["se_setup_score"] == 49.0
    assert "se_explain" not in first and "se_candidates" not in first


# ── 20261011_0064: the indexes serve filters over the stored copy ─────────


def _load_table_details_migration():
    spec = importlib.util.spec_from_file_location(
        "_table_details_migration", TABLE_DETAILS_MIGRATION
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def table_details_migration(db_session):
    migration = _load_table_details_migration()
    migration.op = Operations(MigrationContext.configure(db_session.connection()))
    migration._create_indexes()
    yield migration
    migration._drop_indexes()


def _seed_indexable_run(db_session, numeric_fields, *, rows: int = 200) -> FeatureRun:
    run = FeatureRun(as_of_date=AS_OF, run_type="daily_snapshot", status="published")
    db_session.add(run)
    db_session.flush()
    for i in range(rows):
        db_session.add(
            StockFeatureDaily(
                run_id=run.id,
                symbol=f"S{i:03d}",
                as_of_date=AS_OF,
                composite_score=float(i),
                details_json={
                    **{field: float(i) for field in numeric_fields},
                    "correction_survivor": i % 50 == 0,
                    "ibd_industry_group": f"G{i % 40:02d}",
                    "setup_engine": {"explain": {"summary": "x"}, "candidates": []},
                },
            )
        )
    db_session.flush()
    db_session.connection().exec_driver_sql("ANALYZE stock_feature_daily")
    return run


def _index_serves(db_session, captured, index_name: str, key: str) -> bool:
    """Whether a statement filtering on *key* searches *index_name* by it.

    Only the index condition counts: a plan can also reach the same index for
    its leading ``run_id`` alone (the readiness probe does), which says nothing
    about whether the indexed expression matches the filter's.
    """
    # A 200-row table rarely earns an index scan while a seq scan is allowed.
    db_session.connection().exec_driver_sql("SET LOCAL enable_seqscan = off")
    for statement, parameters in captured:
        if f"->> '{key}'" not in statement:
            continue
        for node in _subtree(_plan(db_session, statement, parameters)):
            if node.get("Index Name") == index_name and f"'{key}'" in node.get(
                "Index Cond", ""
            ):
                return True
    return False


def test_table_details_indexes_leave_out_rows_without_the_copy(db_session):
    """Every run published before 0064 has no copy; B-tree indexes keep NULL
    keys, so indexing those rows would write the whole history into each
    index while the migration holds the table's lock."""
    migration = _load_table_details_migration()
    run = FeatureRun(as_of_date=AS_OF, run_type="daily_snapshot", status="published")
    db_session.add(run)
    db_session.flush()
    # Core, not the ORM: a row written before 0064 has one version, without
    # the copy (an UPDATE here would leave a second, indexable version).
    db_session.execute(
        insert(StockFeatureDaily),
        [
            {
                "run_id": run.id,
                "symbol": f"L{i:03d}",
                "as_of_date": AS_OF,
                "details_json": {"rs_rating": i, "ibd_industry_group": "G01"},
            }
            for i in range(30)
        ],
    )

    migration.op = Operations(MigrationContext.configure(db_session.connection()))
    migration._create_indexes()
    try:
        # A non-concurrent build records how many entries it wrote.
        indexed = dict(
            db_session.connection().exec_driver_sql(
                "SELECT relname, reltuples FROM pg_class WHERE relname = ANY(%(names)s)",
                {"names": list(migration._INDEXES)},
            ).fetchall()
        )
    finally:
        migration._drop_indexes()

    assert set(indexed) == set(migration._INDEXES)
    assert {name: count for name, count in indexed.items() if count} == {}


def _public_name(details_key: str) -> str:
    [name, *_aliases] = sorted(
        name
        for name, binding in _FIELD_BINDINGS.items()
        if binding.json_path == (details_key,)
    )
    return name


def test_table_details_indexes_serve_the_filters_the_repository_emits(
    db_session, table_details_migration
):
    migration = table_details_migration
    run = _seed_indexable_run(db_session, migration._NUMERIC_FIELDS)
    repo = SqlFeatureStoreRepository(db_session)

    def symbols_matching(condition):
        return lambda: repo.query_run_symbols(
            run.id,
            FilterExpression(required_conditions=(condition,)),
            SortSpec(field="composite_score", order=SortOrder.DESC),
            PageSpec(page=1, per_page=50),
        )

    # index name -> (indexed details key, a repository call filtering on it)
    queries = {
        f"ix_sfd_td_{key}": (
            key,
            symbols_matching(RangeFilter(_public_name(key), min_value=195)),
        )
        for key in migration._NUMERIC_FIELDS
    }
    queries["ix_sfd_td_correction_survivor"] = (
        "correction_survivor",
        symbols_matching(BooleanFilter("correction_survivor", True)),
    )
    queries["ix_sfd_td_ibd_industry_group"] = (
        "ibd_industry_group",
        lambda: repo.get_peers_by_industry_for_run(run.id, "G07"),
    )
    assert set(queries) == set(migration._INDEXES), "every index needs a query here"

    unused = []
    for index_name, (key, run_query) in queries.items():
        _result, captured = _capture_statements(run_query)
        if not _index_serves(db_session, captured, index_name, key):
            unused.append(index_name)
    assert unused == [], f"PostgreSQL cannot use {unused} for the repository's SQL"
