"""Integration tests for scan result enrichment during persistence."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.domain.relative_strength import BALANCED_RS_FORMULA_VERSION
from app.domain.scanning.filter_spec import (
    BooleanFilter,
    CategoricalFilter,
    FilterExpression,
    QuerySpec,
    SortOrder,
    SortSpec,
)
from app.domain.scanning.ports import ScanResultRsAudit
from app.infra.db.repositories.scan_result_repo import SqlScanResultRepository
from app.models.industry import IBDGroupRank, IBDIndustryGroup
from app.models.scan_result import Scan, ScanResult
from app.models.stock import StockFundamental, StockIndustry
from app.models.stock_universe import StockUniverse
from app.schemas.scanning import ScanResultItem
from app.services.market_taxonomy_service import MarketTaxonomyEntry

pytestmark = pytest.mark.integration


# Rows the database still holds from before the single-formula cutover.
STORED_LEGACY_FORMULA = "legacy-linear-v1"


@pytest.fixture
def session():
    """Create an isolated in-memory DB with full schema."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as sess:
        yield sess


def _base_raw_result() -> dict:
    """Return a minimal orchestrator-like result dict."""
    return {
        "composite_score": 82.5,
        "rating": "Buy",
        "current_price": 123.45,
        "screeners_run": ["minervini", "ipo"],
        "details": {"screeners": {}},
    }


def _opportunity_projection(
    *, survivor: bool, score: float, action_state: str
) -> dict:
    return {
        "correction_survivor": survivor,
        "resilience_score": score,
        "action_state": action_state,
        "opportunity_state": {
            "schema_version": 1,
            "policy_version": "correction-survivors-v1",
            "as_of_date": "2026-08-21",
            "market": "US",
            "mic": "XNAS",
            "benchmark_symbol": "SPY",
            "benchmark_as_of_date": "2026-08-21",
            "passed_checks": ["benchmark_leadership"],
            "failed_checks": [],
            "warnings": [],
            "score_pillars": {
                "benchmark_leadership": 20.0,
                "multi_horizon_rs": 17.0,
                "trend_integrity": 20.0,
                "structure_tightness": 17.0,
                "liquidity_freshness": 10.0,
            },
            "metrics": {"benchmark_relative_return_65d": 0.083},
            "data_availability": {"features": "available"},
            "action_reasons": [action_state],
        },
    }


def _seed_opportunity_scan_rows(session: Session) -> SqlScanResultRepository:
    session.add_all(
        [
            ScanResult(
                scan_id="scan-opportunity",
                symbol="ALPHA",
                composite_score=80.0,
                rating="Buy",
                details=_opportunity_projection(
                    survivor=True,
                    score=84.0,
                    action_state="setup_ready",
                ),
            ),
            ScanResult(
                scan_id="scan-opportunity",
                symbol="BETA",
                composite_score=90.0,
                rating="Watch",
                details=_opportunity_projection(
                    survivor=False,
                    score=84.0,
                    action_state="watch",
                ),
            ),
            ScanResult(
                scan_id="scan-opportunity",
                symbol="LEGACY",
                composite_score=99.0,
                rating="Watch",
                details={},
            ),
        ]
    )
    session.commit()
    return SqlScanResultRepository(session)


def test_legacy_repository_maps_opportunity_contract_and_preserves_old_row_nulls(
    session: Session,
):
    repo = _seed_opportunity_scan_rows(session)

    page = repo.query("scan-opportunity", QuerySpec())
    responses = {
        item.symbol: ScanResultItem.from_domain(item)
        for item in page.items
    }

    assert responses["ALPHA"].correction_survivor is True
    assert responses["ALPHA"].resilience_score == 84.0
    assert responses["ALPHA"].action_state == "setup_ready"
    assert responses["ALPHA"].opportunity_state is not None
    assert (
        responses["ALPHA"].opportunity_state.policy_version
        == "correction-survivors-v1"
    )
    assert responses["LEGACY"].correction_survivor is None
    assert responses["LEGACY"].resilience_score is None
    assert responses["LEGACY"].action_state is None
    assert responses["LEGACY"].opportunity_state is None


def test_legacy_repository_filters_correction_survivors(session: Session):
    repo = _seed_opportunity_scan_rows(session)
    expression = FilterExpression(
        required_conditions=(BooleanFilter("correction_survivor", True),)
    )

    page = repo.query("scan-opportunity", QuerySpec(expression=expression))

    assert [item.symbol for item in page.items] == ["ALPHA"]


def test_legacy_repository_filters_action_state(session: Session):
    repo = _seed_opportunity_scan_rows(session)
    expression = FilterExpression(
        required_conditions=(
            CategoricalFilter("action_state", ("setup_ready",)),
        )
    )

    page = repo.query("scan-opportunity", QuerySpec(expression=expression))

    assert [item.symbol for item in page.items] == ["ALPHA"]


def test_legacy_repository_sorts_resilience_desc_nulls_last_then_symbol(
    session: Session,
):
    repo = _seed_opportunity_scan_rows(session)

    page = repo.query(
        "scan-opportunity",
        QuerySpec(
            sort=SortSpec(field="resilience_score", order=SortOrder.DESC),
        ),
    )

    assert [item.symbol for item in page.items] == ["ALPHA", "BETA", "LEGACY"]


def test_persist_orchestrator_results_enriches_reference_fields(session: Session):
    session.add(
        StockFundamental(
            symbol="AAPL",
            eps_rating=88,
            ipo_date=date(2010, 6, 29),
            sector=None,  # ensure stock_industry fallback is used
            industry=None,
        )
    )
    session.add(
        StockIndustry(
            symbol="AAPL",
            sector="Technology",
            industry="Consumer Electronics",
        )
    )
    session.add(
        IBDIndustryGroup(
            symbol="AAPL",
            industry_group="Computer-Hardware/Peripherals",
        )
    )
    session.add_all(
        [
            IBDGroupRank(
                industry_group="Computer-Hardware/Peripherals",
                date=date(2026, 2, 18),
                rank=11,
                avg_rs_rating=75.0,
            ),
            IBDGroupRank(
                industry_group="Computer-Hardware/Peripherals",
                date=date(2026, 2, 19),
                rank=5,
                avg_rs_rating=80.0,
            ),
        ]
    )
    session.commit()

    repo = SqlScanResultRepository(session)
    repo.persist_orchestrator_results("scan-1", [("AAPL", _base_raw_result())])

    row = (
        session.query(ScanResult)
        .filter(ScanResult.scan_id == "scan-1", ScanResult.symbol == "AAPL")
        .one()
    )

    assert row.eps_rating == 88
    assert row.ipo_date == "2010-06-29"
    assert row.gics_sector == "Technology"
    assert row.gics_industry == "Consumer Electronics"
    assert row.ibd_industry_group == "Computer-Hardware/Peripherals"
    assert row.ibd_group_rank == 5
    assert row.details["ibd_group_rank_date"] == "2026-02-19"

    page = repo.query("scan-1", QuerySpec())
    assert page.items[0].extended_fields["ibd_group_rank_date"] == "2026-02-19"
    assert ScanResultItem.from_domain(page.items[0]).ibd_group_rank_date == "2026-02-19"


def test_scan_enrichment_uses_only_balanced_group_rows(session: Session):
    session.add(
        IBDIndustryGroup(
            symbol="NVDA",
            industry_group="Electronic-Semiconductor Fabless",
        )
    )
    session.add_all(
        [
            IBDGroupRank(
                market="US",
                industry_group="Electronic-Semiconductor Fabless",
                date=date(2026, 6, 18),
                rank=2,
                avg_rs_rating=88.0,
                rs_formula_version=BALANCED_RS_FORMULA_VERSION,
            ),
            IBDGroupRank(
                market="US",
                industry_group="Electronic-Semiconductor Fabless",
                date=date(2026, 6, 18),
                rank=91,
                avg_rs_rating=99.0,
                rs_formula_version=STORED_LEGACY_FORMULA,
            ),
        ]
    )
    session.commit()

    repo = SqlScanResultRepository(session)
    repo.persist_orchestrator_results("scan-active-rs", [("NVDA", _base_raw_result())])

    row = (
        session.query(ScanResult)
        .filter(
            ScanResult.scan_id == "scan-active-rs",
            ScanResult.symbol == "NVDA",
        )
        .one()
    )
    assert row.ibd_group_rank == 2
    assert row.details["ibd_group_rank_date"] == "2026-06-18"


def test_scan_enrichment_uses_pinned_row_rs_identity(session: Session):
    session.add(
        IBDIndustryGroup(
            symbol="NVDA",
            industry_group="Electronic-Semiconductor Fabless",
        )
    )
    session.add_all(
        [
            IBDGroupRank(
                market="US",
                industry_group="Electronic-Semiconductor Fabless",
                date=date(2026, 6, 18),
                rank=2,
                avg_rs_rating=88.0,
                rs_formula_version=BALANCED_RS_FORMULA_VERSION,
                market_rs_run_id=42,
            ),
            IBDGroupRank(
                market="US",
                industry_group="Electronic-Semiconductor Fabless",
                date=date(2026, 6, 19),
                rank=1,
                avg_rs_rating=90.0,
                rs_formula_version=BALANCED_RS_FORMULA_VERSION,
                market_rs_run_id=43,
            ),
            IBDGroupRank(
                market="US",
                industry_group="Electronic-Semiconductor Fabless",
                date=date(2026, 6, 19),
                rank=91,
                avg_rs_rating=50.0,
                rs_formula_version=STORED_LEGACY_FORMULA,
            ),
        ]
    )
    session.commit()
    raw = _base_raw_result()
    raw.update(
        rs_formula_version=BALANCED_RS_FORMULA_VERSION,
        market_rs_run_id=42,
    )

    repo = SqlScanResultRepository(session)
    repo.persist_orchestrator_results("scan-pinned-rs", [("NVDA", raw)])

    row = (
        session.query(ScanResult)
        .filter(
            ScanResult.scan_id == "scan-pinned-rs",
            ScanResult.symbol == "NVDA",
        )
        .one()
    )
    assert row.ibd_group_rank == 2
    assert row.details["ibd_group_rank_date"] == "2026-06-18"
    assert repo.list_rs_audits_by_scan_id("scan-pinned-rs") == (
        ScanResultRsAudit(
            symbol="NVDA",
            formula_version=BALANCED_RS_FORMULA_VERSION,
            run_id=42,
        ),
    )


def test_persist_orchestrator_results_uses_ipo_screener_date_fallback(session: Session):
    # No fundamentals row on purpose, so IPO date must come from ipo screener details.
    session.add(StockIndustry(symbol="MSFT", sector="Technology", industry="Software"))
    session.commit()

    raw = _base_raw_result()
    raw["details"] = {
        "screeners": {
            "ipo": {
                "details": {
                    "ipo_date": "1986-03-13",
                }
            }
        }
    }

    repo = SqlScanResultRepository(session)
    repo.persist_orchestrator_results("scan-2", [("MSFT", raw)])

    row = (
        session.query(ScanResult)
        .filter(ScanResult.scan_id == "scan-2", ScanResult.symbol == "MSFT")
        .one()
    )

    assert row.ipo_date == "1986-03-13"
    assert row.gics_sector == "Technology"
    assert row.gics_industry == "Software"

def test_persist_orchestrator_results_enriches_non_us_market_taxonomy(session: Session):
    session.add_all(
        [
            StockUniverse(
                symbol="0700.HK",
                market="HK",
                exchange="XHKG",
                currency="HKD",
                timezone="Asia/Hong_Kong",
            ),
            IBDGroupRank(
                market="HK",
                industry_group="Internet Services",
                date=date(2026, 6, 14),
                rank=4,
                avg_rs_rating=81.0,
                rs_formula_version=BALANCED_RS_FORMULA_VERSION,
            ),
        ]
    )
    session.commit()

    class _FakeTaxonomyService:
        def get(self, symbol, *, market=None, exchange=None):  # noqa: ARG002
            if symbol == "0700.HK" and market == "HK":
                return MarketTaxonomyEntry(
                    market="HK",
                    symbol="0700.HK",
                    industry_group="Internet Services",
                    themes=("AI Infrastructure", "Cloud"),
                )
            return None

    repo = SqlScanResultRepository(
        session,
        taxonomy_service=_FakeTaxonomyService(),
    )
    repo.persist_orchestrator_results("scan-hk-1", [("0700.HK", _base_raw_result())])

    row = (
        session.query(ScanResult)
        .filter(ScanResult.scan_id == "scan-hk-1", ScanResult.symbol == "0700.HK")
        .one()
    )

    assert row.ibd_industry_group == "Internet Services"
    assert row.ibd_group_rank == 4
    assert row.details["ibd_group_rank_date"] == "2026-06-14"
    assert row.details["market_themes"] == ["AI Infrastructure", "Cloud"]


def test_non_us_rank_enrichment_honors_explicit_ranking_date(session: Session):
    session.add_all(
        [
            StockUniverse(
                symbol="0700.HK",
                market="HK",
                exchange="XHKG",
                currency="HKD",
                timezone="Asia/Hong_Kong",
            ),
            IBDGroupRank(
                market="HK",
                industry_group="Internet Services",
                date=date(2026, 2, 19),
                rank=7,
                avg_rs_rating=74.0,
                rs_formula_version=BALANCED_RS_FORMULA_VERSION,
            ),
        ]
    )
    session.commit()

    class _FakeTaxonomyService:
        def get(self, symbol, *, market=None, exchange=None):  # noqa: ARG002
            if symbol == "0700.HK" and market == "HK":
                return MarketTaxonomyEntry(
                    market="HK",
                    symbol="0700.HK",
                    industry_group="Internet Services",
                )
            return None

    repo = SqlScanResultRepository(
        session,
        taxonomy_service=_FakeTaxonomyService(),
    )
    session.add(
        ScanResult(
            scan_id="scan-hk-historical",
            symbol="0700.HK",
            composite_score=80.0,
            rating="Buy",
            details={},
            ibd_industry_group=None,
            ibd_group_rank=None,
        )
    )
    session.commit()

    stats = repo.backfill_ibd_metadata_for_scan(
        "scan-hk-historical",
        ranking_date=date(2026, 2, 19),
    )

    row = (
        session.query(ScanResult)
        .filter(
            ScanResult.scan_id == "scan-hk-historical",
            ScanResult.symbol == "0700.HK",
        )
        .one()
    )

    assert stats["updated_rows"] == 1
    assert row.ibd_group_rank == 7
    assert row.details["ibd_group_rank_date"] == "2026-02-19"


def test_persist_orchestrator_results_overrides_non_us_sector_and_industry_with_taxonomy(session: Session):
    session.add_all(
        [
            StockUniverse(
                symbol="7203.T",
                market="JP",
                exchange="XTKS",
                currency="JPY",
                timezone="Asia/Tokyo",
            ),
            IBDGroupRank(
                market="JP",
                industry_group="Transportation Equipment",
                date=date(2026, 6, 14),
                rank=3,
                avg_rs_rating=86.0,
                rs_formula_version=BALANCED_RS_FORMULA_VERSION,
            ),
        ]
    )
    session.commit()

    raw = _base_raw_result()
    raw["gics_sector"] = "Consumer Discretionary"
    raw["gics_industry"] = "Legacy Industry"
    raw["ibd_industry_group"] = "Legacy Autos"
    raw["ibd_group_rank"] = 91

    class _FakeTaxonomyService:
        def get(self, symbol, *, market=None, exchange=None):  # noqa: ARG002
            if symbol == "7203.T" and market == "JP":
                return MarketTaxonomyEntry(
                    market="JP",
                    symbol="7203.T",
                    industry_group="Transportation Equipment",
                    sector="Manufacturing",
                    industry="Automobiles",
                    themes=("Automation",),
                )
            return None

    repo = SqlScanResultRepository(
        session,
        taxonomy_service=_FakeTaxonomyService(),
    )
    repo.persist_orchestrator_results("scan-jp-1", [("7203.T", raw)])

    row = (
        session.query(ScanResult)
        .filter(ScanResult.scan_id == "scan-jp-1", ScanResult.symbol == "7203.T")
        .one()
    )

    assert row.gics_sector == "Manufacturing"
    assert row.gics_industry == "Automobiles"
    assert row.ibd_industry_group == "Transportation Equipment"
    assert row.ibd_group_rank == 3
    assert row.details["market_themes"] == ["Automation"]


def test_backfill_ibd_metadata_for_existing_scan_rows(session: Session):
    session.add(Scan(scan_id="scan-us-1", status="completed", universe_market="US"))
    session.add_all(
        [
            ScanResult(
                scan_id="scan-us-1",
                symbol="AAPL",
                composite_score=80.0,
                rating="Buy",
                details={},
                ibd_industry_group=None,
                ibd_group_rank=None,
            ),
            ScanResult(
                scan_id="scan-us-1",
                symbol="MSFT",
                composite_score=79.0,
                rating="Watch",
                details={},
                ibd_industry_group=None,
                ibd_group_rank=None,
            ),
        ]
    )
    session.add_all(
        [
            IBDIndustryGroup(
                symbol="AAPL",
                industry_group="Computer-Hardware/Peripherals",
            ),
            IBDIndustryGroup(
                symbol="MSFT",
                industry_group="Software",
            ),
            IBDGroupRank(
                industry_group="Computer-Hardware/Peripherals",
                date=date(2026, 2, 19),
                rank=5,
                avg_rs_rating=80.0,
            ),
            IBDGroupRank(
                industry_group="Software",
                date=date(2026, 2, 19),
                rank=9,
                avg_rs_rating=74.0,
            ),
        ]
    )
    session.commit()

    repo = SqlScanResultRepository(session)
    stats = repo.backfill_ibd_metadata_for_scan("scan-us-1")

    rows = (
        session.query(ScanResult)
        .filter(ScanResult.scan_id == "scan-us-1")
        .order_by(ScanResult.symbol.asc())
        .all()
    )

    assert stats["updated_rows"] == 2
    assert rows[0].symbol == "AAPL"
    assert rows[0].ibd_industry_group == "Computer-Hardware/Peripherals"
    assert rows[0].ibd_group_rank == 5
    assert rows[1].symbol == "MSFT"
    assert rows[1].ibd_industry_group == "Software"
    assert rows[1].ibd_group_rank == 9


def test_persist_orchestrator_results_strips_market_before_rank_map_lookup(session: Session):
    session.add_all(
        [
            StockUniverse(
                symbol="0700.HK",
                market=" HK ",
                exchange="XHKG",
                currency="HKD",
                timezone="Asia/Hong_Kong",
            ),
            IBDGroupRank(
                market="HK",
                industry_group="Internet Services",
                date=date(2026, 6, 14),
                rank=4,
                avg_rs_rating=81.0,
                rs_formula_version=BALANCED_RS_FORMULA_VERSION,
            ),
        ]
    )
    session.commit()

    class _FakeTaxonomyService:
        def get(self, symbol, *, market=None, exchange=None):  # noqa: ARG002
            if symbol == "0700.HK" and market == "HK":
                return MarketTaxonomyEntry(
                    market="HK",
                    symbol="0700.HK",
                    industry_group="Internet Services",
                    themes=("AI Infrastructure",),
                )
            return None

    repo = SqlScanResultRepository(
        session,
        taxonomy_service=_FakeTaxonomyService(),
    )
    repo.persist_orchestrator_results("scan-hk-whitespace", [("0700.HK", _base_raw_result())])

    row = (
        session.query(ScanResult)
        .filter(
            ScanResult.scan_id == "scan-hk-whitespace",
            ScanResult.symbol == "0700.HK",
    )
        .one()
    )

    assert row.ibd_group_rank == 4
