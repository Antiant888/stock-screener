"""Daily scans of flagged watchlists, through the real CreateScanUseCase and database."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch

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
from app.interfaces.tasks.feature_store_tasks import _repair_current_us_group_metadata
from app.models.scan_result import Scan, ScanResult
from app.models.stock_universe import StockUniverse
from app.models.theme import ThemeCluster, ThemeConstituent
from app.models.user_watchlist import UserWatchlist, WatchlistItem
from app.services import server_auth
from app.services.daily_snapshot_service import latest_completed_scan
from app.services.theme_discovery_service import ThemeDiscoveryService
from app.services.ui_snapshot_service import UISnapshotService
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


def _publish(db, market: str, passing: dict[str, bool], *, as_of: date = SESSION) -> int:
    profile = get_default_scan_profile(market)
    signature = build_scan_signature_payload(
        universe_type="market",
        screeners=profile["screeners"],
        composite_method=profile["composite_method"],
        criteria=profile["criteria"],
    )
    runs = SqlFeatureRunRepository(db)
    run = runs.start_run(
        as_of_date=as_of,
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
            as_of_date=as_of,
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


def _auto_scan(db, market: str = "US") -> str:
    """The market's own scan, as the snapshot stage leaves it before the watchlist step."""
    ran_at = datetime(2026, 10, 23, 21, 30, tzinfo=UTC)
    scan_id = f"auto-{market.lower()}"
    db.add(Scan(
        scan_id=scan_id, status="completed", trigger_source="auto", universe=f"{market} Market",
        universe_key=f"market:{market}", universe_type="market", universe_market=market,
        total_stocks=1, passed_stocks=1, started_at=ran_at, completed_at=ran_at,
    ))
    db.commit()
    return scan_id


def test_flagged_cross_market_watchlist_gets_one_scan_of_its_us_symbols(db):
    _list(db, {"AAPL": "US", "MSFT": "US", "TSLA": "US", "0700.HK": "HK"})
    run_id = _publish(db, "US", {"AAPL": True, "MSFT": False, "TSLA": True})
    leaders = _watchlist(db, "Leaders", ["MSFT", "0700.HK", "AAPL"])
    dispatcher = FakeTaskDispatcher()

    outcomes = scan_flagged_watchlists(_use_case(dispatcher), market="US", run_id=run_id, session_date=SESSION)

    [scan] = _scans(db)
    assert outcomes == [WatchlistScanOutcome(leaders, WatchlistScanStatus.CREATED, scan_id=scan.scan_id)]
    assert dispatcher.dispatched == []
    assert (scan.status, scan.trigger_source, scan.universe_type, scan.universe_market) == (
        "completed", "watchlist", "custom", "US",
    )
    assert scan.universe_symbols == ["MSFT", "AAPL"]
    assert (scan.total_stocks, scan.passed_stocks) == (2, 1)
    assert scan.warnings == []
    assert scan.metadata_json["watchlist"] == {"id": leaders, "name": "Leaders", "market": "US"}
    assert scan.metadata_json["published_source"]["feature_run_id"] == run_id
    assert scan.metadata_json["published_source"]["data_mode"] == "last_published"
    assert scan.idempotency_key == f"watchlist-daily:{leaders}:US:2026-10-23"
    rows = db.query(ScanResult.symbol).filter(ScanResult.scan_id == scan.scan_id).order_by(ScanResult.symbol)
    assert [symbol for (symbol,) in rows] == ["AAPL", "MSFT"]


def test_hk_step_for_the_same_watchlist_scans_only_its_hk_symbols(db):
    _list(db, {"AAPL": "US", "0700.HK": "HK"})
    _publish(db, "US", {"AAPL": True})
    hk_run = _publish(db, "HK", {"0700.HK": True})
    leaders = _watchlist(db, "Leaders", ["AAPL", "0700.HK"])

    outcomes = scan_flagged_watchlists(
        _use_case(FakeTaskDispatcher()), market="HK", run_id=hk_run, session_date=SESSION
    )

    [scan] = _scans(db)
    assert outcomes == [WatchlistScanOutcome(leaders, WatchlistScanStatus.CREATED, scan_id=scan.scan_id)]
    assert (scan.universe_market, scan.universe_symbols, scan.idempotency_key) == (
        "HK", ["0700.HK"], f"watchlist-daily:{leaders}:HK:2026-10-23",
    )


def test_unflagged_and_off_market_watchlists_get_no_scan(db):
    _list(db, {"AAPL": "US", "0700.HK": "HK"})
    run_id = _publish(db, "US", {"AAPL": True})
    _watchlist(db, "Unflagged", ["AAPL"], flagged=False)
    hong_kong = _watchlist(db, "Hong Kong", ["0700.HK"], position=1)

    outcomes = scan_flagged_watchlists(
        _use_case(FakeTaskDispatcher()), market="US", run_id=run_id, session_date=SESSION
    )

    assert outcomes == [WatchlistScanOutcome(hong_kong, WatchlistScanStatus.NO_SYMBOLS_IN_MARKET)]
    assert _scans(db) == []


def test_rerun_for_the_same_session_converges_on_one_scan(db):
    _list(db, {"AAPL": "US"})
    run_id = _publish(db, "US", {"AAPL": True})
    leaders = _watchlist(db, "Leaders", ["AAPL"])
    use_case = _use_case(FakeTaskDispatcher())

    first = scan_flagged_watchlists(use_case, market="US", run_id=run_id, session_date=SESSION)
    second = scan_flagged_watchlists(use_case, market="US", run_id=run_id, session_date=SESSION)

    [scan] = _scans(db)
    assert first == [WatchlistScanOutcome(leaders, WatchlistScanStatus.CREATED, scan_id=scan.scan_id)]
    assert second == [WatchlistScanOutcome(leaders, WatchlistScanStatus.DUPLICATE, scan_id=scan.scan_id)]


def test_a_run_the_market_has_since_replaced_gets_no_scan_keyed_to_its_session(db):
    _list(db, {"AAPL": "US"})
    guarded_run = _publish(db, "US", {"AAPL": True})
    _publish(db, "US", {"AAPL": False}, as_of=date(2026, 10, 26))
    leaders = _watchlist(db, "Leaders", ["AAPL"])

    outcomes = scan_flagged_watchlists(
        _use_case(FakeTaskDispatcher()), market="US", run_id=guarded_run, session_date=SESSION
    )

    assert outcomes == [
        WatchlistScanOutcome(leaders, WatchlistScanStatus.SNAPSHOT_UNAVAILABLE, reason="source_run_changed")
    ]
    assert _scans(db) == []


def test_a_watchlist_scans_its_covered_symbols_and_warns_about_the_rest(db):
    _list(db, {"AAPL": "US", "NEWCO": "US", "MSFT": "US"})
    run_id = _publish(db, "US", {"AAPL": True, "MSFT": False})
    leaders = _watchlist(db, "Leaders", ["AAPL", "NEWCO", "MSFT"])

    outcomes = scan_flagged_watchlists(
        _use_case(FakeTaskDispatcher()), market="US", run_id=run_id, session_date=SESSION
    )

    [scan] = _scans(db)
    assert outcomes == [WatchlistScanOutcome(leaders, WatchlistScanStatus.CREATED, scan_id=scan.scan_id)]
    assert (scan.universe_symbols, scan.total_stocks, scan.passed_stocks) == (["AAPL", "MSFT"], 2, 1)
    assert scan.metadata_json["published_source"]["feature_run_id"] == run_id
    assert scan.warnings == [{
        "code": "symbols_not_in_snapshot",
        "message": "Skipped 1 symbol with no row in the published snapshot: NEWCO",
        "dropped_symbols": ["NEWCO"],
        "dropped_count": 1,
    }]


def test_the_uncovered_symbols_warning_keeps_a_capped_sample(db):
    missing = [f"NEW{n:03d}" for n in range(205)]
    _list(db, {"AAPL": "US", **{symbol: "US" for symbol in missing}})
    run_id = _publish(db, "US", {"AAPL": True})
    _watchlist(db, "Leaders", ["AAPL", *missing])

    scan_flagged_watchlists(_use_case(FakeTaskDispatcher()), market="US", run_id=run_id, session_date=SESSION)

    [warning] = _scans(db)[0].warnings
    assert warning["dropped_count"] == 205
    assert warning["dropped_symbols"] == missing[:200]
    assert warning["message"] == (
        "Skipped 205 symbols with no row in the published snapshot: " + ", ".join(missing[:20]) + ", ..."
    )


def test_a_watchlist_the_snapshot_covers_none_of_does_not_stop_the_next(db):
    _list(db, {"AAPL": "US", "NEWCO": "US"})
    run_id = _publish(db, "US", {"AAPL": True})
    uncovered = _watchlist(db, "IPOs", ["NEWCO"], position=0)
    leaders = _watchlist(db, "Leaders", ["AAPL"], position=1)

    outcomes = scan_flagged_watchlists(
        _use_case(FakeTaskDispatcher()), market="US", run_id=run_id, session_date=SESSION
    )

    [scan] = _scans(db)
    assert outcomes == [
        WatchlistScanOutcome(uncovered, WatchlistScanStatus.SNAPSHOT_UNAVAILABLE, reason="symbols_not_in_snapshot"),
        WatchlistScanOutcome(leaders, WatchlistScanStatus.CREATED, scan_id=scan.scan_id),
    ]
    assert scan.metadata_json["watchlist"]["name"] == "Leaders"


def test_an_unexpected_error_fails_one_watchlist_and_the_rest_still_run(db):
    _list(db, {"AAPL": "US", "MSFT": "US"})
    run_id = _publish(db, "US", {"AAPL": True, "MSFT": True})
    broken = _watchlist(db, "Broken", ["MSFT"], position=0)
    leaders = _watchlist(db, "Leaders", ["AAPL"], position=1)
    real = _use_case(FakeTaskDispatcher())

    class FlakyUseCase:
        def execute(self, uow, cmd):
            if cmd.universe_symbols == ["MSFT"]:
                raise RuntimeError("database went away")
            return real.execute(uow, cmd)

    outcomes = scan_flagged_watchlists(FlakyUseCase(), market="US", run_id=run_id, session_date=SESSION)

    [scan] = _scans(db)
    assert outcomes == [
        WatchlistScanOutcome(broken, WatchlistScanStatus.FAILED, reason="database went away"),
        WatchlistScanOutcome(leaders, WatchlistScanStatus.CREATED, scan_id=scan.scan_id),
    ]


def test_retention_keeps_three_daily_scans_per_watchlist_and_never_the_auto_scan(db):
    _list(db, {"AAPL": "US"})
    leaders = _watchlist(db, "Leaders", ["AAPL"])
    _auto_scan(db)
    use_case = _use_case(FakeTaskDispatcher())

    for day in (19, 20, 21, 22, 23):
        session = date(2026, 10, day)
        run_id = _publish(db, "US", {"AAPL": True}, as_of=session)
        scan_flagged_watchlists(use_case, market="US", run_id=run_id, session_date=session)

    assert [(scan.universe_key, scan.idempotency_key) for scan in _scans(db)] == [
        ("market:US", None),
        (f"watchlist:{leaders}:US", f"watchlist-daily:{leaders}:US:2026-10-21"),
        (f"watchlist:{leaders}:US", f"watchlist-daily:{leaders}:US:2026-10-22"),
        (f"watchlist:{leaders}:US", f"watchlist-daily:{leaders}:US:2026-10-23"),
    ]


def test_a_daily_watchlist_scan_never_stands_in_for_the_markets_latest_scan(db):
    _list(db, {"AAPL": "US"})
    run_id = _publish(db, "US", {"AAPL": True})
    auto = _auto_scan(db)
    db.add(ScanResult(
        scan_id=auto, symbol="AAPL", minervini_score=80.0, stage=2, rs_rating=95.0,
        details={"applicable_screeners": ["minervini"], "unavailable_screeners": []},
    ))
    theme = ThemeCluster(
        name="Leaders", canonical_key="leaders", display_name="Leaders", pipeline="technical",
        is_active=True, lifecycle_state="active",
    )
    db.add(theme)
    db.flush()
    db.add(ThemeConstituent(theme_cluster_id=theme.id, symbol="AAPL", source="manual", confidence=1.0, is_active=True))
    db.commit()
    _watchlist(db, "Leaders", ["AAPL"])

    [outcome] = scan_flagged_watchlists(
        _use_case(FakeTaskDispatcher()), market="US", run_id=run_id, session_date=SESSION
    )
    with patch("app.services.ui_snapshot_service.safe_publish_scan_bootstrap") as repair_publish:
        _repair_current_us_group_metadata(ranking_date=SESSION, session_factory=SessionLocal)

    watchlist_scan = db.query(Scan).filter_by(scan_id=outcome.scan_id).one()
    assert (watchlist_scan.universe_type, watchlist_scan.universe_market) == ("custom", "US")
    assert latest_completed_scan(db, "US").scan_id == auto
    assert ThemeDiscoveryService(db, pipeline="technical").calculate_screener_metrics(theme.id) == {
        "num_passing_minervini": 1, "num_stage_2": 1, "avg_rs_rating": 95.0,
    }
    assert UISnapshotService(SessionLocal).publish_scan_bootstrap(market="US").source_revision == auto
    assert [call.args for call in repair_publish.call_args_list] == [(auto,)]


def test_pipeline_stage_scans_from_the_run_the_snapshot_guard_passed(db, monkeypatch):
    from app.tasks import daily_market_pipeline_tasks as module

    _list(db, {"AAPL": "US"})
    run_id = _publish(db, "US", {"AAPL": True})
    leaders = _watchlist(db, "Leaders", ["AAPL"])
    dispatcher = FakeTaskDispatcher()
    monkeypatch.setattr("app.wiring.bootstrap.get_create_scan_use_case", lambda: _use_case(dispatcher))
    guarded = {"status": "ok", "stage": "scan", "auto_scan_id": "auto-1", "run_id": run_id, "as_of_date": "2026-10-23"}

    payload = module.scan_watchlists_after_snapshot.run(guarded, market="us")

    [scan] = _scans(db)
    assert payload == {
        **guarded,
        "watchlist_scans": {
            "status": "completed",
            "outcomes": [
                {"watchlist_id": leaders, "status": "created", "scan_id": scan.scan_id, "reason": None}
            ],
        },
    }
    assert scan.idempotency_key == f"watchlist-daily:{leaders}:US:2026-10-23"
    assert dispatcher.dispatched == []


def test_pipeline_stage_skips_when_the_guard_names_no_run(db):
    from app.tasks import daily_market_pipeline_tasks as module

    _list(db, {"AAPL": "US"})
    _publish(db, "US", {"AAPL": True})
    _watchlist(db, "Leaders", ["AAPL"])

    payload = module.scan_watchlists_after_snapshot.run({"auto_scan_id": "auto-1"}, market="US")

    assert payload == {
        "auto_scan_id": "auto-1",
        "watchlist_scans": {"status": "skipped", "reason": "snapshot_run_unknown"},
    }
    assert _scans(db) == []


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("bad", {}),
        ({"run_id": "abc", "as_of_date": "2026-10-23"}, {"run_id": "abc", "as_of_date": "2026-10-23"}),
        ({"run_id": 5, "as_of_date": "not-a-date"}, {"run_id": 5, "as_of_date": "not-a-date"}),
    ],
)
def test_pipeline_stage_skips_a_payload_it_cannot_read(result, expected):
    from app.tasks import daily_market_pipeline_tasks as module

    payload = module.scan_watchlists_after_snapshot.run(result, market="US")

    assert payload == {**expected, "watchlist_scans": {"status": "skipped", "reason": "snapshot_run_unknown"}}


def test_a_session_other_than_the_runs_gets_no_scan(db):
    _list(db, {"AAPL": "US"})
    run_id = _publish(db, "US", {"AAPL": True})
    leaders = _watchlist(db, "Leaders", ["AAPL"])

    outcomes = scan_flagged_watchlists(
        _use_case(FakeTaskDispatcher()), market="US", run_id=run_id, session_date=date(2026, 10, 22)
    )

    assert outcomes == [
        WatchlistScanOutcome(leaders, WatchlistScanStatus.SKIPPED, reason="session_run_mismatch")
    ]
    assert _scans(db) == []


def test_a_soft_time_limit_stops_the_remaining_watchlists(db, monkeypatch):
    from celery.exceptions import SoftTimeLimitExceeded

    from app.tasks import daily_market_pipeline_tasks as module

    _list(db, {"AAPL": "US", "MSFT": "US"})
    run_id = _publish(db, "US", {"AAPL": True, "MSFT": True})
    _watchlist(db, "First", ["MSFT"], position=0)
    _watchlist(db, "Second", ["AAPL"], position=1)
    scanned = []

    class TimedOutUseCase:
        def execute(self, uow, cmd):
            scanned.append(cmd.universe_symbols)
            raise SoftTimeLimitExceeded("soft limit")

    monkeypatch.setattr("app.wiring.bootstrap.get_create_scan_use_case", TimedOutUseCase)
    guarded = {"auto_scan_id": "auto-1", "run_id": run_id, "as_of_date": "2026-10-23"}

    payload = module.scan_watchlists_after_snapshot.run(guarded, market="US")

    assert payload == {
        **guarded,
        "watchlist_scans": {"status": "failed", "reason": "SoftTimeLimitExceeded('soft limit',)"},
    }
    assert scanned == [["MSFT"]]
    assert _scans(db) == []


def test_pipeline_stage_records_a_failure_instead_of_ending_the_chain(monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app.tasks import daily_market_pipeline_tasks as module

    def _unreachable(db, market):
        raise OperationalError("SELECT", {}, Exception("connection refused"))

    monkeypatch.setattr("app.services.watchlist_daily_scans.flagged_watchlists_in_market", _unreachable)

    payload = module.scan_watchlists_after_snapshot.run(
        {"auto_scan_id": "auto-1", "run_id": 5, "as_of_date": "2026-10-23"}, market="HK"
    )

    assert payload["auto_scan_id"] == "auto-1"
    assert payload["watchlist_scans"]["status"] == "failed"
    assert "connection refused" in payload["watchlist_scans"]["reason"]


@pytest.mark.asyncio
async def test_a_daily_scan_lists_under_its_market_and_publishes_only_its_own_bootstrap(db, monkeypatch):
    monkeypatch.setattr(server_auth.settings, "server_auth_enabled", False)
    _list(db, {"AAPL": "US", "NEWCO": "US", "0700.HK": "HK"})
    run_id = _publish(db, "US", {"AAPL": True})
    leaders = _watchlist(db, "Leaders", ["AAPL", "NEWCO", "0700.HK"])
    auto = _auto_scan(db)
    UISnapshotService(SessionLocal).publish_scan_bootstrap(market="US")

    [outcome] = scan_flagged_watchlists(
        _use_case(FakeTaskDispatcher()), market="US", run_id=run_id, session_date=SESSION
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        us_history = (await client.get("/api/v1/scans", params={"market": "US"})).json()["scans"]
        hk_history = (await client.get("/api/v1/scans", params={"market": "HK"})).json()["scans"]
        latest = (await client.get("/api/v1/scans/bootstrap", params={"market": "US"})).json()
        own = (await client.get("/api/v1/scans/bootstrap", params={"scan_id": outcome.scan_id})).json()

    assert [
        (scan["scan_id"], scan["trigger_source"], scan["universe_def"]["type"], scan["watchlist"])
        for scan in us_history
    ] == [
        (outcome.scan_id, "watchlist", "custom", {"id": leaders, "name": "Leaders", "market": "US"}),
        (auto, "auto", "market", None),
    ]
    assert [warning["message"] for warning in us_history[0]["warnings"]] == [
        "Skipped 1 symbol with no row in the published snapshot: NEWCO",
    ]
    assert hk_history == []
    assert latest["payload"]["selected_scan"]["scan_id"] == auto
    assert [scan["scan_id"] for scan in latest["payload"]["recent_scans"]["scans"]] == [auto]
    assert own["payload"]["selected_scan"]["scan_id"] == outcome.scan_id
