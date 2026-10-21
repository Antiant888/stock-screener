import inspect
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.domain.relative_strength import BALANCED_RS_FORMULA_VERSION
from app.models.industry import IBDGroupRank
from app.models.stock_universe import StockUniverse
from app.services import ibd_group_rank_service as group_rank_module
from app.services.group_rank_input_sources import StockUniverseGroupRankSource
from app.services.ibd_group_rank_service import IBDGroupRankService
from app.services.stock_universe_service import StockUniverseService


def _add_rank(session, group, rank_date, rank):
    session.add(
        IBDGroupRank(
            industry_group=group,
            date=rank_date,
            rank=rank,
            avg_rs_rating=50.0,
            num_stocks=10,
            num_stocks_rs_above_80=2,
            top_symbol="TEST",
            top_rs_rating=90.0,
        )
    )
    session.flush()


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[IBDGroupRank.__table__, StockUniverse.__table__],
    )
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)()


def _make_group_rank_service(group_constituent_source=None, calendar_service=None):
    return IBDGroupRankService(
        canonical_group_service=Mock(),
        market_rs_repository=Mock(),
        market_rs_snapshot_service=Mock(),
        universe_source=StockUniverseGroupRankSource(StockUniverseService()),
        calendar_service=calendar_service or Mock(),
        group_constituent_source=group_constituent_source,
    )


def _canonical_row(as_of: date) -> dict:
    return {
        "industry_group": "Software",
        "date": as_of,
        "rank": 1,
        "avg_rs_rating": 90.0,
        "avg_rs_rating_1m": 88.0,
        "avg_rs_rating_3m": 92.0,
        "median_rs_rating": 91.0,
        "weighted_avg_rs_rating": 90.5,
        "rs_std_dev": 2.0,
        "num_stocks": 3,
        "num_stocks_rs_above_80": 3,
        "top_symbol": "AAA",
        "top_rs_rating": 97.0,
        "rs_formula_version": BALANCED_RS_FORMULA_VERSION,
        "market_rs_run_id": 42,
    }


def test_calculation_publishes_the_market_rs_run_then_stores_group_averages():
    service = _make_group_rank_service()
    run = Mock(
        expected_symbol_count=4,
        eligible_symbol_count=3,
        excluded_symbol_count=1,
        benchmark_symbol="SPY",
    )
    service.market_rs_snapshot_service.calculate.return_value = run
    service.canonical_group_service.calculate_and_store.return_value = [
        _canonical_row(date(2026, 3, 20))
    ]
    db = Mock()

    result = service.calculate_group_rankings(db, date(2026, 3, 20), market="US")

    assert result.rankings[0].avg_rs_rating_1m == 88.0
    assert result.rankings[0].market_rs_run_id == 42
    assert result.prefetch_stats.symbols_with_prices == 3
    assert result.prefetch_stats.skipped_unsupported_symbols == 1
    service.market_rs_snapshot_service.calculate.assert_called_once_with(
        db,
        market="US",
        as_of_date=date(2026, 3, 20),
        formula_version=BALANCED_RS_FORMULA_VERSION,
        rebuild_incompatible=True,
    )
    service.canonical_group_service.calculate_and_store.assert_called_once_with(
        db,
        market="US",
        as_of_date=date(2026, 3, 20),
        formula_version=BALANCED_RS_FORMULA_VERSION,
    )


def test_get_group_history_uses_universe_lookup_for_top_symbol_name(monkeypatch):
    service = _make_group_rank_service()
    db_session = _make_session()
    group = f"TEST_GROUP_UNIT_{uuid4().hex}"
    current_date = date.today()

    try:
        db_session.add(
            IBDGroupRank(
                industry_group=group,
                date=current_date,
                rank=1,
                avg_rs_rating=92.0,
                median_rs_rating=91.0,
                weighted_avg_rs_rating=93.0,
                rs_std_dev=1.5,
                num_stocks=8,
                num_stocks_rs_above_80=6,
                top_symbol="AAPL",
                top_rs_rating=98.0,
            )
        )
        db_session.add(
            StockUniverse(
                symbol="AAPL",
                name="Apple Inc.",
                market="US",
                exchange="NASDAQ",
                is_active=True,
                status="active",
                status_reason="active",
            )
        )
        db_session.commit()

        monkeypatch.setattr(
            service.ranking_repository,
            "historical_ranks_batch",
            lambda *_args, **_kwargs: {},
        )
        monkeypatch.setattr(
            service,
            "_get_constituent_stocks",
            lambda *_args, **_kwargs: [{"symbol": "MSFT", "company_name": "Microsoft"}],
        )

        result = service.get_group_history(db_session, group, days=30)

        assert result["top_symbol"] == "AAPL"
        assert result["top_symbol_name"] == "Apple Inc."
        assert result["pct_rs_above_80"] == 75.0
    finally:
        db_session.rollback()
        db_session.close()


def test_get_group_history_uses_calendar_rank_change_offsets(monkeypatch):
    service = _make_group_rank_service()
    db_session = _make_session()
    group = f"TEST_GROUP_UNIT_{uuid4().hex}"
    current_date = date.today()
    captured_period_days: list[dict[str, int]] = []

    try:
        db_session.add(
            IBDGroupRank(
                market="US",
                industry_group=group,
                date=current_date,
                rank=4,
                avg_rs_rating=88.0,
                median_rs_rating=87.0,
                weighted_avg_rs_rating=89.0,
                rs_std_dev=2.0,
                num_stocks=5,
                num_stocks_rs_above_80=3,
                top_symbol="AAPL",
                top_rs_rating=96.0,
            )
        )
        db_session.commit()

        expected_period_days = dict(
            group_rank_module.CALENDAR_DAY_GROUP_RANK_CHANGE_WINDOWS
        )

        def fake_historical_batch(  # noqa: ANN001
            db,
            *,
            group_names,
            current_date,
            period_days,
            market,
        ):
            captured_period_days.append(dict(period_days))
            return {(group, "1w"): 6}

        monkeypatch.setattr(
            service.ranking_repository,
            "historical_ranks_batch",
            fake_historical_batch,
        )
        monkeypatch.setattr(service, "_get_constituent_stocks", lambda *_args, **_kwargs: [])

        result = service.get_group_history(db_session, group, days=30, market="US")

        assert captured_period_days == [expected_period_days]
        assert result["rank_change_1w"] == 2
        assert result["rank_change_1m"] is None
    finally:
        db_session.rollback()
        db_session.close()


def test_get_current_rankings_uses_calendar_rank_change_offsets(monkeypatch):
    service = _make_group_rank_service()
    db_session = _make_session()
    group = f"TEST_GROUP_UNIT_{uuid4().hex}"
    current_date = date(2026, 4, 18)
    captured_period_days: list[dict[str, int]] = []

    try:
        _add_rank(db_session, group, current_date, 4)
        db_session.commit()

        expected_period_days = dict(
            group_rank_module.CALENDAR_DAY_GROUP_RANK_CHANGE_WINDOWS
        )

        def fake_historical_batch(  # noqa: ANN001
            db,
            *,
            group_names,
            current_date,
            period_days,
            market,
        ):
            captured_period_days.append(dict(period_days))
            return {(group, "1w"): 6}

        monkeypatch.setattr(
            service.ranking_repository,
            "historical_ranks_batch",
            fake_historical_batch,
        )

        rankings = service.get_current_rankings(
            db_session,
            limit=10,
            calculation_date=current_date,
            market="US",
        )

        assert captured_period_days == [expected_period_days]
        assert rankings[0]["rank_change_1w"] == 2
        assert rankings[0]["rank_change_1m"] is None
        assert rankings[0]["pct_rs_above_80"] == 20.0
    finally:
        db_session.rollback()
        db_session.close()


def test_get_group_history_propagates_constituent_source_failures(monkeypatch):
    service = _make_group_rank_service(
        group_constituent_source=Mock(),
    )
    service.group_constituent_source.get_constituent_items.side_effect = RuntimeError(
        "source failed"
    )
    service.market_rs_repository.get_completed_exact.return_value = SimpleNamespace(
        id=7,
        eligible_symbol_count=10,
    )
    db_session = _make_session()
    group = f"TEST_GROUP_UNIT_{uuid4().hex}"
    current_date = date.today()

    try:
        db_session.add(
            IBDGroupRank(
                market="US",
                industry_group=group,
                date=current_date,
                rank=1,
                avg_rs_rating=91.0,
                num_stocks=1,
                num_stocks_rs_above_80=1,
                top_symbol="AAPL",
                top_rs_rating=96.0,
                market_rs_run_id=7,
            )
        )
        db_session.commit()
        monkeypatch.setattr(
            service.ranking_repository,
            "historical_ranks_batch",
            lambda *_args, **_kwargs: {},
        )

        with pytest.raises(RuntimeError, match="source failed"):
            service.get_group_history(db_session, group, days=30, market="US")
    finally:
        db_session.rollback()
        db_session.close()


def test_calculate_group_rankings_has_no_diagnostics_output_parameter():
    signature = inspect.signature(
        IBDGroupRankService.calculate_group_rankings
    )

    assert "diagnostics" not in signature.parameters


def test_group_rank_service_has_no_runtime_wiring_or_raw_sql():
    source = Path(group_rank_module.__file__).read_text()
    assert "gc.collect" not in source
    assert "get_market_calendar_service" not in source
    assert "pg_insert" not in source
    assert "StockUniverse" not in source

    for method_name in (
        "calculate_group_rankings",
        "get_current_rankings",
        "get_historical_ranks_batch",
        "get_group_history",
        "get_rank_movers",
        "backfill_rankings_optimized",
        "find_missing_dates",
        "fill_gaps_optimized",
    ):
        assert hasattr(IBDGroupRankService, method_name)


def test_get_current_rankings_can_target_explicit_date():
    service = _make_group_rank_service()
    db_session = _make_session()
    group = f"TEST_GROUP_UNIT_{uuid4().hex}"

    try:
        _add_rank(db_session, group, date(2024, 1, 10), 5)
        _add_rank(db_session, group, date(2024, 1, 17), 3)
        db_session.commit()

        rankings = service.get_current_rankings(
            db_session,
            limit=10,
            calculation_date=date(2024, 1, 10),
        )

        assert len(rankings) == 1
        assert rankings[0]["date"] == "2024-01-10"
        assert rankings[0]["rank"] == 5
    finally:
        db_session.rollback()
        db_session.close()


def test_get_current_rankings_ignores_rows_stored_under_another_formula():
    service = _make_group_rank_service()
    db_session = _make_session()
    group = f"TEST_GROUP_UNIT_{uuid4().hex}"

    try:
        db_session.add(
            IBDGroupRank(
                industry_group=group,
                date=date(2024, 1, 17),
                rank=1,
                avg_rs_rating=50.0,
                num_stocks=10,
                rs_formula_version="legacy-linear-v1",
            )
        )
        _add_rank(db_session, group, date(2024, 1, 10), 5)
        db_session.commit()

        rankings = service.get_current_rankings(db_session, limit=10)

        assert [row["date"] for row in rankings] == ["2024-01-10"]
    finally:
        db_session.rollback()
        db_session.close()


def test_get_rank_movers_filters_gainers_and_losers_by_sign(monkeypatch):
    service = _make_group_rank_service()
    rankings = [
        {"industry_group": "Up Big", "rank_change_1w": 8},
        {"industry_group": "Up Small", "rank_change_1w": 1},
        {"industry_group": "Flat", "rank_change_1w": 0},
        {"industry_group": "Down Small", "rank_change_1w": -1},
        {"industry_group": "Down Big", "rank_change_1w": -7},
    ]
    monkeypatch.setattr(
        service,
        "get_current_rankings",
        lambda *args, **kwargs: rankings,
    )

    movers = service.get_rank_movers(Mock(), period="1w", limit=10, market="HK")

    assert [g["industry_group"] for g in movers["gainers"]] == ["Up Big", "Up Small"]
    assert [row["industry_group"] for row in movers["losers"]] == [
        "Down Big",
        "Down Small",
    ]


def test_get_rank_movers_omits_losers_when_only_gainers_exist(monkeypatch):
    service = _make_group_rank_service()
    rankings = [
        {"industry_group": "A", "rank_change_1w": 5},
        {"industry_group": "B", "rank_change_1w": 2},
        {"industry_group": "C", "rank_change_1w": 1},
    ]
    monkeypatch.setattr(
        service,
        "get_current_rankings",
        lambda *args, **kwargs: rankings,
    )

    movers = service.get_rank_movers(Mock(), period="1w", limit=10, market="HK")

    assert [g["industry_group"] for g in movers["gainers"]] == ["A", "B", "C"]
    assert movers["losers"] == []


def test_find_missing_dates_reads_existing_dates_from_repository():
    calendar = Mock()
    calendar.is_trading_day.return_value = True
    service = _make_group_rank_service(calendar_service=calendar)
    service.ranking_repository = Mock()
    service.ranking_repository.existing_dates.return_value = frozenset(
        {date(2026, 3, 19)}
    )

    result = service.find_missing_dates(
        Mock(),
        lookback_days=2,
        market="US",
        end_date=date(2026, 3, 20),
    )

    assert result == [date(2026, 3, 18)]
    assert service.ranking_repository.existing_dates.call_args.kwargs == {
        "start_date": date(2026, 3, 18),
        "end_date": date(2026, 3, 20),
        "market": "US",
    }
    assert calendar.is_trading_day.call_count == 2


def test_backfill_rankings_optimized_fills_each_trading_day(monkeypatch):
    calendar = Mock()
    calendar.is_trading_day.side_effect = lambda _market, day: day.weekday() < 5
    service = _make_group_rank_service(calendar_service=calendar)
    calculated: list[date] = []

    def fake_calculate(db, calculation_date, **kwargs):
        calculated.append(calculation_date)
        return SimpleNamespace(rankings=(object(),))

    monkeypatch.setattr(service, "calculate_group_rankings", fake_calculate)

    result = service.backfill_rankings_optimized(
        Mock(), date(2026, 3, 19), date(2026, 3, 23), market="US"
    )

    assert calculated == [date(2026, 3, 19), date(2026, 3, 20), date(2026, 3, 23)]
    assert result == {
        "start_date": "2026-03-19",
        "end_date": "2026-03-23",
        "deleted": 0,
        "total_dates": 3,
        "processed": 3,
        "skipped": 0,
        "errors": 0,
    }
