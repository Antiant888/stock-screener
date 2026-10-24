"""Daily scans of flagged watchlists, through the real CreateScanUseCase and database."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest

from app.database import SessionLocal
from app.domain.feature_store.models import FeatureRowWrite, RunStats, RunType
from app.domain.relative_strength.calculator import BALANCED_RS_FORMULA_VERSION
from app.domain.scanning.defaults import get_default_scan_profile
from app.domain.scanning.signature import build_scan_signature_payload, hash_scan_signature
from app.infra.db.repositories.feature_run_repo import SqlFeatureRunRepository
from app.infra.db.repositories.feature_store_repo import SqlFeatureStoreRepository
from app.main import app
from app.models.scan_result import Scan, ScanResult
from app.models.stock_universe import StockUniverse
from app.models.user_watchlist import UserWatchlist, WatchlistItem
from app.services import server_auth
from app.services.watchlist_daily_scans import (
    WatchlistScanOutcome,
    WatchlistScanStatus,
    scan_flagged_watchlists,
)
from app.use_cases.scanning.create_scan import CreateScanUseCase
from tests.unit.use_cases.conftest import FakeTaskDispatcher

SESSION = date(2026, 10, 23)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _list(db, symbols: dict[str, str]) -> None:
    db.add_all(
        StockUniverse(symbol=symbol, name=symbol, market=market, is_active=True)
        for symbol, market in symbols.items()
    )
    db.commit()


def _watchlist(db, name: str, symbols: list[str], *, flagged: bool = True, position: int = 0) -> int:
    watchlist = UserWatchlist(name=name, position=position, include_in_daily_scan=flagged)
    db.add(watchlist)
    db.flush()
    db.add_all(
        WatchlistItem(watchlist_id=watchlist.id, symbol=symbol, position=index)
        for index, symbol in enumerate(symbols)
    )
    db.commit()
    return watchlist.id


def _publish(db, market: str, passing: dict[str, bool]) -> int:
    profile = get_default_scan_profile(market)
    signature = build_scan_signature_payload(
        universe_type="market",
        screeners=profile["screeners"],
        composite_method=profile["composite_method"],
        criteria=profile["criteria"],
    )
    runs = SqlFeatureRunRepository(db)
    run = runs.start_run(
        as_of_date=SESSION,
        run_type=RunType.DAILY_SNAPSHOT,
        universe_hash=f"market:{market}",
        input_hash=hash_scan_signature(signature),
        config_json={
            "signature": signature,
            "market": market,
            "rs_formula_version": BALANCED_RS_FORMULA_VERSION,
            "market_rs_run_id": 7,
        },
    )
    runs.mark_completed(
        run.id,
        stats=RunStats(
            total_symbols=len(passing), processed_symbols=len(passing), failed_symbols=0,
            duration_seconds=1.0, passed_symbols=sum(passing.values()),
        ),
    )
    runs.publish_atomically(run.id, pointer_key=f"latest_published_market:{market}")
    SqlFeatureStoreRepository(db).upsert_snapshot_rows(run.id, [
        FeatureRowWrite(
            symbol=symbol,
            as_of_date=SESSION,
            composite_score=85.0 if passes else 40.0,
            overall_rating=4 if passes else 2,
            passes_count=1 if passes else 0,
            details={
                "composite_score": 85.0 if passes else 40.0,
                "rating": "Buy" if passes else "Pass",
                "passes_template": passes,
                "current_price": 100.0,
                "rs_rating": 80,
                "applicable_screeners": ["minervini"],
                "unavailable_screeners": [],
            },
        )
        for symbol, passes in passing.items()
    ])
    db.commit()
    return run.id


def _use_case(dispatcher: FakeTaskDispatcher) -> CreateScanUseCase:
    return CreateScanUseCase(dispatcher, expected_session=lambda market: SESSION)


def _scans(db) -> list[Scan]:
    db.expire_all()
    return db.query(Scan).order_by(Scan.id).all()


def test_flagged_cross_market_watchlist_gets_one_scan_of_its_us_symbols(db):
    _list(db, {"AAPL": "US", "MSFT": "US", "TSLA": "US", "0700.HK": "HK"})
    run_id = _publish(db, "US", {"AAPL": True, "MSFT": False, "TSLA": True})
    leaders = _watchlist(db, "Leaders", ["MSFT", "0700.HK", "AAPL"])
    dispatcher = FakeTaskDispatcher()

    outcomes = scan_flagged_watchlists(_use_case(dispatcher), market="US", session_date=SESSION)

    [scan] = _scans(db)
    assert outcomes == [WatchlistScanOutcome(leaders, WatchlistScanStatus.CREATED, scan_id=scan.scan_id)]
    assert dispatcher.dispatched == []
    assert (scan.status, scan.trigger_source, scan.universe_type, scan.universe_market) == (
        "completed", "watchlist", "custom", "US",
    )
    assert scan.universe_symbols == ["MSFT", "AAPL"]
    assert (scan.total_stocks, scan.passed_stocks) == (2, 1)
    assert scan.metadata_json["watchlist"] == {"id": leaders, "name": "Leaders", "market": "US"}
    assert scan.metadata_json["published_source"]["feature_run_id"] == run_id
    assert scan.metadata_json["published_source"]["data_mode"] == "last_published"
    assert scan.idempotency_key == f"watchlist-daily:{leaders}:US:2026-10-23"
    rows = db.query(ScanResult.symbol).filter(ScanResult.scan_id == scan.scan_id).order_by(ScanResult.symbol)
    assert [symbol for (symbol,) in rows] == ["AAPL", "MSFT"]


def test_hk_step_for_the_same_watchlist_has_no_hk_publication_to_answer_from(db):
    _list(db, {"AAPL": "US", "0700.HK": "HK"})
    _publish(db, "US", {"AAPL": True})
    leaders = _watchlist(db, "Leaders", ["AAPL", "0700.HK"])

    outcomes = scan_flagged_watchlists(_use_case(FakeTaskDispatcher()), market="HK", session_date=SESSION)

    assert outcomes == [
        WatchlistScanOutcome(leaders, WatchlistScanStatus.SNAPSHOT_UNAVAILABLE, reason="no_published_run")
    ]
    assert _scans(db) == []


def test_unflagged_and_off_market_watchlists_get_no_scan(db):
    _list(db, {"AAPL": "US", "0700.HK": "HK"})
    _publish(db, "US", {"AAPL": True})
    _watchlist(db, "Unflagged", ["AAPL"], flagged=False)
    hong_kong = _watchlist(db, "Hong Kong", ["0700.HK"], position=1)

    outcomes = scan_flagged_watchlists(_use_case(FakeTaskDispatcher()), market="US", session_date=SESSION)

    assert outcomes == [WatchlistScanOutcome(hong_kong, WatchlistScanStatus.NO_SYMBOLS_IN_MARKET)]
    assert _scans(db) == []


def test_rerun_for_the_same_session_converges_on_one_scan(db):
    _list(db, {"AAPL": "US"})
    _publish(db, "US", {"AAPL": True})
    leaders = _watchlist(db, "Leaders", ["AAPL"])
    use_case = _use_case(FakeTaskDispatcher())

    first = scan_flagged_watchlists(use_case, market="US", session_date=SESSION)
    second = scan_flagged_watchlists(use_case, market="US", session_date=SESSION)

    [scan] = _scans(db)
    assert first == [WatchlistScanOutcome(leaders, WatchlistScanStatus.CREATED, scan_id=scan.scan_id)]
    assert second == [WatchlistScanOutcome(leaders, WatchlistScanStatus.DUPLICATE, scan_id=scan.scan_id)]


def test_a_watchlist_the_snapshot_cannot_answer_does_not_stop_the_next(db):
    _list(db, {"AAPL": "US", "NEWCO": "US"})
    _publish(db, "US", {"AAPL": True})
    uncovered = _watchlist(db, "IPOs", ["NEWCO"], position=0)
    leaders = _watchlist(db, "Leaders", ["AAPL"], position=1)

    outcomes = scan_flagged_watchlists(_use_case(FakeTaskDispatcher()), market="US", session_date=SESSION)

    [scan] = _scans(db)
    assert outcomes == [
        WatchlistScanOutcome(uncovered, WatchlistScanStatus.SNAPSHOT_UNAVAILABLE, reason="incomplete_coverage"),
        WatchlistScanOutcome(leaders, WatchlistScanStatus.CREATED, scan_id=scan.scan_id),
    ]
    assert scan.metadata_json["watchlist"]["name"] == "Leaders"


def test_an_unexpected_error_fails_one_watchlist_and_the_rest_still_run(db):
    _list(db, {"AAPL": "US", "MSFT": "US"})
    _publish(db, "US", {"AAPL": True, "MSFT": True})
    broken = _watchlist(db, "Broken", ["MSFT"], position=0)
    leaders = _watchlist(db, "Leaders", ["AAPL"], position=1)
    real = _use_case(FakeTaskDispatcher())

    class FlakyUseCase:
        def execute(self, uow, cmd):
            if cmd.universe_symbols == ["MSFT"]:
                raise RuntimeError("database went away")
            return real.execute(uow, cmd)

    outcomes = scan_flagged_watchlists(FlakyUseCase(), market="US", session_date=SESSION)

    [scan] = _scans(db)
    assert outcomes == [
        WatchlistScanOutcome(broken, WatchlistScanStatus.FAILED, reason="database went away"),
        WatchlistScanOutcome(leaders, WatchlistScanStatus.CREATED, scan_id=scan.scan_id),
    ]


def test_retention_keeps_the_three_newest_daily_scans_per_watchlist_and_market(db):
    _list(db, {"AAPL": "US"})
    _publish(db, "US", {"AAPL": True})
    leaders = _watchlist(db, "Leaders", ["AAPL"])
    use_case = _use_case(FakeTaskDispatcher())

    for day in (19, 20, 21, 22, 23):
        scan_flagged_watchlists(use_case, market="US", session_date=date(2026, 10, day))

    assert [scan.idempotency_key for scan in _scans(db)] == [
        f"watchlist-daily:{leaders}:US:2026-10-21",
        f"watchlist-daily:{leaders}:US:2026-10-22",
        f"watchlist-daily:{leaders}:US:2026-10-23",
    ]


def test_pipeline_stage_adds_the_outcomes_to_the_snapshot_payload(db, monkeypatch):
    from app.tasks import daily_market_pipeline_tasks as module

    _list(db, {"AAPL": "US"})
    _publish(db, "US", {"AAPL": True})
    leaders = _watchlist(db, "Leaders", ["AAPL"])
    dispatcher = FakeTaskDispatcher()
    monkeypatch.setattr("app.wiring.bootstrap.get_create_scan_use_case", lambda: _use_case(dispatcher))

    payload = module.scan_watchlists_after_snapshot.run(
        {"status": "ok", "stage": "scan", "auto_scan_id": "auto-1"}, market="us", as_of_date="2026-10-23"
    )

    [scan] = _scans(db)
    assert payload == {
        "status": "ok",
        "stage": "scan",
        "auto_scan_id": "auto-1",
        "watchlist_scans": {
            "status": "completed",
            "outcomes": [
                {"watchlist_id": leaders, "status": "created", "scan_id": scan.scan_id, "reason": None}
            ],
        },
    }
    assert scan.idempotency_key == f"watchlist-daily:{leaders}:US:2026-10-23"
    assert dispatcher.dispatched == []


def test_pipeline_stage_records_a_failure_instead_of_ending_the_chain(monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app.tasks import daily_market_pipeline_tasks as module

    def _unreachable(db, market):
        raise OperationalError("SELECT", {}, Exception("connection refused"))

    monkeypatch.setattr("app.services.watchlist_daily_scans.flagged_watchlists_in_market", _unreachable)

    payload = module.scan_watchlists_after_snapshot.run(
        {"auto_scan_id": "auto-1"}, market="HK", as_of_date="2026-10-23"
    )

    assert payload["auto_scan_id"] == "auto-1"
    assert payload["watchlist_scans"]["status"] == "failed"
    assert "connection refused" in payload["watchlist_scans"]["reason"]


@pytest.mark.asyncio
async def test_scan_list_names_the_watchlist_and_the_market_scan_stays_the_default(db, monkeypatch):
    monkeypatch.setattr(server_auth.settings, "server_auth_enabled", False)
    _list(db, {"AAPL": "US"})
    _publish(db, "US", {"AAPL": True})
    leaders = _watchlist(db, "Leaders", ["AAPL"])
    market_scan_time = datetime(2026, 1, 2, 21, 30, tzinfo=UTC)
    db.add(Scan(
        scan_id="auto-us", status="completed", trigger_source="auto", universe="US Market",
        universe_key="market:US", universe_type="market", universe_market="US",
        total_stocks=1, passed_stocks=1, started_at=market_scan_time, completed_at=market_scan_time,
    ))
    db.commit()
    [outcome] = scan_flagged_watchlists(_use_case(FakeTaskDispatcher()), market="US", session_date=SESSION)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        listed = (await client.get("/api/v1/scans", params={"market": "US"})).json()["scans"]
        bootstrap = (await client.get("/api/v1/scans/bootstrap", params={"market": "US"})).json()

    assert [(scan["scan_id"], scan["trigger_source"], scan["watchlist"]) for scan in listed] == [
        (outcome.scan_id, "watchlist", {"id": leaders, "name": "Leaders", "market": "US"}),
        ("auto-us", "auto", None),
    ]
    assert bootstrap["payload"]["selected_scan"]["scan_id"] == "auto-us"
    assert [scan["scan_id"] for scan in bootstrap["payload"]["recent_scans"]["scans"]] == [
        outcome.scan_id, "auto-us",
    ]
