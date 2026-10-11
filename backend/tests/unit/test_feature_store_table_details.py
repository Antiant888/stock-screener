"""Table-level details: the copy of ``details_json`` filters, sorts and pages read.

``details_json`` carries the setup engine's ``explain``/``candidates`` payload
(~64 kB per row on a live US run), and every JSON filter or sort over a run used
to decompress and parse it per row. ``table_details_json`` holds the same
document without that payload. Rows written before it existed have none, so a
run falls back to ``details_json`` unless every row carries it -- both kinds of
run must answer every query identically.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, event, null, select, update
from sqlalchemy.orm import Session

from app.database import Base
from app.domain.common.query import (
    CategoricalFilter,
    PageSpec,
    RangeFilter,
    SortOrder,
    SortSpec,
)
from app.domain.feature_store.models import FeatureRowWrite
from app.domain.scanning.filter_expression_model import FilterExpression, QuerySpec
from app.infra.db.models.feature_store import FeatureRun, StockFeatureDaily
from app.infra.db.repositories.feature_store_repo import SqlFeatureStoreRepository

AS_OF = date(2026, 2, 17)
SETUP_PAYLOAD = {
    "explain": {"summary": "full setup evidence"},
    "candidates": [{"pattern": "vcp", "score": 0.9}],
}

# symbol -> (composite_score, rs_rating, adr_percent, ibd_industry_group, gics_sector)
ROWS = {
    "AAPL": (92.5, 95.0, 2.5, "Comp-Peripherals", "Technology"),
    "MSFT": (78.0, 80.0, 1.8, "Comp-Software", "Technology"),
    "GOOGL": (55.0, 60.0, 3.1, "Comp-Software", "Communication Services"),
    "XOM": (40.0, 85.0, 4.2, "Oil-Integrated", "Energy"),
}

TABLE_DETAILS_SQL = "stock_feature_daily.table_details_json ->>"
DETAILS_SQL = "stock_feature_daily.details_json ->>"


def _details(symbol: str) -> dict:
    _score, rs, adr, group, sector = ROWS[symbol]
    return {
        "rating": "Buy",
        "rs_rating": rs,
        "adr_percent": adr,
        "ibd_industry_group": group,
        "gics_sector": sector,
        "setup_engine": {"setup_score": rs / 10, **SETUP_PAYLOAD},
    }


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as sess:
        yield sess


@pytest.fixture(params=["table_details", "legacy"])
def run_id(request, session):
    """A seeded run; ``legacy`` clears one row's table details like a pre-0064 row."""
    session.add(FeatureRun(id=1, as_of_date=AS_OF, run_type="daily_snapshot", status="published"))
    for symbol, (score, *_rest) in ROWS.items():
        session.add(
            StockFeatureDaily(
                run_id=1,
                symbol=symbol,
                as_of_date=AS_OF,
                composite_score=score,
                overall_rating=4,
                passes_count=1,
                details_json=_details(symbol),
            )
        )
    session.flush()
    if request.param == "legacy":
        # SQL NULL, as ADD COLUMN leaves existing rows; a Python None here
        # would store the JSON literal null instead.
        session.execute(
            update(StockFeatureDaily)
            .where(StockFeatureDaily.symbol == "XOM")
            .values(table_details_json=null())
        )
    session.commit()
    session.info["run_state"] = request.param
    return 1


@pytest.fixture
def sql(session):
    """Every statement the session's engine runs, in order."""
    statements: list[str] = []

    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    yield statements
    event.remove(engine, "before_cursor_execute", capture)


def _assert_reads_expected_column(session, statements: list[str]) -> None:
    text = "\n".join(statements)
    if session.info["run_state"] == "table_details":
        assert TABLE_DETAILS_SQL in text and DETAILS_SQL not in text, text
    else:
        assert DETAILS_SQL in text and TABLE_DETAILS_SQL not in text, text


# ── The stored copy ─────────────────────────────────────────────────────


def test_inserting_a_row_stores_its_details_without_the_setup_payload(session):
    session.add(
        StockFeatureDaily(
            run_id=1,
            symbol="AAPL",
            as_of_date=AS_OF,
            details_json={
                "rs_rating": 91,
                "setup_engine": {"setup_score": 7.5, **SETUP_PAYLOAD},
            },
        )
    )
    session.flush()

    stored = session.execute(select(StockFeatureDaily.table_details_json)).scalar_one()
    assert stored == {"rs_rating": 91, "setup_engine": {"setup_score": 7.5}}


def test_replacing_details_refreshes_the_stored_copy(session):
    """Group enrichment rewrites ``details_json`` on published rows."""
    row = StockFeatureDaily(
        run_id=1,
        symbol="AAPL",
        as_of_date=AS_OF,
        details_json={"ibd_industry_group": None, "setup_engine": dict(SETUP_PAYLOAD)},
    )
    session.add(row)
    session.flush()

    row.details_json = {**row.details_json, "ibd_industry_group": "Comp-Peripherals"}
    session.flush()

    stored = session.execute(select(StockFeatureDaily.table_details_json)).scalar_one()
    assert stored == {"ibd_industry_group": "Comp-Peripherals", "setup_engine": {}}


def test_upserting_a_snapshot_stores_and_refreshes_the_copy(session):
    """Publishing writes through the Core upsert, not the ORM."""
    session.add(FeatureRun(id=1, as_of_date=AS_OF, run_type="daily_snapshot", status="running"))
    session.flush()
    repo = SqlFeatureStoreRepository(session)

    def write(details):
        repo.upsert_snapshot_rows(
            1,
            [
                FeatureRowWrite(
                    symbol="AAPL",
                    as_of_date=AS_OF,
                    composite_score=90.0,
                    overall_rating=5,
                    passes_count=3,
                    details=details,
                )
            ],
        )

    write({"rs_rating": 91, "setup_engine": {"setup_score": 7.5, **SETUP_PAYLOAD}})
    write({"rs_rating": 42, "setup_engine": {"setup_score": 1.0, **SETUP_PAYLOAD}})

    stored = session.execute(select(StockFeatureDaily.table_details_json)).scalar_one()
    assert stored == {"rs_rating": 42, "setup_engine": {"setup_score": 1.0}}


# ── Reads: identical answers, cheaper column when every row has it ───────


def test_filtered_sorted_page(session, run_id, sql):
    page = SqlFeatureStoreRepository(session).query_run_as_scan_results(
        run_id,
        QuerySpec(
            expression=FilterExpression(
                required_conditions=(RangeFilter("rs_rating", min_value=70),)
            ),
            sort=SortSpec(field="adr_percent", order=SortOrder.DESC),
            page=PageSpec(page=1, per_page=2),
        ),
        include_setup_payload=False,
    )

    assert [item.symbol for item in page.items] == ["XOM", "AAPL"]
    assert page.total == 3
    assert page.items[0].extended_fields["se_setup_score"] == 8.5
    assert "se_candidates" not in page.items[0].extended_fields
    _assert_reads_expected_column(session, sql)


def test_full_detail_page_keeps_the_setup_payload(session, run_id, sql):
    page = SqlFeatureStoreRepository(session).query_run_as_scan_results(
        run_id,
        QuerySpec(
            expression=FilterExpression(
                required_conditions=(RangeFilter("rs_rating", min_value=90),)
            ),
            sort=SortSpec(field="composite_score", order=SortOrder.DESC),
        ),
        include_setup_payload=True,
    )

    [item] = page.items
    assert item.symbol == "AAPL"
    assert item.extended_fields["se_candidates"] == SETUP_PAYLOAD["candidates"]
    _assert_reads_expected_column(session, sql)


def test_navigation_symbols(session, run_id, sql):
    symbols, total = SqlFeatureStoreRepository(session).query_run_symbols(
        run_id,
        FilterExpression(
            required_conditions=(
                CategoricalFilter("ibd_industry_group", values=("Comp-Software",)),
            )
        ),
        SortSpec(field="rs_rating", order=SortOrder.ASC),
    )

    assert symbols == ("GOOGL", "MSFT")
    assert total == 2
    _assert_reads_expected_column(session, sql)


def test_export_rows(session, run_id, sql):
    items = SqlFeatureStoreRepository(session).query_all_as_scan_results(
        run_id,
        FilterExpression(required_conditions=(RangeFilter("adr_percent", min_value=3),)),
        SortSpec(field="rs_rating", order=SortOrder.DESC),
        include_setup_payload=False,
    )

    assert [item.symbol for item in items] == ["XOM", "GOOGL"]
    assert "se_explain" not in items[0].extended_fields
    _assert_reads_expected_column(session, sql)


def test_industry_peers(session, run_id, sql):
    peers = SqlFeatureStoreRepository(session).get_peers_by_industry_for_run(
        run_id, "Comp-Software"
    )

    assert [peer.symbol for peer in peers] == ["MSFT", "GOOGL"]
    _assert_reads_expected_column(session, sql)


def test_sector_peers(session, run_id, sql):
    peers = SqlFeatureStoreRepository(session).get_peers_by_sector_for_run(
        run_id, "Technology"
    )

    assert [peer.symbol for peer in peers] == ["AAPL", "MSFT"]
    _assert_reads_expected_column(session, sql)


def test_filter_options(session, run_id, sql):
    options = SqlFeatureStoreRepository(session).get_filter_options_for_run(run_id)

    assert options.ibd_industries == ("Comp-Peripherals", "Comp-Software", "Oil-Integrated")
    assert options.gics_sectors == ("Communication Services", "Energy", "Technology")
    _assert_reads_expected_column(session, sql)
