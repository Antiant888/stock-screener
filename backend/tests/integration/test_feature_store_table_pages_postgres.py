"""PostgreSQL plans for feature-store table pages.

A table page sorts a whole run and keeps one page of it. Anything the SELECT
list computes below that ``Sort`` runs once per row of the run: converting each
row's ``details_json`` (~64 kB on a live US run) to ``jsonb`` there took a
10,050-row run from 13 ms to over 10 s. These tests read the plan PostgreSQL
actually chooses for the statements the repository emits.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import event

from app.database import engine
from app.domain.common.query import PageSpec, SortOrder, SortSpec
from app.domain.scanning.filter_expression_model import QuerySpec
from app.infra.db.models.feature_store import FeatureRun, StockFeatureDaily
from app.infra.db.repositories.feature_store_repo import SqlFeatureStoreRepository

pytestmark = pytest.mark.skipif(
    engine.dialect.name != "postgresql", reason="reads PostgreSQL query plans"
)

AS_OF = date(2026, 2, 17)


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


def test_table_page_sorts_the_run_before_reading_details(db_session):
    run = _seed_run(db_session)
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

    outputs = _sort_subtree_outputs(_plan(db_session, *_page_statement(captured)))
    assert not [output for output in outputs if "jsonb" in output], outputs

    assert [item.symbol for item in page.items] == [
        "S049", "S048", "S047", "S046", "S045", "S044", "S043", "S042", "S041", "S040",
    ]
    assert page.total == 60
    first = page.items[0].extended_fields
    assert first["se_setup_score"] == 49.0
    assert "se_explain" not in first and "se_candidates" not in first
