"""A custom (watchlist) or test scan never stands in for a market's own scan."""

from __future__ import annotations

from datetime import date, datetime

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.infra.db.models.feature_store import FeatureRun
from app.infra.db.repositories.scan_repo import SqlScanRepository
from app.infra.db.uow import SqlUnitOfWork
from app.main import app
from app.models.scan_result import SCAN_TRIGGER_SOURCE_AUTO, Scan
from app.services.bootstrap_readiness_service import BootstrapReadinessService
from app.services.daily_snapshot_service import latest_completed_scan
from app.services import server_auth
from app.services.ui_snapshot_service import UISnapshotService
from app.wiring.bootstrap import get_uow


@pytest.fixture
def session_factory():
    # One connection, so the API's worker thread sees the same in-memory database.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _add_scan(session_factory, scan_id, universe_type, market, hour, minute=0, **fields):
    with session_factory() as db:
        db.add(
            Scan(
                scan_id=scan_id,
                status="completed",
                universe_type=universe_type,
                universe_market=market,
                universe_symbols=["AAPL"] if universe_type in ("custom", "test") else None,
                total_stocks=1,
                passed_stocks=1,
                started_at=datetime(2026, 9, 30, hour, minute),
                completed_at=datetime(2026, 9, 30, hour, minute, 30),
                **fields,
            )
        )
        db.commit()


@pytest.fixture
def scans(session_factory):
    _add_scan(session_factory, "us-old", "market", "US", 8)
    _add_scan(session_factory, "us-1", "market", "US", 9)
    _add_scan(session_factory, "hk-1", "market", "HK", 10)
    _add_scan(session_factory, "watchlist-unscoped", "custom", None, 11)
    # Still a symbol list when it stores the one market all its symbols share.
    _add_scan(session_factory, "watchlist-us", "custom", "US", 12)
    _add_scan(session_factory, "test-1", "test", None, 13)
    return session_factory


def test_daily_snapshot_reads_the_market_scan_not_a_newer_custom_scan(scans):
    with scans() as db:
        assert latest_completed_scan(db, "US").scan_id == "us-1"


def test_scan_bootstrap_selects_the_market_scan_not_a_newer_custom_scan(scans):
    snapshot = UISnapshotService(scans).publish_scan_bootstrap(market="US")

    assert snapshot.source_revision == "us-1"
    assert snapshot.payload["selected_scan"]["scan_id"] == "us-1"


def test_all_markets_scan_bootstrap_selects_the_newest_market_scan(scans):
    snapshot = UISnapshotService(scans).publish_scan_bootstrap()

    assert snapshot.source_revision == "hk-1"
    assert snapshot.payload["selected_scan"]["scan_id"] == "hk-1"


def test_symbol_list_scans_show_in_their_own_markets_history(scans):
    with scans() as db:
        repo = SqlScanRepository(db)
        assert [s.scan_id for s in repo.list_recent(market="US")] == [
            "test-1", "watchlist-us", "watchlist-unscoped", "us-1", "us-old",
        ]
        # A symbol list with no stored market belongs to every market's history.
        assert [s.scan_id for s in repo.list_recent(market="HK")] == [
            "test-1", "watchlist-unscoped", "hk-1",
        ]

    snapshot = UISnapshotService(scans).publish_scan_bootstrap(market="US")
    assert [s["scan_id"] for s in snapshot.payload["recent_scans"]["scans"]] == [
        "test-1", "watchlist-us", "watchlist-unscoped", "us-1", "us-old",
    ]


@pytest.fixture
def busy_watchlists(session_factory):
    _add_scan(session_factory, "us-1", "market", "US", 1)
    for n in range(25):
        _add_scan(session_factory, f"watchlist-{n}", "custom", None, 2, minute=n)
    return session_factory


# The 20 newest symbol-list scans, then the market's own scan.
BUSY_US_HISTORY = [f"watchlist-{n}" for n in range(24, 4, -1)] + ["us-1"]


def test_newer_watchlist_scans_never_push_the_market_scan_out_of_history(busy_watchlists):
    with busy_watchlists() as db:
        assert [s.scan_id for s in SqlScanRepository(db).list_recent(market="US")] == BUSY_US_HISTORY

    snapshot = UISnapshotService(busy_watchlists).publish_scan_bootstrap(market="US")
    assert [s["scan_id"] for s in snapshot.payload["recent_scans"]["scans"]] == BUSY_US_HISTORY
    assert snapshot.payload["selected_scan"]["scan_id"] == "us-1"


@pytest.mark.asyncio
async def test_scan_list_endpoint_keeps_the_market_scan_beneath_newer_watchlist_scans(
    busy_watchlists, monkeypatch
):
    monkeypatch.setattr(server_auth.settings, "server_auth_enabled", False)
    app.dependency_overrides[get_uow] = lambda: SqlUnitOfWork(busy_watchlists)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/scans", params={"market": "US"})
    finally:
        app.dependency_overrides.pop(get_uow, None)

    assert response.status_code == 200
    assert [s["scan_id"] for s in response.json()["scans"]] == BUSY_US_HISTORY


def test_bootstrap_readiness_waits_for_the_markets_own_auto_scan(session_factory):
    with session_factory() as db:
        db.add(FeatureRun(id=7, as_of_date=date(2026, 9, 30), run_type="daily_snapshot", status="published"))
        db.commit()
    service = BootstrapReadinessService()
    auto = {"trigger_source": SCAN_TRIGGER_SOURCE_AUTO, "feature_run_id": 7}

    _add_scan(session_factory, "watchlist-auto", "custom", "US", 9, **auto)
    with session_factory() as db:
        assert service.has_completed_auto_scan(db, "US") is False

    _add_scan(session_factory, "us-auto", "market", "US", 10, **auto)
    with session_factory() as db:
        assert service.has_completed_auto_scan(db, "US") is True


def test_scan_lists_flag_which_scans_are_market_views(scans):
    snapshot = UISnapshotService(scans).publish_scan_bootstrap(market="US")

    assert {s["scan_id"]: s["market_view"] for s in snapshot.payload["recent_scans"]["scans"]} == {
        "test-1": False,
        "watchlist-us": False,
        "watchlist-unscoped": False,
        "us-1": True,
        "us-old": True,
    }
    assert snapshot.payload["selected_scan"]["market_view"] is True


@pytest.mark.parametrize(
    ("universe_type", "universe", "expected"),
    [
        ("market", "US Market", True),
        ("index", "S&P 500", True),
        ("all", "All Stocks", True),
        ("custom", "Custom (3 symbols)", False),
        ("test", "Test (1 symbol)", False),
        # Pre-migration rows carry only the legacy universe label.
        (None, "market:us", True),
        (None, "all", True),
        (None, None, True),
        (None, "custom", False),
        (None, " Custom ", False),
        (None, "test", False),
        (None, "Custom (3 symbols)", False),
    ],
)
def test_market_view_rule_agrees_in_python_and_sql(session_factory, universe_type, universe, expected):
    with session_factory() as db:
        db.add(Scan(scan_id="row", status="completed", universe_type=universe_type, universe=universe))
        db.commit()
        row = db.query(Scan).one()

        assert row.represents_market_view() is expected
        in_sql = db.query(Scan.scan_id).filter(Scan.represents_market_view()).all()
        assert bool(in_sql) is expected


def test_a_legacy_custom_row_never_stands_in_for_the_market_scan(session_factory):
    _add_scan(session_factory, "us-1", "market", "US", 9)
    _add_scan(session_factory, "legacy-custom", None, "US", 10, universe="custom")

    with session_factory() as db:
        assert latest_completed_scan(db, "US").scan_id == "us-1"


def test_every_latest_scan_picker_agrees_when_scans_finish_out_of_order(session_factory):
    from app.services.theme_discovery_service import ThemeDiscoveryService

    with session_factory() as db:
        for scan_id, started, completed in (
            # Started first but finished last.
            ("us-long", datetime(2026, 9, 30, 9), datetime(2026, 9, 30, 12)),
            ("us-quick", datetime(2026, 9, 30, 10), datetime(2026, 9, 30, 11)),
        ):
            db.add(
                Scan(
                    scan_id=scan_id,
                    status="completed",
                    universe_type="market",
                    universe_market="US",
                    started_at=started,
                    completed_at=completed,
                )
            )
        db.commit()

    snapshot = UISnapshotService(session_factory).publish_scan_bootstrap(market="US")
    with session_factory() as db:
        history = SqlScanRepository(db).list_recent(market="US")
        theme_pick = ThemeDiscoveryService.__new__(ThemeDiscoveryService)
        theme_pick.db = db

        assert history[0].scan_id == "us-quick"
        assert snapshot.payload["selected_scan"]["scan_id"] == "us-quick"
        assert latest_completed_scan(db, "US").scan_id == "us-quick"
        assert theme_pick._latest_completed_scan_id() == "us-quick"


def test_publishing_a_missing_latest_variant_serves_it_to_readers(scans, monkeypatch):
    import app.services.ui_snapshot_service as ui_snapshot_service
    import app.wiring.bootstrap as bootstrap

    service = UISnapshotService(scans)
    monkeypatch.setattr(bootstrap, "get_ui_snapshot_service", lambda: service)
    assert service.get_scan_bootstrap(market="US") is None

    ui_snapshot_service.safe_publish_scan_latest("US")

    snapshot = service.get_scan_bootstrap(market="US")
    assert snapshot is not None and snapshot.is_stale is False
    assert snapshot.payload["selected_scan"]["scan_id"] == "us-1"
