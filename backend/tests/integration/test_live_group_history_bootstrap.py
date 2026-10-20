"""Acceptance coverage for live Group history repair and API reads."""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest

from app.database import get_db
from app.domain.relative_strength import (
    BALANCED_RS_FORMULA_VERSION,
    BALANCED_RS_PRICE_BASIS,
    BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
    HORIZON_SESSIONS,
)
from app.infra.db.models.relative_strength import MarketRsRun
from app.infra.db.repositories.market_rs_repo import MarketRsRunRepository
from app.main import app
from app.models.industry import IBDGroupRank, IBDIndustryGroup
from app.models.scan_result import Scan
from app.models.stock import StockPrice
from app.models.stock_universe import StockUniverse
from app.models.watchlist import Watchlist
from app.services.group_history_bootstrap_service import (
    GroupHistoryBootstrapService,
    GroupHistoryBootstrapStatus,
)
from app.services.group_history_readiness_service import (
    GroupHistoryReadinessService,
)
from app.services.group_history_reconciliation import GroupHistoryTarget
from app.services.group_history_snapshot_coordinator import (
    build_group_history_snapshot_coordinator,
)
from app.services.group_history_universe import GroupHistoryUniverseResolver
from app.services.group_rank_history_policy import (
    DEFAULT_CALENDAR_DAY_GROUP_RANK_HISTORY_LOOKBACK_DAYS,
)
from app.services.group_rank_snapshot_reader import GroupRankSnapshotReader
from app.services.point_in_time_universe_service import (
    PointInTimeUniverseUnavailable,
)
from app.services.rrg_history_provider import StoredGroupRankHistoryProvider
from app.services.server_auth import require_server_session


# Rows the database still holds from before the single-formula cutover.
STORED_LEGACY_FORMULA = "legacy-linear-v1"



class _WeekdayCalendar:
    @staticmethod
    def trading_days(_market: str, start: date, end: date) -> list[date]:
        days = []
        cursor = start
        while cursor <= end:
            if cursor.weekday() < 5:
                days.append(cursor)
            cursor += timedelta(days=1)
        return days

    @staticmethod
    def session_anchors(_market: str, as_of_date: date, *, offsets) -> dict:
        days_by_offset = {
            0: 0,
            1: 1,
            5: 7,
            21: 30,
            63: 90,
            126: 180,
            189: 270,
            252: 365,
        }
        return {
            offset: as_of_date - timedelta(days=days_by_offset[offset])
            for offset in (0, *offsets)
        }


def _store_balanced_snapshot(db, snapshot_date: date) -> None:
    run = MarketRsRun(
        market="US",
        as_of_date=snapshot_date,
        formula_version=BALANCED_RS_FORMULA_VERSION,
        status="completed",
        benchmark_symbol="SPY",
        benchmark_as_of_date=snapshot_date,
        universe_hash=f"acceptance-{snapshot_date.isoformat()}",
        expected_symbol_count=20,
        eligible_symbol_count=20,
        excluded_symbol_count=0,
        diagnostics_json={
            "price_basis": BALANCED_RS_PRICE_BASIS,
            "rs_snapshot_schema_version": BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
        },
    )
    db.add(run)
    db.flush()
    is_current = snapshot_date == date(2026, 6, 30)
    rows = (
        ("Group Alpha", 1 if is_current else 2, 40.0 + snapshot_date.toordinal() % 31),
        ("Group Beta", 2 if is_current else 1, 70.0 - snapshot_date.toordinal() % 29),
    )
    for group, rank, average_rs in rows:
        db.add(
            IBDGroupRank(
                market="US",
                industry_group=group,
                date=snapshot_date,
                rank=rank,
                avg_rs_rating=average_rs,
                avg_rs_rating_1m=average_rs - 1,
                avg_rs_rating_3m=average_rs - 2,
                num_stocks=10,
                num_stocks_rs_above_80=4,
                top_symbol="AAA" if group == "Group Alpha" else "BBB",
                top_rs_rating=95,
                rs_formula_version=BALANCED_RS_FORMULA_VERSION,
                market_rs_run_id=run.id,
            )
        )
    db.commit()


class _UnavailablePointInTimeUniverse:
    @staticmethod
    def resolve(_db, *, market: str, as_of_date: date):
        raise PointInTimeUniverseUnavailable(
            f"No historical lifecycle for {market} on {as_of_date}"
        )


def _seed_production_repair_inputs(
    db,
    *,
    calendar: _WeekdayCalendar,
    repair_date: date,
) -> None:
    groups = {
        "Group Alpha": ("A1", "A2", "A3"),
        "Group Beta": ("B1", "B2", "B3"),
    }
    for group, symbols in groups.items():
        for index, symbol in enumerate(symbols, start=1):
            db.add(
                StockUniverse(
                    symbol=symbol,
                    name=f"{symbol} Acceptance",
                    market="US",
                    exchange="NASDAQ",
                    market_cap=float(index * 100),
                    is_active=True,
                    status="active",
                )
            )
            db.add(
                IBDIndustryGroup(
                    symbol=symbol,
                    industry_group=group,
                    market="US",
                    source="manual",
                )
            )

    anchors = calendar.session_anchors(
        "US",
        repair_date,
        offsets=tuple(HORIZON_SESSIONS.values()),
    )
    for offset, anchor_date in anchors.items():
        db.add(
            StockPrice(
                symbol="SPY",
                date=anchor_date,
                close=100.0,
                adj_close=100.0,
            )
        )
        for symbol in groups["Group Alpha"]:
            value = 50.0 if offset == 0 else 100.0
            db.add(
                StockPrice(
                    symbol=symbol,
                    date=anchor_date,
                    close=value,
                    adj_close=value,
                )
            )
        for symbol in groups["Group Beta"]:
            value = 200.0 if offset == 0 else 100.0
            db.add(
                StockPrice(
                    symbol=symbol,
                    date=anchor_date,
                    close=value,
                    adj_close=value,
                )
            )
    db.commit()


@pytest.mark.asyncio
async def test_live_repair_populates_rank_changes_movers_and_rrg_without_data_loss(
    db_session,
    monkeypatch,
):
    through_date = date(2026, 6, 30)
    db_session.add_all(
        [
            StockPrice(
                symbol="KEEP",
                date=through_date,
                close=123.0,
                adj_close=123.0,
            ),
            Watchlist(symbol="KEEP", notes="preserve me"),
            Scan(
                scan_id="preserved-scan",
                criteria={"rs_min": 80},
                status="completed",
            ),
            IBDGroupRank(
                market="US",
                industry_group="Legacy Group",
                date=through_date,
                rank=1,
                avg_rs_rating=55,
                num_stocks=3,
                rs_formula_version=STORED_LEGACY_FORMULA,
            ),
        ]
    )
    db_session.commit()
    calendar = _WeekdayCalendar()
    desired_dates = calendar.trading_days(
        "US",
        through_date
        - timedelta(
            days=DEFAULT_CALENDAR_DAY_GROUP_RANK_HISTORY_LOOKBACK_DAYS
        ),
        through_date,
    )
    production_repair_date = desired_dates[0]
    for snapshot_date in desired_dates:
        if snapshot_date != production_repair_date:
            _store_balanced_snapshot(db_session, snapshot_date)
    _seed_production_repair_inputs(
        db_session,
        calendar=calendar,
        repair_date=production_repair_date,
    )

    preserved = {
        StockPrice: db_session.query(StockPrice).count(),
        Watchlist: db_session.query(Watchlist).count(),
        Scan: db_session.query(Scan).count(),
    }
    legacy_count = (
        db_session.query(IBDGroupRank)
        .filter(
            IBDGroupRank.rs_formula_version == STORED_LEGACY_FORMULA
        )
        .count()
    )

    repository = MarketRsRunRepository()
    universe_resolver = GroupHistoryUniverseResolver(
        point_in_time_universe=_UnavailablePointInTimeUniverse(),
    )
    coordinator = build_group_history_snapshot_coordinator(
        universe_resolver=universe_resolver,
        calendar_service=calendar,
        market_rs_repository=repository,
    )
    provider = StoredGroupRankHistoryProvider(snapshot_reader=GroupRankSnapshotReader())
    readiness = GroupHistoryReadinessService(
        calendar_service=calendar,
        snapshot_reader=coordinator.reader,
        rrg_history_provider=provider,
    )
    repair = GroupHistoryBootstrapService(
        readiness_service=readiness,
        snapshot_coordinator=coordinator,
        universe_resolver=universe_resolver,
    ).ensure(
        db_session,
        target=GroupHistoryTarget(
            market="US",
            formula_version=BALANCED_RS_FORMULA_VERSION,
            through_date=through_date,
        ),
    )

    assert repair.status is GroupHistoryBootstrapStatus.READY
    assert repair.after.ready is True
    assert repair.skipped_valid == len(desired_dates) - 1
    assert repair.processed_dates == (production_repair_date,)
    assert repair.policy_counts == {
        "current_active_fallback_v1": len(repair.processed_dates)
    }
    production_run = repository.get_completed_exact(
        db_session,
        market="US",
        as_of_date=production_repair_date,
        formula_version=BALANCED_RS_FORMULA_VERSION,
    )
    assert production_run is not None
    assert production_run.expected_symbol_count == 6
    assert production_run.eligible_symbol_count == 6
    for model, count in preserved.items():
        assert db_session.query(model).count() == count
    assert (
        db_session.query(IBDGroupRank)
        .filter(
            IBDGroupRank.rs_formula_version == STORED_LEGACY_FORMULA
        )
        .count()
        == legacy_count
    )

    def _override_get_db():
        yield db_session

    from app.api.v1 import groups as groups_api

    monkeypatch.setattr(
        groups_api,
        "cached_group_payload",
        lambda **kwargs: kwargs["compute"](),
    )
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_server_session] = lambda: True
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            rankings_response = await client.get(
                "/api/v1/groups/rankings/current",
                params={"market": "US", "limit": 10, "as_of_date": through_date},
            )
            assert rankings_response.status_code == 200, rankings_response.text
            rankings = rankings_response.json()["rankings"]
            assert rankings
            assert all(
                row[f"rank_change_{period}"] is not None
                for row in rankings
                for period in ("1w", "1m", "3m", "6m")
            )

            for period in ("1w", "1m", "3m", "6m"):
                movers_response = await client.get(
                    "/api/v1/groups/rankings/movers",
                    params={
                        "market": "US",
                        "period": period,
                        "as_of_date": through_date,
                    },
                )
                assert movers_response.status_code == 200
                movers = movers_response.json()
                assert movers["gainers"]
                assert movers["losers"]

            rrg_response = await client.get(
                "/api/v1/groups/rrg/scopes",
                params={"market": "US", "as_of_date": through_date},
            )
            assert rrg_response.status_code == 200
            rrg = rrg_response.json()
            assert "groups" in rrg["available_scopes"]
            assert rrg["payload"]["groups"]["groups"]
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_server_session, None)
