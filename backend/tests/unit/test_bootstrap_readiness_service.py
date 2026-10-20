"""Unit tests for Bootstrap readiness evaluation."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.domain.relative_strength import (
    BALANCED_RS_FORMULA_VERSION,
    BALANCED_RS_PRICE_BASIS,
    BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
)
from app.infra.db.models.feature_store import FeatureRun, FeatureRunPointer
from app.infra.db.models.relative_strength import MarketRsRun
from app.models.app_settings import AppSetting
from app.models.industry import IBDGroupRank
from app.models.market_breadth import MarketBreadth
from app.models.market_exposure import MarketExposure
import app.models.scan_result  # noqa: F401
import app.models.stock  # noqa: F401
import app.models.stock_universe  # noqa: F401
from app.models.scan_result import SCAN_TRIGGER_SOURCE_AUTO, SCAN_TRIGGER_SOURCE_MANUAL, Scan
from app.models.stock import StockFundamental, StockPrice
from app.models.stock_universe import StockUniverse
from app.services.bootstrap_readiness_service import (
    PRE_BOOTSTRAP_SEED_IMPORT_CATEGORY,
    PRE_BOOTSTRAP_SEED_IMPORT_KEY,
    PRE_BOOTSTRAP_SEED_IMPORT_SCHEMA_VERSION,
    BootstrapReadinessService,
)


# Rows the database still holds from before the single-formula cutover.
STORED_LEGACY_FORMULA = "legacy-linear-v1"



class FakeBootstrapReadinessService(BootstrapReadinessService):
    def __init__(
        self,
        *,
        core_ready: dict[str, bool],
        scan_ready: dict[str, bool],
        empty: bool = False,
    ) -> None:
        self.core_ready = core_ready
        self.scan_ready = scan_ready
        self.empty = empty

    def is_empty_system(self, db) -> bool:
        return self.empty

    def has_core_market_data(self, db, market: str) -> bool:
        return self.core_ready.get(market, False)

    def has_completed_auto_scan(self, db, market: str, *, bootstrap_started_at=None) -> bool:
        return self.scan_ready.get(market, False)


@pytest.fixture
def readiness_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(engine)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def seed_core_market_data(db, *, symbol: str = "AAPL", market: str = "US", active: bool = True) -> None:
    db.add(
        StockUniverse(
            symbol=symbol,
            name=f"{symbol} Inc.",
            market=market,
            exchange="NYSE",
            currency="USD",
            timezone="America/New_York",
            is_active=active,
        )
    )
    db.add(
        StockPrice(
            symbol=symbol,
            date=date(2026, 5, 1),
            open=100,
            high=101,
            low=99,
            close=100,
            volume=1_000_000,
        )
    )
    db.add(StockFundamental(symbol=symbol, market_cap=1_000_000_000))
    db.commit()


def seed_scan(
    db,
    *,
    market: str = "US",
    scan_status: str = "completed",
    trigger_source: str = SCAN_TRIGGER_SOURCE_AUTO,
    feature_status: str = "published",
    started_at: datetime | None = None,
) -> None:
    ran_at = started_at or datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
    feature_run = FeatureRun(
        as_of_date=date(2026, 5, 1),
        run_type="daily_snapshot",
        status=feature_status,
        published_at=ran_at if feature_status == "published" else None,
    )
    db.add(feature_run)
    db.flush()
    db.add(
        Scan(
            scan_id=str(uuid4()),
            criteria={},
            universe="all",
            universe_market=market,
            status=scan_status,
            trigger_source=trigger_source,
            feature_run_id=feature_run.id,
            started_at=ran_at,
            completed_at=ran_at if scan_status == "completed" else None,
        )
    )
    db.commit()


def seed_balanced_publication(
    db,
    *,
    market: str = "US",
    rs_status: str = "completed",
    feature_status: str = "published",
    snapshot_contract: bool = True,
    with_run_id: bool = True,
) -> None:
    published_at = datetime(2026, 5, 1, 21, 0, tzinfo=timezone.utc)
    rs_run = MarketRsRun(
        market=market,
        as_of_date=date(2026, 5, 1),
        formula_version=BALANCED_RS_FORMULA_VERSION,
        status=rs_status,
        benchmark_symbol="SPY",
        benchmark_as_of_date=date(2026, 5, 1),
        universe_hash="hash",
        expected_symbol_count=1,
        eligible_symbol_count=1,
        excluded_symbol_count=0,
        diagnostics_json=(
            {
                "price_basis": BALANCED_RS_PRICE_BASIS,
                "rs_snapshot_schema_version": BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
            }
            if snapshot_contract
            else {}
        ),
    )
    db.add(rs_run)
    db.flush()
    config = {
        "market": market,
        "rs_formula_version": BALANCED_RS_FORMULA_VERSION,
        "rs_as_of_date": "2026-05-01",
        "rs_universe_size": 1,
    }
    if with_run_id:
        config["market_rs_run_id"] = rs_run.id
    feature_run = FeatureRun(
        as_of_date=date(2026, 5, 1),
        run_type="daily_snapshot",
        status=feature_status,
        published_at=published_at if feature_status == "published" else None,
        config_json=config,
    )
    db.add(feature_run)
    db.flush()
    db.add(
        FeatureRunPointer(
            key=f"latest_published_market:{market}",
            run_id=feature_run.id,
        )
    )
    db.commit()


def test_readiness_requires_core_data_and_auto_scan_for_every_enabled_market() -> None:
    service = FakeBootstrapReadinessService(
        core_ready={"US": True, "HK": True},
        scan_ready={"US": True, "HK": False},
    )

    result = service.evaluate(object(), enabled_markets=["US", "HK"])

    assert result.ready is False
    assert result.missing_markets == ["HK"]
    assert result.market_results["HK"].core_ready is True
    assert result.market_results["HK"].scan_ready is False


def test_readiness_is_ready_when_every_enabled_market_is_complete() -> None:
    service = FakeBootstrapReadinessService(
        core_ready={"US": True, "HK": True},
        scan_ready={"US": True, "HK": True},
    )

    result = service.evaluate(object(), enabled_markets=["US", "HK"])

    assert result.ready is True
    assert result.missing_markets == []


def test_empty_system_is_reported_independently_from_market_readiness() -> None:
    service = FakeBootstrapReadinessService(core_ready={}, scan_ready={}, empty=True)

    result = service.evaluate(object(), enabled_markets=["US"])

    assert result.empty_system is True
    assert result.ready is False


def test_sql_service_reports_empty_system_without_rows(readiness_db) -> None:
    result = BootstrapReadinessService().evaluate(readiness_db, enabled_markets=["US"])

    assert result.empty_system is True
    assert result.ready is False
    assert result.missing_markets == ["US"]


def test_sql_service_reports_ready_with_core_data_and_published_auto_scan(readiness_db) -> None:
    seed_core_market_data(readiness_db)
    seed_scan(readiness_db)

    result = BootstrapReadinessService().evaluate(readiness_db, enabled_markets=["US"])

    assert result.empty_system is False
    assert result.ready is True
    assert result.missing_markets == []
    assert result.market_results["US"].core_ready is True
    assert result.market_results["US"].scan_ready is True


def test_sql_service_ignores_auto_scan_before_bootstrap_attempt(readiness_db) -> None:
    bootstrap_started_at = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
    seed_core_market_data(readiness_db)
    seed_scan(readiness_db, started_at=bootstrap_started_at - timedelta(minutes=1))

    result = BootstrapReadinessService().evaluate(
        readiness_db,
        enabled_markets=["US"],
        bootstrap_started_at=bootstrap_started_at,
    )

    assert result.ready is False
    assert result.missing_markets == ["US"]
    assert result.market_results["US"].core_ready is True
    assert result.market_results["US"].scan_ready is False


def test_sql_service_ignores_inactive_universe_rows_for_core_readiness(readiness_db) -> None:
    seed_core_market_data(readiness_db, active=False)
    seed_scan(readiness_db)

    result = BootstrapReadinessService().evaluate(readiness_db, enabled_markets=["US"])

    assert result.empty_system is True
    assert result.ready is False
    assert result.missing_markets == ["US"]
    assert result.market_results["US"].core_ready is False
    assert result.market_results["US"].scan_ready is True




def test_pristine_installation_rejects_inactive_universe_rows(readiness_db) -> None:
    readiness_db.add(
        StockUniverse(
            symbol="OLD",
            name="Old Corp",
            market="US",
            exchange="NYSE",
            currency="USD",
            timezone="America/New_York",
            is_active=False,
        )
    )
    readiness_db.commit()

    service = BootstrapReadinessService()
    assert service.is_empty_system(readiness_db) is True
    assert service.is_pristine_installation(readiness_db) is False


@pytest.mark.parametrize(
    "persisted_row",
    [
        StockPrice(
            symbol="ORPHAN",
            date=date(2026, 5, 1),
            open=100,
            high=101,
            low=99,
            close=100,
            volume=1_000,
        ),
        StockFundamental(symbol="ORPHAN", market_cap=1_000_000),
        Scan(scan_id="pristine-check", criteria={}, status="completed"),
        FeatureRun(
            as_of_date=date(2026, 5, 1),
            run_type="daily_snapshot",
            status="completed",
        ),
        MarketBreadth(
            market="US",
            date=date(2026, 5, 1),
        ),
        MarketExposure(
            market="US",
            date=date(2026, 5, 1),
            exposure_score=50.0,
            stance="neutral",
        ),
        IBDGroupRank(
            market="US",
            industry_group="Software",
            date=date(2026, 5, 1),
            rank=1,
            avg_rs_rating=90,
            rs_formula_version=STORED_LEGACY_FORMULA,
        ),
        MarketRsRun(
            market="US",
            as_of_date=date(2026, 5, 1),
            formula_version=STORED_LEGACY_FORMULA,
            status="completed",
            benchmark_symbol="SPY",
            benchmark_as_of_date=date(2026, 5, 1),
            universe_hash="hash",
            expected_symbol_count=0,
            eligible_symbol_count=0,
            excluded_symbol_count=0,
            diagnostics_json={},
        ),
    ],
    ids=[
        "price",
        "fundamental",
        "scan",
        "feature-run",
        "market-breadth",
        "market-exposure",
        "group-rank",
        "market-rs-run",
    ],
)
def test_pristine_installation_rejects_any_durable_data(
    readiness_db,
    persisted_row,
) -> None:
    readiness_db.add(persisted_row)
    readiness_db.commit()

    assert BootstrapReadinessService().is_pristine_installation(readiness_db) is False


def test_pre_bootstrap_seed_import_requires_marker_for_raw_inputs(
    readiness_db,
) -> None:
    seed_core_market_data(readiness_db)

    service = BootstrapReadinessService()
    assert service.is_pristine_installation(readiness_db) is False
    assert service.is_pre_bootstrap_seed_import_installation(readiness_db) is False


def test_pre_bootstrap_seed_import_marker_requires_pristine_state(
    readiness_db,
) -> None:
    seed_core_market_data(readiness_db)

    service = BootstrapReadinessService()

    assert (
        service.mark_pre_bootstrap_seed_import(
            readiness_db,
            source="group_history_reconciliation",
        )
        is False
    )
    assert service.has_pre_bootstrap_seed_import_marker(readiness_db) is False


def test_pre_bootstrap_seed_import_marker_allows_raw_bootstrap_inputs(
    readiness_db,
) -> None:
    service = BootstrapReadinessService()
    assert (
        service.mark_pre_bootstrap_seed_import(
            readiness_db,
            source="group_history_reconciliation",
        )
        is True
    )
    readiness_db.commit()
    seed_core_market_data(readiness_db)

    assert service.is_pristine_installation(readiness_db) is False
    assert service.is_pre_bootstrap_seed_import_installation(readiness_db) is True


@pytest.mark.parametrize(
    ("value", "category"),
    [
        ("not-json", PRE_BOOTSTRAP_SEED_IMPORT_CATEGORY),
        (
            json.dumps(
                {
                    "schema_version": PRE_BOOTSTRAP_SEED_IMPORT_SCHEMA_VERSION + 1,
                    "sources": ["group_history_reconciliation"],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ),
            PRE_BOOTSTRAP_SEED_IMPORT_CATEGORY,
        ),
        (
            json.dumps(
                {
                    "schema_version": PRE_BOOTSTRAP_SEED_IMPORT_SCHEMA_VERSION,
                    "sources": [],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ),
            PRE_BOOTSTRAP_SEED_IMPORT_CATEGORY,
        ),
        (
            json.dumps(
                {
                    "schema_version": PRE_BOOTSTRAP_SEED_IMPORT_SCHEMA_VERSION,
                    "sources": ["group_history_reconciliation"],
                    "updated_at": "not-a-date",
                }
            ),
            PRE_BOOTSTRAP_SEED_IMPORT_CATEGORY,
        ),
        (
            json.dumps(
                {
                    "schema_version": PRE_BOOTSTRAP_SEED_IMPORT_SCHEMA_VERSION,
                    "sources": ["group_history_reconciliation"],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ),
            "other",
        ),
    ],
    ids=[
        "malformed-json",
        "wrong-schema",
        "empty-sources",
        "invalid-updated-at",
        "wrong-category",
    ],
)
def test_pre_bootstrap_seed_import_rejects_invalid_marker_metadata(
    readiness_db,
    value,
    category,
) -> None:
    readiness_db.add(
        AppSetting(
            key=PRE_BOOTSTRAP_SEED_IMPORT_KEY,
            value=value,
            category=category,
        )
    )
    readiness_db.commit()
    seed_core_market_data(readiness_db)

    service = BootstrapReadinessService()

    assert service.has_pre_bootstrap_seed_import_marker(readiness_db) is False
    assert service.is_pre_bootstrap_seed_import_installation(readiness_db) is False


def test_pre_bootstrap_seed_import_keeps_valid_old_startup_marker(
    readiness_db,
) -> None:
    readiness_db.add(
        AppSetting(
            key=PRE_BOOTSTRAP_SEED_IMPORT_KEY,
            value=json.dumps(
                {
                    "schema_version": PRE_BOOTSTRAP_SEED_IMPORT_SCHEMA_VERSION,
                    "sources": ["group_history_reconciliation"],
                    "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
                }
            ),
            category=PRE_BOOTSTRAP_SEED_IMPORT_CATEGORY,
        )
    )
    readiness_db.commit()
    seed_core_market_data(readiness_db)

    assert (
        BootstrapReadinessService().is_pre_bootstrap_seed_import_installation(
            readiness_db
        )
        is True
    )


@pytest.mark.parametrize(
    "persisted_row",
    [
        Scan(scan_id="seed-only-scan", criteria={}, status="completed"),
        FeatureRun(
            as_of_date=date(2026, 5, 1),
            run_type="daily_snapshot",
            status="completed",
        ),
        MarketBreadth(
            market="US",
            date=date(2026, 5, 1),
        ),
        MarketExposure(
            market="US",
            date=date(2026, 5, 1),
            exposure_score=50.0,
            stance="neutral",
        ),
        IBDGroupRank(
            market="US",
            industry_group="Software",
            date=date(2026, 5, 1),
            rank=1,
            avg_rs_rating=90,
            rs_formula_version=STORED_LEGACY_FORMULA,
        ),
        MarketRsRun(
            market="US",
            as_of_date=date(2026, 5, 1),
            formula_version=STORED_LEGACY_FORMULA,
            status="completed",
            benchmark_symbol="SPY",
            benchmark_as_of_date=date(2026, 5, 1),
            universe_hash="hash",
            expected_symbol_count=0,
            eligible_symbol_count=0,
            excluded_symbol_count=0,
            diagnostics_json={},
        ),
    ],
    ids=[
        "scan",
        "feature-run",
        "market-breadth",
        "market-exposure",
        "group-rank",
        "market-rs-run",
    ],
)
def test_pre_bootstrap_seed_import_rejects_bootstrap_outputs(
    readiness_db,
    persisted_row,
) -> None:
    readiness_db.add(persisted_row)
    readiness_db.commit()

    service = BootstrapReadinessService()
    assert (
        service.mark_pre_bootstrap_seed_import(
            readiness_db,
            source="group_history_reconciliation",
        )
        is False
    )
    assert service.is_pre_bootstrap_seed_import_installation(readiness_db) is False


def test_market_readiness_requires_a_committed_balanced_publication(readiness_db) -> None:
    seed_core_market_data(readiness_db)
    seed_scan(readiness_db)
    expectation = {"US": BALANCED_RS_FORMULA_VERSION}
    # The shadow backfill's state: completed balanced runs, nothing published.
    readiness_db.add(
        MarketRsRun(
            market="US",
            as_of_date=date(2026, 4, 30),
            formula_version=BALANCED_RS_FORMULA_VERSION,
            status="completed",
            benchmark_symbol="SPY",
            benchmark_as_of_date=date(2026, 4, 30),
            universe_hash="hash",
            expected_symbol_count=1,
            eligible_symbol_count=1,
            excluded_symbol_count=0,
            diagnostics_json={
                "price_basis": BALANCED_RS_PRICE_BASIS,
                "rs_snapshot_schema_version": BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
            },
        )
    )
    readiness_db.commit()

    before = BootstrapReadinessService().evaluate(
        readiness_db,
        enabled_markets=["US"],
        expected_formula_versions=expectation,
    ).market_results["US"]

    assert before.core_ready is True
    assert before.scan_ready is True
    assert before.rs_ready is False
    assert before.ready is False

    seed_balanced_publication(readiness_db)

    after = BootstrapReadinessService().evaluate(
        readiness_db,
        enabled_markets=["US"],
        expected_formula_versions=expectation,
    ).market_results["US"]

    assert after.rs_ready is True
    assert after.ready is True


@pytest.mark.parametrize(
    "publication_kwargs",
    [
        {"feature_status": "completed"},
        {"rs_status": "failed"},
        {"snapshot_contract": False},
        {"with_run_id": False},
    ],
    ids=[
        "feature-run-not-published",
        "market-rs-run-not-completed",
        "old-snapshot-contract",
        "no-market-rs-run-id",
    ],
)
def test_market_readiness_rejects_an_incomplete_publication(
    readiness_db,
    publication_kwargs,
) -> None:
    seed_core_market_data(readiness_db)
    seed_scan(readiness_db)
    seed_balanced_publication(readiness_db, **publication_kwargs)

    result = BootstrapReadinessService().evaluate(
        readiness_db,
        enabled_markets=["US"],
        expected_formula_versions={"US": BALANCED_RS_FORMULA_VERSION},
    ).market_results["US"]

    assert result.rs_ready is False
    assert result.ready is False


def test_runtime_bootstrap_status_stays_failed_until_the_publication_commits(
    readiness_db,
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from app.services import runtime_preferences_service as module

    seed_core_market_data(readiness_db)
    seed_scan(readiness_db)
    readiness_db.add(
        MarketRsRun(
            market="US",
            as_of_date=date(2026, 4, 30),
            formula_version=BALANCED_RS_FORMULA_VERSION,
            status="completed",
            benchmark_symbol="SPY",
            benchmark_as_of_date=date(2026, 4, 30),
            universe_hash="hash",
            expected_symbol_count=1,
            eligible_symbol_count=1,
            excluded_symbol_count=0,
            diagnostics_json={},
        )
    )
    readiness_db.commit()
    prefs = module.RuntimePreferences(
        primary_market="US",
        enabled_markets=["US"],
        bootstrap_state="failed",
    )
    manifest = SimpleNamespace(pending_balanced_activation_markets=("US",))
    monkeypatch.setattr(module, "get_runtime_preferences", lambda _db: prefs)
    monkeypatch.setattr(
        module,
        "BootstrapRunManifestRepository",
        lambda: SimpleNamespace(load=lambda _db: manifest),
    )

    status = module.get_runtime_bootstrap_status(readiness_db)

    assert status.bootstrap_state == "failed"
    assert status.bootstrap_required is True

    seed_balanced_publication(readiness_db)

    status = module.get_runtime_bootstrap_status(readiness_db)

    assert status.bootstrap_state == "ready"
    assert status.bootstrap_required is False


def test_market_readiness_is_rs_ready_without_expectation(readiness_db) -> None:
    seed_core_market_data(readiness_db)
    seed_scan(readiness_db)

    result = BootstrapReadinessService().evaluate(
        readiness_db,
        enabled_markets=["US"],
    ).market_results["US"]

    assert result.rs_ready is True
    assert result.ready is True


@pytest.mark.parametrize(
    "scan_kwargs",
    [
        {"market": "HK"},
        {"scan_status": "running"},
        {"trigger_source": SCAN_TRIGGER_SOURCE_MANUAL},
        {"feature_status": "completed"},
    ],
)
def test_sql_service_requires_published_completed_auto_scan_for_market(
    readiness_db,
    scan_kwargs,
) -> None:
    seed_core_market_data(readiness_db)
    seed_scan(readiness_db, **scan_kwargs)

    result = BootstrapReadinessService().evaluate(readiness_db, enabled_markets=["US"])

    assert result.ready is False
    assert result.missing_markets == ["US"]
    assert result.market_results["US"].core_ready is True
    assert result.market_results["US"].scan_ready is False


def test_stage_status_reports_outputs_per_capable_market(readiness_db) -> None:
    readiness_db.add(MarketBreadth(market="US", date=date(2026, 5, 1)))
    readiness_db.commit()
    service = BootstrapReadinessService()

    assert service.stage_status(readiness_db, "US") == {
        "breadth": "ready",
        "exposure": "missing",
        "groups": "missing",
    }
    # DE computes breadth but has no group rankings; AU has neither.
    assert service.stage_status(readiness_db, "DE") == {
        "breadth": "missing",
        "exposure": "missing",
    }
    assert service.stage_status(readiness_db, "AU") == {}


def test_feature_status_is_independent_of_market_readiness(readiness_db, monkeypatch) -> None:
    import app.infra.db.models.cot  # noqa: F401
    import app.infra.db.models.options_analytics  # noqa: F401
    import app.infra.db.models.social_signals  # noqa: F401
    from app.config import settings
    from app.infra.db.models.social_signals import SocialSourceRegistry

    Base.metadata.create_all(readiness_db.get_bind())
    service = BootstrapReadinessService()
    monkeypatch.setattr(settings, "options_analytics_enabled", True)

    assert service.feature_status(readiness_db, enabled_markets=["US"]) == {
        "cot": "missing",
        "options": "missing",
        "social": "disabled",
    }
    assert service.feature_status(readiness_db, enabled_markets=["HK"])["options"] == "disabled"

    readiness_db.add(SocialSourceRegistry(id=1, mode="live", provider="xui"))
    readiness_db.commit()
    assert service.feature_status(readiness_db, enabled_markets=["US"])["social"] == "missing"


def test_feature_status_ignores_publications_from_older_versions(readiness_db, monkeypatch) -> None:
    """An upgrade that bumps a version leaves a pointer the read path rejects."""
    import app.infra.db.models.social_signals  # noqa: F401
    from app.config import settings
    from app.domain.cot.models import (
        COT_CALCULATION_VERSION,
        COT_REGISTRY_VERSION,
        COT_SCHEMA_VERSION,
    )
    from app.infra.db.models.cot import CotImportRun, CotPublicationPointer
    from app.infra.db.models.options_analytics import OptionsAnalyticsPointer
    from app.use_cases.options_analytics import OPTIONS_ANALYTICS_CALCULATION_VERSION

    Base.metadata.create_all(readiness_db.get_bind())
    monkeypatch.setattr(settings, "options_analytics_enabled", True)
    service = BootstrapReadinessService()

    def cot_run(run_id, calculation_version):
        readiness_db.add(
            CotImportRun(
                id=run_id,
                origin="test",
                status="published",
                registry_version=COT_REGISTRY_VERSION,
                schema_version=COT_SCHEMA_VERSION,
                calculation_version=calculation_version,
            )
        )

    cot_run(1, "cot-positions-v0")
    readiness_db.add(CotPublicationPointer(key="latest_published", run_id=1, report_date=date(2026, 9, 22)))
    readiness_db.add(OptionsAnalyticsPointer(market="US", calculation_version="options-analytics-v0", run_id=1))
    readiness_db.commit()
    stale = service.feature_status(readiness_db, enabled_markets=["US"])
    assert (stale["cot"], stale["options"]) == ("missing", "missing")

    cot_run(2, COT_CALCULATION_VERSION)
    readiness_db.query(CotPublicationPointer).update({"run_id": 2})
    readiness_db.add(
        OptionsAnalyticsPointer(market="US", calculation_version=OPTIONS_ANALYTICS_CALCULATION_VERSION, run_id=2)
    )
    readiness_db.commit()
    current = service.feature_status(readiness_db, enabled_markets=["US"])
    assert (current["cot"], current["options"]) == ("ready", "ready")



def test_cot_readiness_only_counts_the_pointer_readers_load(readiness_db) -> None:
    from app.domain.cot.models import (
        COT_CALCULATION_VERSION,
        COT_REGISTRY_VERSION,
        COT_SCHEMA_VERSION,
    )
    from app.infra.db.models.cot import CotImportRun, CotPublicationPointer
    from app.services.bootstrap_readiness_service import has_compatible_cot_publication

    Base.metadata.create_all(readiness_db.get_bind())
    readiness_db.add(
        CotImportRun(
            id=1,
            origin="test",
            status="published",
            registry_version=COT_REGISTRY_VERSION,
            schema_version=COT_SCHEMA_VERSION,
            calculation_version=COT_CALCULATION_VERSION,
        )
    )
    readiness_db.add(CotPublicationPointer(key="staging", run_id=1, report_date=date(2026, 9, 22)))
    readiness_db.commit()
    assert has_compatible_cot_publication(readiness_db) is False

    readiness_db.add(CotPublicationPointer(key="latest_published", run_id=1, report_date=date(2026, 9, 22)))
    readiness_db.commit()
    assert has_compatible_cot_publication(readiness_db) is True
