from __future__ import annotations

import gzip
import json
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
from app.models.industry import IBDGroupRank
from app.services.rrg_service import DEFAULT_LOOKBACK_DAYS, MIN_TAIL_WEEKS, RRGService
from app.services.static_rrg_history_bundle import (
    StaticRRGHistoryBundleService,
    StaticRRGHistoryProvider,
)
from app.services.static_rrg_history_contract import (
    STATIC_RRG_HISTORY_SCHEMA_VERSION,
    StaticRRGGroupPoint,
    StaticRRGHistoryBundleError,
    StaticRRGHistoryState,
    StaticRRGWeek,
)


# Rows the database still holds from before the single-formula cutover.
STORED_LEGACY_FORMULA = "legacy-linear-v1"


def test_static_rrg_history_readiness_requires_min_tail_weeks():
    service = StaticRRGHistoryBundleService()
    group = StaticRRGGroupPoint(
        industry_group="Software",
        rank=1,
        avg_rs_rating=80.0,
        num_stocks=5,
    )

    short = StaticRRGHistoryState(
        schema_version=STATIC_RRG_HISTORY_SCHEMA_VERSION,
        market="US",
        rs_formula_version=BALANCED_RS_FORMULA_VERSION,
        weeks=tuple(
            StaticRRGWeek(
                source_date=date(2026, 1, 2) + timedelta(weeks=day),
                groups=(group,),
            )
            for day in range(MIN_TAIL_WEEKS - 1)
        ),
    )
    enough = short.model_copy(
        update={
            "weeks": short.weeks
            + (
                StaticRRGWeek(
                    source_date=date(2026, 1, 2)
                    + timedelta(weeks=MIN_TAIL_WEEKS - 1),
                    groups=(group,),
                ),
            )
        }
    )

    assert service.has_minimum_history(None) is False
    assert service.has_minimum_history(short) is False
    assert service.has_minimum_history(enough) is True


def test_static_rrg_history_readiness_counts_only_provider_usable_weeks():
    service = StaticRRGHistoryBundleService()
    group = StaticRRGGroupPoint(
        industry_group="Software",
        rank=1,
        avg_rs_rating=80.0,
        num_stocks=5,
    )
    through_date = date(2026, 7, 24)
    old_week = StaticRRGWeek(
        source_date=through_date - timedelta(days=DEFAULT_LOOKBACK_DAYS + 1),
        groups=(group,),
    )
    recent_weeks = tuple(
        StaticRRGWeek(
            source_date=through_date - timedelta(weeks=MIN_TAIL_WEEKS - index - 2),
            groups=(group,),
        )
        for index in range(MIN_TAIL_WEEKS - 1)
    )
    sparse = StaticRRGHistoryState(
        schema_version=STATIC_RRG_HISTORY_SCHEMA_VERSION,
        market="US",
        rs_formula_version=BALANCED_RS_FORMULA_VERSION,
        weeks=(old_week,) + recent_weeks,
    )

    assert len(sparse.weeks) == MIN_TAIL_WEEKS
    assert service.has_minimum_history(sparse) is False


def _session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[IBDGroupRank.__table__, MarketRsRun.__table__],
    )
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _rank(
    *,
    market: str,
    group: str,
    row_date: date,
    rank: int,
    avg_rs: float,
    formula_version: str = BALANCED_RS_FORMULA_VERSION,
    market_rs_run_id: int | None = None,
):
    return IBDGroupRank(
        market=market,
        industry_group=group,
        date=row_date,
        rank=rank,
        avg_rs_rating=avg_rs,
        median_rs_rating=avg_rs - 1,
        num_stocks=12,
        num_stocks_rs_above_80=3,
        top_symbol="AAA",
        top_rs_rating=95,
        rs_formula_version=formula_version,
        market_rs_run_id=market_rs_run_id,
    )


def _balanced_run(db, *, market: str, row_date: date) -> int:
    run = MarketRsRun(
        market=market,
        as_of_date=row_date,
        formula_version=BALANCED_RS_FORMULA_VERSION,
        status="completed",
        benchmark_symbol="^HSI",
        benchmark_as_of_date=row_date,
        universe_hash=f"balanced-{row_date.isoformat()}",
        expected_symbol_count=12,
        eligible_symbol_count=12,
        excluded_symbol_count=0,
        diagnostics_json={
            "price_basis": BALANCED_RS_PRICE_BASIS,
            "rs_snapshot_schema_version": BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
        },
    )
    db.add(run)
    db.flush()
    return run.id


def _seed_weeks(
    db,
    *,
    latest: date,
    weeks: int,
    market: str = "HK",
) -> None:
    for week in range(weeks):
        row_date = latest - timedelta(weeks=weeks - week - 1)
        run_id = _balanced_run(db, market=market, row_date=row_date)
        db.add_all(
            [
                _rank(
                    market=market,
                    group="Semiconductors",
                    row_date=row_date,
                    rank=1,
                    avg_rs=60 + week * 1.5,
                    market_rs_run_id=run_id,
                ),
                _rank(
                    market=market,
                    group="Banks",
                    row_date=row_date,
                    rank=2,
                    avg_rs=85 - week,
                    market_rs_run_id=run_id,
                ),
            ]
        )
    db.commit()


def test_weekly_state_round_trips_only_rrg_inputs(tmp_path):
    engine, factory = _session_factory()
    latest = date(2026, 7, 10)
    path = tmp_path / "rrg-history-hk.json.gz"
    try:
        with factory() as db:
            _seed_weeks(db, latest=latest, weeks=14)
            state = StaticRRGHistoryBundleService().build(
                db,
                market="HK",
                through_date=latest,
                formula_version=BALANCED_RS_FORMULA_VERSION,
            )
        stats = StaticRRGHistoryBundleService().write(state, path)
        restored = StaticRRGHistoryBundleService().load(path, expected_market="HK")

        assert stats["weeks"] == 14
        assert restored == state
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        assert payload["schema_version"] == STATIC_RRG_HISTORY_SCHEMA_VERSION
        assert payload["schema_version"] == "static-rrg-history-v4"
        assert payload["rs_formula_version"] == BALANCED_RS_FORMULA_VERSION
        assert set(payload) == {
            "schema_version",
            "market",
            "rs_formula_version",
            "weeks",
        }
        assert set(payload["weeks"][0]) == {"source_date", "groups"}
        assert set(payload["weeks"][0]["groups"][0]) == {
            "industry_group",
            "rank",
            "avg_rs_rating",
            "num_stocks",
        }
    finally:
        engine.dispose()


def test_merge_keeps_prior_weeks_without_mutating_database(tmp_path):
    source_engine, source_factory = _session_factory()
    target_engine, target_factory = _session_factory()
    latest = date(2026, 7, 10)
    try:
        with source_factory() as db:
            _seed_weeks(db, latest=latest - timedelta(weeks=1), weeks=13)
            previous = StaticRRGHistoryBundleService().build(
                db,
                market="HK",
                through_date=latest - timedelta(weeks=1),
                formula_version=BALANCED_RS_FORMULA_VERSION,
            )
        with target_factory() as db:
            run_id = _balanced_run(db, market="HK", row_date=latest)
            db.add(_rank(market="HK", group="Semiconductors", row_date=latest, rank=1, avg_rs=91, market_rs_run_id=run_id))
            db.commit()
            merged = StaticRRGHistoryBundleService().build(
                db,
                market="HK",
                through_date=latest,
                previous=previous,
                formula_version=BALANCED_RS_FORMULA_VERSION,
            )
            database_groups = [row.industry_group for row in db.query(IBDGroupRank).all()]

        assert len(merged.weeks) == 14
        assert database_groups == ["Semiconductors"]
        assert merged.weeks[-2] == previous.weeks[-1]
    finally:
        source_engine.dispose()
        target_engine.dispose()


def test_merge_discards_restored_weeks_after_requested_export_date():
    source_engine, source_factory = _session_factory()
    target_engine, target_factory = _session_factory()
    latest = date(2026, 7, 10)
    future = latest + timedelta(weeks=1)
    try:
        with source_factory() as db:
            _seed_weeks(db, latest=future, weeks=14)
            previous = StaticRRGHistoryBundleService().build(
                db,
                market="HK",
                through_date=future,
                formula_version=BALANCED_RS_FORMULA_VERSION,
            )
        with target_factory() as db:
            run_id = _balanced_run(db, market="HK", row_date=latest)
            db.add(
                _rank(
                    market="HK",
                    group="Semiconductors",
                    row_date=latest,
                    rank=1,
                    avg_rs=91,
                    market_rs_run_id=run_id,
                )
            )
            db.commit()
            merged = StaticRRGHistoryBundleService().build(
                db,
                market="HK",
                through_date=latest,
                previous=previous,
                formula_version=BALANCED_RS_FORMULA_VERSION,
            )

        assert merged.weeks[-1].source_date == latest
        assert all(week.source_date <= latest for week in merged.weeks)
    finally:
        source_engine.dispose()
        target_engine.dispose()


def test_merge_rejects_a_previous_bundle_from_another_formula():
    source_engine, source_factory = _session_factory()
    target_engine, target_factory = _session_factory()
    latest = date(2026, 7, 10)
    try:
        with source_factory() as db:
            _seed_weeks(db, latest=latest - timedelta(weeks=1), weeks=13)
            previous = StaticRRGHistoryBundleService().build(
                db,
                market="HK",
                through_date=latest - timedelta(weeks=1),
                formula_version=BALANCED_RS_FORMULA_VERSION,
            ).model_copy(update={"rs_formula_version": STORED_LEGACY_FORMULA})
        with target_factory() as db:
            run_id = _balanced_run(db, market="HK", row_date=latest)
            db.add(
                _rank(
                    market="HK",
                    group="Balanced Only",
                    row_date=latest,
                    rank=1,
                    avg_rs=91,
                    market_rs_run_id=run_id,
                )
            )
            db.commit()
            with pytest.raises(StaticRRGHistoryBundleError, match="formula"):
                StaticRRGHistoryBundleService().build(
                    db,
                    market="HK",
                    through_date=latest,
                    previous=previous,
                    formula_version=BALANCED_RS_FORMULA_VERSION,
                )
    finally:
        source_engine.dispose()
        target_engine.dispose()


def test_prepare_rebuilds_when_prior_bundle_uses_another_formula(tmp_path):
    source_engine, source_factory = _session_factory()
    target_engine, target_factory = _session_factory()
    latest = date(2026, 7, 10)
    service = StaticRRGHistoryBundleService()
    try:
        with source_factory() as db:
            _seed_weeks(db, latest=latest - timedelta(weeks=1), weeks=13)
            previous = service.build(
                db,
                market="HK",
                through_date=latest - timedelta(weeks=1),
                formula_version=BALANCED_RS_FORMULA_VERSION,
            ).model_copy(update={"rs_formula_version": STORED_LEGACY_FORMULA})
        service.write(previous, tmp_path / "rrg-history-hk.json.gz")

        with target_factory() as db:
            for week in range(14):
                row_date = latest - timedelta(weeks=13 - week)
                run_id = 100 + week
                db.add(
                    MarketRsRun(
                        id=run_id,
                        market="HK",
                        as_of_date=row_date,
                        formula_version=BALANCED_RS_FORMULA_VERSION,
                        status="completed",
                        benchmark_symbol="^HSI",
                        benchmark_as_of_date=row_date,
                        universe_hash=f"balanced-{week}",
                        expected_symbol_count=12,
                        eligible_symbol_count=12,
                        excluded_symbol_count=0,
                        diagnostics_json={
                            "price_basis": BALANCED_RS_PRICE_BASIS,
                            "rs_snapshot_schema_version": (
                                BALANCED_RS_SNAPSHOT_SCHEMA_VERSION
                            ),
                        },
                    )
                )
                db.add(
                    _rank(
                        market="HK",
                        group="Balanced Only",
                        row_date=row_date,
                        rank=1,
                        avg_rs=70 + week,
                        formula_version=BALANCED_RS_FORMULA_VERSION,
                        market_rs_run_id=run_id,
                    )
                )
            db.commit()
            preparation = service.prepare(
                db,
                market="HK",
                through_date=latest,
                directory=tmp_path,
                formula_version=BALANCED_RS_FORMULA_VERSION,
            )

        assert preparation.state is not None
        assert preparation.state.rs_formula_version == BALANCED_RS_FORMULA_VERSION
        assert preparation.warnings
        assert "formula" in preparation.warnings[0].lower()
        assert {
            group.industry_group
            for week in preparation.state.weeks
            for group in week.groups
        } == {"Balanced Only"}
    finally:
        source_engine.dispose()
        target_engine.dispose()


def test_invalid_row_is_normalized_to_bundle_error(tmp_path):
    path = tmp_path / "rrg-history-hk.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(
            {
                "schema_version": STATIC_RRG_HISTORY_SCHEMA_VERSION,
                "market": "HK",
                "rs_formula_version": STORED_LEGACY_FORMULA,
                "weeks": [
                    {
                        "source_date": "2026-07-10",
                        "groups": [
                            {
                                "industry_group": "Semiconductors",
                                "rank": "bad",
                                "avg_rs_rating": 88,
                                "num_stocks": 12,
                            }
                        ],
                    }
                ],
            },
            handle,
        )

    with pytest.raises(StaticRRGHistoryBundleError, match="Unable to load"):
        StaticRRGHistoryBundleService().load(path, expected_market="HK")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.pop("schema_version"),
        lambda payload: payload.update({"schema_version": "static-rrg-history-v2"}),
        lambda payload: payload.update({"unexpected": True}),
        lambda payload: payload["weeks"][0].update({"unexpected": True}),
        lambda payload: payload["weeks"][0]["groups"][0].update({"unexpected": True}),
        lambda payload: payload["weeks"][0]["groups"][0].update({"rank": "1"}),
        lambda payload: payload["weeks"][0]["groups"][0].update(
            {"avg_rs_rating": "80.0"}
        ),
        lambda payload: payload["weeks"][0]["groups"][0].update(
            {"num_stocks": "10"}
        ),
        lambda payload: payload["weeks"][0]["groups"][0].update(
            {"avg_rs_rating": float("nan")}
        ),
    ],
)
def test_history_contract_rejects_unversioned_and_unknown_fields(tmp_path, mutation):
    path = tmp_path / "rrg-history-hk.json.gz"
    payload = {
        "schema_version": STATIC_RRG_HISTORY_SCHEMA_VERSION,
        "market": "HK",
        "rs_formula_version": STORED_LEGACY_FORMULA,
        "weeks": [
            {
                "source_date": "2026-07-10",
                "groups": [
                    {
                        "industry_group": "Banks",
                        "rank": 1,
                        "avg_rs_rating": 80,
                        "num_stocks": 10,
                    }
                ],
            }
        ],
    }
    mutation(payload)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)

    with pytest.raises(StaticRRGHistoryBundleError, match="Unable to load"):
        StaticRRGHistoryBundleService().load(path, expected_market="HK")


def test_prepare_bootstraps_invalid_prior_state_and_persists_current_state(tmp_path):
    engine, factory = _session_factory()
    latest = date(2026, 7, 10)
    path = tmp_path / "rrg-history-hk.json.gz"
    path.write_bytes(b"not gzip")
    try:
        with factory() as db:
            _seed_weeks(db, latest=latest, weeks=14)
            preparation = StaticRRGHistoryBundleService().prepare(
                db,
                market="HK",
                through_date=latest,
                directory=tmp_path,
                formula_version=BALANCED_RS_FORMULA_VERSION,
            )

        assert preparation.state is not None
        assert preparation.warnings
        assert path.read_bytes() == b"not gzip"
        stats = StaticRRGHistoryBundleService().persist(
            preparation,
            exported_as_of_date=latest,
        )
        assert stats is not None
        assert stats["weeks"] == 14
        assert preparation.plan.output_path.exists()
    finally:
        engine.dispose()


def test_weekly_state_computes_non_us_group_and_sector_rrg():
    engine, factory = _session_factory()
    latest = date(2026, 7, 10)

    class _Taxonomy:
        def sector_map_for_market(self, market):
            assert market == "HK"
            return {"Semiconductors": "Technology", "Banks": "Financials"}

    try:
        with factory() as db:
            _seed_weeks(db, latest=latest, weeks=14)
            state = StaticRRGHistoryBundleService().build(
                db,
                market="HK",
                through_date=latest,
                formula_version=BALANCED_RS_FORMULA_VERSION,
            )
            payload = RRGService(
                history_provider=StaticRRGHistoryProvider(state),
                taxonomy_service=_Taxonomy(),
            ).get_rrg_scopes(
                db,
                market="HK",
                scopes=("groups", "sectors"),
                as_of_date=latest,
            )

        assert len(payload["groups"]["groups"]) == 2
        assert {row["industry_group"] for row in payload["sectors"]["groups"]} == {
            "Technology",
            "Financials",
        }
    finally:
        engine.dispose()
