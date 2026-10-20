"""Unit tests for the RRG DB orchestrator (``RRGService``).

Uses an in-memory SQLite database seeded with ``IBDGroupRank`` history and a
minimal stub for the current-rankings lookup, so it exercises the single
batched query, group omission, and the sector roll-up without the full app.

(The pure RRG math is covered separately by
``tests/unit/golden/test_golden_rrg.py``.)
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.domain.relative_strength import (
    BALANCED_RS_FORMULA_VERSION,
    BALANCED_RS_PRICE_BASIS,
    BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
)
from app.infra.db.models.relative_strength import MarketRsRun
from app.models.industry import IBDGroupRank, IBDIndustryGroup
from app.models.stock_universe import StockUniverse
from app.services.group_rank_snapshot_reader import GroupSnapshotWindowResult
from app.services.rrg_history_provider import StoredGroupRankHistoryProvider
from app.services.rrg_service import RRGService
from app.services.static_rrg_history_bundle import (
    StaticRRGHistoryBundleService,
    StaticRRGHistoryProvider,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _weekly_fridays(n_weeks: int, start: date = date(2024, 1, 5)):
    """n_weeks consecutive Fridays, ascending."""
    return [start + timedelta(weeks=k) for k in range(n_weeks)]


def _seed_group(
    session,
    *,
    market,
    group,
    dates,
    rs_fn,
    num_stocks=10,
    rank=1,
    formula_version=BALANCED_RS_FORMULA_VERSION,
):
    for i, d in enumerate(dates):
        market_rs_run_id = None
        if formula_version == BALANCED_RS_FORMULA_VERSION:
            run = (
                session.query(MarketRsRun)
                .filter(
                    MarketRsRun.market == market,
                    MarketRsRun.as_of_date == d,
                    MarketRsRun.formula_version == formula_version,
                )
                .one_or_none()
            )
            if run is None:
                run = MarketRsRun(
                    market=market,
                    as_of_date=d,
                    formula_version=formula_version,
                    status="completed",
                    benchmark_symbol="SPY",
                    benchmark_as_of_date=d,
                    universe_hash=f"rrg-{market}-{d.isoformat()}",
                    expected_symbol_count=10,
                    eligible_symbol_count=10,
                    excluded_symbol_count=0,
                    diagnostics_json={
                        "price_basis": BALANCED_RS_PRICE_BASIS,
                        "rs_snapshot_schema_version": BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
                    },
                )
                session.add(run)
                session.flush()
            market_rs_run_id = run.id
        session.add(
            IBDGroupRank(
                market=market,
                industry_group=group,
                date=d,
                rank=rank,
                avg_rs_rating=float(rs_fn(i)),
                num_stocks=num_stocks,
                num_stocks_rs_above_80=0,
                rs_formula_version=formula_version,
                market_rs_run_id=market_rs_run_id,
            )
        )
    session.commit()


def _service(**kwargs):
    return RRGService(history_provider=StoredGroupRankHistoryProvider(), **kwargs)


def test_stored_rrg_history_provider_reads_only_balanced_rows():
    session = _session()
    dates = _weekly_fridays(14)
    _seed_group(
        session,
        market="HK",
        group="Internet Services",
        dates=dates,
        rs_fn=lambda i: 10 + i,
        formula_version="legacy-linear-v1",
    )
    _seed_group(
        session,
        market="HK",
        group="Internet Services",
        dates=dates,
        rs_fn=lambda i: 70 + i,
    )

    latest, _meta, series = StoredGroupRankHistoryProvider().get_all_groups_history(
        session,
        market="hk",
        days=365,
    )

    assert latest == dates[-1].isoformat()
    assert [point[1] for point in series["Internet Services"]] == [
        70.0 + index for index in range(14)
    ]


def test_live_and_static_rrg_coordinates_match_for_formula_isolated_history():
    session = _session()
    dates = _weekly_fridays(40)
    _seed_group(
        session,
        market="HK",
        group="Internet Services",
        dates=dates,
        rs_fn=lambda i: 40 + 0.6 * i,
        formula_version=BALANCED_RS_FORMULA_VERSION,
    )

    live = RRGService(history_provider=StoredGroupRankHistoryProvider()).get_rrg(session, market="HK")
    state = StaticRRGHistoryBundleService().build(
        session,
        market="HK",
        through_date=dates[-1],
        formula_version=BALANCED_RS_FORMULA_VERSION,
    )
    static = RRGService(
        history_provider=StaticRRGHistoryProvider(state)
    ).get_rrg(session, market="HK")

    assert state.rs_formula_version == BALANCED_RS_FORMULA_VERSION
    assert live["groups"] == static["groups"]


def test_get_rrg_groups_scope_returns_quadrants_and_tails():
    session = _session()
    dates = _weekly_fridays(40)
    _seed_group(session, market="US", group="AlphaTech", dates=dates,
                rs_fn=lambda i: 40 + 0.6 * i, num_stocks=12, rank=1)
    _seed_group(session, market="US", group="BetaMetals", dates=dates,
                rs_fn=lambda i: 60 - 0.6 * i, num_stocks=8, rank=2)

    latest = dates[-1].isoformat()
    payload = RRGService(
        history_provider=StoredGroupRankHistoryProvider()
    ).get_rrg(session, market="US")

    assert payload["date"] == latest
    assert payload["scope"] == "groups"
    by_name = {g["industry_group"]: g for g in payload["groups"]}
    assert set(by_name) == {"AlphaTech", "BetaMetals"}

    alpha = by_name["AlphaTech"]
    assert alpha["quadrant"] == "Leading"      # rising RS
    assert alpha["current"]["x"] > 100 and alpha["current"]["y"] > 100
    assert alpha["current"] == alpha["tail"][-1]
    assert by_name["BetaMetals"]["quadrant"] == "Lagging"  # falling RS

    # Sorted by rank ascending.
    assert [g["industry_group"] for g in payload["groups"]] == ["AlphaTech", "BetaMetals"]


def test_get_rrg_passes_as_of_date_to_history_provider():
    session = _session()
    expected_date = date(2026, 6, 11)

    class _FakeHistoryProvider:
        def get_all_groups_history(self, db, *, market, days, as_of_date=None):
            assert market == "US"
            assert as_of_date == expected_date
            return (
                expected_date.isoformat(),
                {
                    "Software": {
                        "industry_group": "Software",
                        "date": expected_date.isoformat(),
                        "rank": 1,
                        "num_stocks": 10,
                        "avg_rs_rating": 80.0,
                    }
                },
                {
                    "Software": [
                        (expected_date - timedelta(weeks=i), 80.0 - i, 10)
                        for i in range(40)
                    ]
                },
            )

    payload = RRGService(history_provider=_FakeHistoryProvider()).get_rrg(
        session,
        market="US",
        as_of_date=expected_date,
    )

    assert payload["date"] == "2026-06-11"


def test_get_rrg_omits_groups_with_too_little_history():
    session = _session()
    dates = _weekly_fridays(40)
    _seed_group(session, market="US", group="AlphaTech", dates=dates,
                rs_fn=lambda i: 40 + 0.6 * i)
    # Only 5 weekly points -> below MIN_TAIL_WEEKS -> omitted.
    _seed_group(session, market="US", group="TinyNew", dates=_weekly_fridays(5),
                rs_fn=lambda i: 50.0, rank=2)

    payload = RRGService(
        history_provider=StoredGroupRankHistoryProvider()
    ).get_rrg(session, market="US")
    assert [g["industry_group"] for g in payload["groups"]] == ["AlphaTech"]


def test_get_rrg_sectors_scope_rolls_groups_into_gics_sectors():
    session = _session()
    dates = _weekly_fridays(40)
    _seed_group(session, market="US", group="AlphaTech", dates=dates,
                rs_fn=lambda i: 40 + 0.6 * i, num_stocks=12, rank=1)
    _seed_group(session, market="US", group="GammaChips", dates=dates,
                rs_fn=lambda i: 42 + 0.5 * i, num_stocks=8, rank=2)
    _seed_group(session, market="US", group="BetaMetals", dates=dates,
                rs_fn=lambda i: 60 - 0.6 * i, num_stocks=10, rank=3)

    # Group -> dominant GICS sector via constituents.
    for sym, grp, sector in [
        ("AAA", "AlphaTech", "Technology"),
        ("CCC", "GammaChips", "Technology"),
        ("MMM", "BetaMetals", "Basic Materials"),
    ]:
        session.add(IBDIndustryGroup(symbol=sym, industry_group=grp, market="US"))
        session.add(StockUniverse(symbol=sym, market="US", sector=sector))
    session.commit()

    payload = RRGService(history_provider=StoredGroupRankHistoryProvider()).get_rrg(
        session, market="US", scope="sectors"
    )

    assert payload["scope"] == "sectors"
    sectors = {g["industry_group"]: g for g in payload["groups"]}
    assert set(sectors) == {"Technology", "Basic Materials"}
    # Technology aggregates two rising groups -> Leading; num_stocks summed.
    assert sectors["Technology"]["quadrant"] == "Leading"
    assert sectors["Technology"]["num_stocks"] == 20
    assert sectors["Basic Materials"]["quadrant"] == "Lagging"


def test_get_group_sector_map_majority_vote():
    session = _session()
    for sym, sector in [("A1", "Technology"), ("A2", "Technology"), ("A3", "Healthcare")]:
        session.add(IBDIndustryGroup(symbol=sym, industry_group="AlphaTech", market="US"))
        session.add(StockUniverse(symbol=sym, market="US", sector=sector))
    session.commit()

    mapping = _service().get_group_sector_map(
        session, market="US"
    )
    assert mapping["AlphaTech"] == "Technology"  # 2 vs 1


def test_get_rrg_non_us_uses_feature_run_history_provider():
    session = _session()
    dates = _weekly_fridays(40)

    class _FakeHistoryProvider:
        def get_all_groups_history(self, db, *, market, days, as_of_date=None):  # noqa: ARG002
            assert market == "HK"
            assert as_of_date is None
            latest = dates[-1].isoformat()
            return (
                latest,
                {
                    "Internet Services": {
                        "industry_group": "Internet Services",
                        "date": latest,
                        "rank": 1,
                        "num_stocks": 9,
                        "avg_rs_rating": 80.0,
                    }
                },
                {
                    "Internet Services": [
                        (d, 40.0 + index, 9)
                        for index, d in enumerate(dates)
                    ]
                },
            )

    payload = RRGService(history_provider=_FakeHistoryProvider()).get_rrg(
        session, market="HK"
    )

    assert payload["market"] == "HK"
    assert [g["industry_group"] for g in payload["groups"]] == ["Internet Services"]


def test_get_rrg_uses_injected_history_provider_for_all_markets():
    session = _session()
    dates = _weekly_fridays(40)
    calls: list[tuple[str, int]] = []

    class _FakeHistoryProvider:
        def get_all_groups_history(self, db, *, market, days, as_of_date=None):  # noqa: ARG002
            assert as_of_date is None
            calls.append((market, days))
            latest = dates[-1].isoformat()
            return (
                latest,
                {
                    "Internet Services": {
                        "industry_group": "Internet Services",
                        "date": latest,
                        "rank": 1,
                        "num_stocks": 9,
                        "avg_rs_rating": 80.0,
                    }
                },
                {
                    "Internet Services": [
                        (d, 40.0 + index, 9)
                        for index, d in enumerate(dates)
                    ]
                },
            )

    payload = RRGService(
        history_provider=_FakeHistoryProvider(),
    ).get_rrg(session, market="HK", lookback_days=123)

    assert calls == [("HK", 123)]
    assert [group["industry_group"] for group in payload["groups"]] == ["Internet Services"]


def test_get_rrg_as_of_date_caps_us_history_series():
    session = _session()
    dates = _weekly_fridays(41)
    expected_dates = dates[:-1]
    future_date = dates[-1]
    _seed_group(
        session,
        market="US",
        group="AlphaTech",
        dates=expected_dates,
        rs_fn=lambda i: 40 + 0.6 * i,
    )
    _seed_group(
        session,
        market="US",
        group="AlphaTech",
        dates=[future_date],
        rs_fn=lambda _i: 5.0,
    )

    expected_latest = expected_dates[-1]
    future_week = (future_date - timedelta(days=(future_date.weekday() + 1) % 7)).isoformat()

    payload = RRGService(
        history_provider=StoredGroupRankHistoryProvider(),
    ).get_rrg(session, market="US", as_of_date=expected_latest)

    alpha = payload["groups"][0]
    assert payload["date"] == expected_latest.isoformat()
    assert alpha["current"]["date"] != future_week
    assert all(point["date"] != future_week for point in alpha["tail"])


def test_stored_history_provider_ignores_invalid_dates_outside_repair_window():
    dates = _weekly_fridays(13, start=date(2025, 12, 5))
    invalid_old_date = date(2025, 7, 1)
    snapshots = {
        item: [
            {
                "industry_group": "Software",
                "date": item.isoformat(),
                "rank": 1,
                "avg_rs_rating": 60.0 + index,
                "num_stocks": 10,
            }
        ]
        for index, item in enumerate(dates)
    }

    class _Reader:
        @staticmethod
        def available_dates(*_args, **_kwargs):
            return (invalid_old_date, *dates)

        @staticmethod
        def inspect_window(*_args, **_kwargs):
            return GroupSnapshotWindowResult(
                snapshots=snapshots,
                errors={invalid_old_date: "wrong Market RS run"},
            )

    provider = StoredGroupRankHistoryProvider(snapshot_reader=_Reader())

    latest, meta, series = provider.get_all_groups_history(
        object(),
        market="US",
        days=400,
        as_of_date=dates[-1],
    )

    assert latest == dates[-1].isoformat()
    assert meta["Software"]["rank"] == 1
    assert len(series["Software"]) == 13


def test_get_rrg_rejects_unknown_scope_at_service_boundary():
    session = _session()

    class _FakeHistoryProvider:
        def get_all_groups_history(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("unknown scopes should fail before loading history")

    with pytest.raises(ValueError, match="Unsupported RRG scope"):
        RRGService(history_provider=_FakeHistoryProvider()).get_rrg(
            session,
            market="US",
            scope="bogus",
        )


def test_get_rrg_disabled_non_us_market_returns_empty_without_history_lookup():
    session = _session()

    class _FakeHistoryProvider:
        def get_all_groups_history(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("disabled RRG markets should not load history")

    payload = RRGService(history_provider=_FakeHistoryProvider()).get_rrg_scopes(
        session, market="KR", scopes=("groups", "sectors")
    )

    assert payload["groups"]["groups"] == []
    assert payload["sectors"]["groups"] == []


def test_get_group_sector_map_uses_taxonomy_for_curated_rrg_market():
    session = _session()

    class _FakeTaxonomyService:
        def sector_map_for_market(self, market):
            assert market == "HK"
            return {"Internet Services": "Information Technology"}

    mapping = RRGService(
        history_provider=StoredGroupRankHistoryProvider(),
        taxonomy_service=_FakeTaxonomyService(),
    ).get_group_sector_map(session, market="HK")

    assert mapping == {"Internet Services": "Information Technology"}


def test_non_us_sector_map_does_not_fall_back_to_us_stock_universe_join():
    session = _session()
    session.add(
        IBDIndustryGroup(
            symbol="0700.HK",
            industry_group="Internet Services",
            market="HK",
        )
    )
    session.add(StockUniverse(symbol="0700.HK", market="HK", sector="Technology"))
    session.commit()

    class _EmptyTaxonomyService:
        def sector_map_for_market(self, market):
            assert market == "HK"
            return {}

    mapping = RRGService(
        history_provider=StoredGroupRankHistoryProvider(),
        taxonomy_service=_EmptyTaxonomyService(),
    ).get_group_sector_map(session, market="HK")

    assert mapping == {}
