"""Query operations for stored balanced group-ranking rows."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Mapping, Sequence

from sqlalchemy import and_, desc
from sqlalchemy.orm import Session

from app.domain.relative_strength import BALANCED_RS_FORMULA_VERSION

from ..models.industry import IBDGroupRank
from .group_rank_history_policy import (
    CALENDAR_DAY_GROUP_RANK_LOOKUP_TOLERANCE_DAYS,
)


class GroupRankingRepository:
    def current_rank_rows(
        self,
        db: Session,
        *,
        limit: int,
        market: str,
        calculation_date: date | None,
    ) -> list[IBDGroupRank]:
        normalized_market = (market or "US").upper()
        if calculation_date is not None:
            latest_date = calculation_date
        else:
            latest_record = (
                db.query(IBDGroupRank)
                .filter(
                    IBDGroupRank.market == normalized_market,
                    IBDGroupRank.rs_formula_version == BALANCED_RS_FORMULA_VERSION,
                )
                .order_by(desc(IBDGroupRank.date))
                .first()
            )
            if not latest_record:
                return []
            latest_date = latest_record.date

        return (
            db.query(IBDGroupRank)
            .filter(
                IBDGroupRank.date == latest_date,
                IBDGroupRank.market == normalized_market,
                IBDGroupRank.rs_formula_version == BALANCED_RS_FORMULA_VERSION,
            )
            .order_by(IBDGroupRank.rank)
            .limit(limit)
            .all()
        )

    def existing_dates(
        self,
        db: Session,
        *,
        start_date: date,
        end_date: date,
        market: str,
    ) -> frozenset[date]:
        rows = (
            db.query(IBDGroupRank.date)
            .distinct()
            .filter(
                and_(
                    IBDGroupRank.date >= start_date,
                    IBDGroupRank.date <= end_date,
                    IBDGroupRank.market == (market or "US").upper(),
                    IBDGroupRank.rs_formula_version == BALANCED_RS_FORMULA_VERSION,
                )
            )
            .all()
        )
        return frozenset(item_date for (item_date,) in rows)

    def historical_ranks_batch(
        self,
        db: Session,
        *,
        group_names: Sequence[str],
        current_date: date,
        period_days: Mapping[str, int],
        market: str,
    ) -> dict[tuple[str, str], int]:
        if not group_names or not period_days:
            return {}

        max_days = max(period_days.values())
        earliest_date = current_date - timedelta(
            days=max_days + CALENDAR_DAY_GROUP_RANK_LOOKUP_TOLERANCE_DAYS
        )
        all_records = (
            db.query(
                IBDGroupRank.industry_group,
                IBDGroupRank.date,
                IBDGroupRank.rank,
            )
            .filter(
                and_(
                    IBDGroupRank.industry_group.in_(group_names),
                    IBDGroupRank.date >= earliest_date,
                    IBDGroupRank.date < current_date,
                    IBDGroupRank.market == (market or "US").upper(),
                    IBDGroupRank.rs_formula_version == BALANCED_RS_FORMULA_VERSION,
                )
            )
            .all()
        )

        group_history: dict[str, list[tuple[date, int]]] = {}
        for record in all_records:
            group_history.setdefault(
                record.industry_group,
                [],
            ).append((record.date, record.rank))

        result: dict[tuple[str, str], int] = {}
        for group_name in group_names:
            history = group_history.get(group_name, [])
            if not history:
                continue

            for period_name, days in period_days.items():
                target_date = current_date - timedelta(days=days)
                candidates = [
                    (item_date, rank)
                    for item_date, rank in history
                    if (
                        abs((item_date - target_date).days)
                        <= CALENDAR_DAY_GROUP_RANK_LOOKUP_TOLERANCE_DAYS
                    )
                ]
                if not candidates:
                    continue

                def distance_key(
                    item: tuple[date, int],
                ) -> tuple[int, bool]:
                    item_date, _ = item
                    delta = item_date - target_date
                    return (
                        abs(delta.days),
                        delta.days > 0,
                    )

                _, closest_rank = min(
                    candidates,
                    key=distance_key,
                )
                result[(group_name, period_name)] = closest_rank

        return result

    def group_rank_rows(
        self,
        db: Session,
        *,
        industry_group: str,
        start_date: date,
        market: str,
    ) -> list[IBDGroupRank]:
        return (
            db.query(IBDGroupRank)
            .filter(
                and_(
                    IBDGroupRank.industry_group == industry_group,
                    IBDGroupRank.date >= start_date,
                    IBDGroupRank.market == (market or "US").upper(),
                    IBDGroupRank.rs_formula_version == BALANCED_RS_FORMULA_VERSION,
                )
            )
            .order_by(IBDGroupRank.date.desc())
            .all()
        )
