from datetime import date, datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.domain.relative_strength import (
    BALANCED_RS_FORMULA_VERSION,
    BALANCED_RS_PRICE_BASIS,
    BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
)
from app.domain.scanning.ports import CanonicalMarketRsSource, CanonicalStockRsSource
from app.infra.db.models.relative_strength import (
    MarketRsRun,
    StockRsPlacementSnapshot,
    StockRsSnapshot,
)
from app.services.market_rs_reader import (
    CanonicalMarketRsUnavailable,
    SqlMarketRsReader,
)

AS_OF = date(2026, 4, 10)


def _snapshot(
    symbol,
    *,
    overall,
    one_day=50,
    one_week=50,
    one_month,
    three_month,
    six_month=50,
    twelve_month,
):
    return StockRsSnapshot(
        symbol=symbol,
        overall_rs=overall,
        rs_1d=one_day,
        rs_1w=one_week,
        rs_1m=one_month,
        rs_3m=three_month,
        rs_6m=six_month,
        rs_9m=50,
        rs_12m=twelve_month,
        weighted_composite=float(overall),
        excess_return_1d=0.1,
        excess_return_1w=0.1,
        excess_return_1m=0.1,
        excess_return_3m=0.1,
        excess_return_6m=0.1,
        excess_return_9m=0.1,
        excess_return_12m=0.1,
    )


def _seed_balanced_run(db_session):
    run = MarketRsRun(
        market="US",
        as_of_date=AS_OF,
        formula_version=BALANCED_RS_FORMULA_VERSION,
        status="completed",
        benchmark_symbol="SPY",
        benchmark_as_of_date=AS_OF,
        universe_hash="a" * 64,
        expected_symbol_count=3,
        eligible_symbol_count=3,
        excluded_symbol_count=0,
        diagnostics_json={
            "price_basis": BALANCED_RS_PRICE_BASIS,
            "rs_snapshot_schema_version": BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
        },
        completed_at=datetime.now(timezone.utc),
        rows=[
            _snapshot("AAA", overall=99, one_month=50, three_month=75, twelve_month=99),
            _snapshot("BBB", overall=50, one_month=25, three_month=50, twelve_month=75),
            _snapshot("CCC", overall=1, one_month=1, three_month=1, twelve_month=1),
        ],
    )
    db_session.add(run)
    db_session.commit()
    return run


def _reader(db_session):
    return SqlMarketRsReader(sessionmaker(bind=db_session.get_bind()))


def test_reader_returns_exact_market_snapshot_without_repercentiling_requested_set(
    db_session,
):
    run = _seed_balanced_run(db_session)
    reader = _reader(db_session)

    resolution = reader.get(
        market="US",
        symbols=("AAA", "CCC"),
        as_of_date=AS_OF,
    )

    assert resolution.formula_version == BALANCED_RS_FORMULA_VERSION
    assert isinstance(resolution.source, CanonicalMarketRsSource)
    assert resolution.run_id == run.id
    assert resolution.universe_size == 3
    assert resolution.ratings_by_symbol["AAA"] == {
        "rs_rating": 99,
        "rs_rating_1m": 50,
        "rs_rating_3m": 75,
        "rs_rating_12m": 99,
    }
    assert "BBB" not in resolution.ratings_by_symbol

    watchlist_resolution = reader.get(
        market="US",
        symbols=("AAA",),
        as_of_date=AS_OF,
    )
    assert (
        watchlist_resolution.ratings_by_symbol["AAA"]
        == resolution.ratings_by_symbol["AAA"]
    )


def test_reader_returns_exact_run_id_without_relying_on_active_date(db_session):
    run = _seed_balanced_run(db_session)

    resolution = _reader(db_session).get(
        market="US",
        symbols=("AAA",),
        as_of_date=None,
        formula_version=BALANCED_RS_FORMULA_VERSION,
        run_id=run.id,
    )

    assert resolution.run_id == run.id
    assert resolution.as_of_date == AS_OF


def test_reader_fails_closed_when_balanced_exact_run_is_missing(db_session):

    with pytest.raises(CanonicalMarketRsUnavailable, match="2026-04-10"):
        _reader(db_session).get(market="US", symbols=("AAA",), as_of_date=AS_OF)


def _placement_row(symbol, **ratings):
    return StockRsPlacementSnapshot(
        symbol=symbol,
        **{f"rs_{horizon}": rating for horizon, rating in ratings.items()},
        **{f"excess_return_{horizon}": 0.05 for horizon in ratings},
    )


def test_reader_delivers_placed_ratings_separately_from_eligible_ratings(db_session):
    run = _seed_balanced_run(db_session)
    run.placed_symbol_count = 2
    run.placements.extend(
        [
            _placement_row("YNG", **{"1d": 99, "1w": 99, "1m": 66, "3m": 1}),
            _placement_row("TINY", **{"1d": 40}),
        ]
    )
    db_session.commit()

    resolution = _reader(db_session).get(
        market="US",
        symbols=("AAA", "YNG", "TINY", "GAP"),
        as_of_date=AS_OF,
    )

    assert resolution.universe_size == 3
    assert resolution.ratings_by_symbol == {
        "AAA": {"rs_rating": 99, "rs_rating_1m": 50, "rs_rating_3m": 75, "rs_rating_12m": 99}
    }
    assert resolution.source.placed_ratings_by_symbol == {
        "YNG": {"rs_rating_1m": 66, "rs_rating_3m": 1},
        "TINY": {},
    }
    young = resolution.stock_source("YNG")
    assert young.ratings is None
    assert young.placed_ratings == {"rs_rating_1m": 66, "rs_rating_3m": 1}
    assert young.scan_row_fields() == {
        "rs_rating": None,
        "rs_rating_1m": 66,
        "rs_rating_3m": 1,
        "rs_rating_12m": None,
    }
    assert resolution.stock_source("TINY").placed_ratings == {}
    assert resolution.stock_source("AAA").scan_row_fields() == {
        "rs_rating": 99,
        "rs_rating_1m": 50,
        "rs_rating_3m": 75,
        "rs_rating_12m": 99,
    }
    gap = resolution.stock_source("GAP")
    assert (gap.ratings, gap.placed_ratings) == (None, None)


def test_reader_treats_a_run_stored_before_placement_exactly_as_today(db_session):
    run = _seed_balanced_run(db_session)
    assert run.placed_symbol_count is None

    resolution = _reader(db_session).get(
        market="US",
        symbols=("AAA", "YNG"),
        as_of_date=AS_OF,
    )

    assert resolution.universe_size == 3
    assert set(resolution.ratings_by_symbol) == {"AAA"}
    assert resolution.source.placed_ratings_by_symbol == {}
    young = resolution.stock_source("YNG")
    assert (young.ratings, young.placed_ratings) == (None, None)
    assert young.scan_row_fields() == {
        "rs_rating": None,
        "rs_rating_1m": None,
        "rs_rating_3m": None,
        "rs_rating_12m": None,
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ratings": {"rs_rating_1m": 5}},
        {"ratings": {"rs_rating": 80}, "placed_ratings": {"rs_rating_1m": 5}},
        {"ratings": None, "placed_ratings": {"rs_rating": 50}},
    ],
)
def test_stock_source_rejects_ratings_without_an_overall_or_mixed_states(kwargs):
    with pytest.raises(ValueError):
        CanonicalStockRsSource(
            formula_version=BALANCED_RS_FORMULA_VERSION,
            run_id=42,
            universe_size=10,
            **kwargs,
        )


def test_market_source_rejects_a_symbol_that_is_both_eligible_and_placed():
    with pytest.raises(ValueError, match="YNG"):
        CanonicalMarketRsSource(
            formula_version=BALANCED_RS_FORMULA_VERSION,
            run_id=42,
            universe_size=10,
            ratings_by_symbol={"YNG": {"rs_rating": 80}},
            placed_ratings_by_symbol={"yng": {"rs_rating_1m": 5}},
        )
