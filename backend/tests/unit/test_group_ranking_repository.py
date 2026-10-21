from datetime import date

from app.models.industry import IBDGroupRank
from app.services.group_ranking_repository import (
    GroupRankingRepository,
)


def _seed_rank(
    db_session,
    *,
    market: str,
    calculation_date: date,
    group: str = "Software",
    rank: int = 1,
) -> None:
    db_session.add(
        IBDGroupRank(
            market=market,
            date=calculation_date,
            industry_group=group,
            rank=rank,
            avg_rs_rating=80.0,
            median_rs_rating=80.0,
            weighted_avg_rs_rating=80.0,
            rs_std_dev=0.0,
            num_stocks=3,
            num_stocks_rs_above_80=1,
            top_symbol="AAA",
            top_rs_rating=90.0,
        )
    )


def test_current_rank_rows_select_latest_or_explicit_market_date(
    db_session,
):
    _seed_rank(
        db_session,
        market="US",
        calculation_date=date(2026, 3, 19),
        rank=5,
    )
    _seed_rank(
        db_session,
        market="US",
        calculation_date=date(2026, 3, 20),
        rank=1,
    )
    _seed_rank(
        db_session,
        market="JP",
        calculation_date=date(2026, 3, 21),
        rank=1,
    )
    db_session.commit()
    repository = GroupRankingRepository()

    latest = repository.current_rank_rows(
        db_session,
        limit=10,
        market="US",
        calculation_date=None,
    )
    explicit = repository.current_rank_rows(
        db_session,
        limit=10,
        market="US",
        calculation_date=date(2026, 3, 19),
    )

    assert [row.date for row in latest] == [date(2026, 3, 20)]
    assert [row.rank for row in explicit] == [5]


def test_existing_dates_is_market_and_range_scoped(db_session):
    for market, calculation_date in (
        ("US", date(2026, 3, 18)),
        ("US", date(2026, 3, 20)),
        ("US", date(2026, 3, 22)),
        ("JP", date(2026, 3, 20)),
    ):
        _seed_rank(
            db_session,
            market=market,
            calculation_date=calculation_date,
        )
    db_session.commit()

    result = GroupRankingRepository().existing_dates(
        db_session,
        start_date=date(2026, 3, 19),
        end_date=date(2026, 3, 21),
        market="US",
    )

    assert result == frozenset({date(2026, 3, 20)})


def test_historical_ranks_batch_picks_closest_and_prefers_earlier_tie(
    db_session,
):
    current_date = date(2024, 1, 22)
    _seed_rank(
        db_session,
        market="US",
        calculation_date=date(2024, 1, 13),
        group="Software",
        rank=11,
    )
    _seed_rank(
        db_session,
        market="US",
        calculation_date=date(2024, 1, 17),
        group="Software",
        rank=99,
    )
    db_session.commit()

    result = GroupRankingRepository().historical_ranks_batch(
        db_session,
        group_names=("Software",),
        current_date=current_date,
        period_days={"1w": 7},
        market="US",
    )

    assert result[("Software", "1w")] == 11
