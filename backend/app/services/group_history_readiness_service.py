"""Read-only readiness checks for live Group ranking history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.analysis.rrg_weekly import bucket_rrg_weekly
from app.domain.group_history import GroupHistoryTarget
from app.domain.markets import get_market_catalog
from app.domain.relative_strength import BALANCED_RS_FORMULA_VERSION
from app.services.group_rank_history_policy import (
    CALENDAR_DAY_GROUP_RANK_CHANGE_WINDOWS,
    CALENDAR_DAY_GROUP_RANK_LOOKUP_TOLERANCE_DAYS,
    DEFAULT_CALENDAR_DAY_GROUP_RANK_HISTORY_LOOKBACK_DAYS,
)
from app.services.group_rank_snapshot_reader import (
    GroupRankSnapshotReader,
    GroupSnapshotIntegrityError,
)
from app.services.market_calendar_service import MarketCalendarService
from app.services.rrg_service import (
    DEFAULT_LOOKBACK_DAYS,
    MIN_TAIL_WEEKS,
    compute_group_rrg,
)


@dataclass(frozen=True)
class GroupHistoryReadinessReport:
    market: str
    through_date: date
    formula_version: str
    supported: bool
    desired_dates: tuple[date, ...] = ()
    valid_dates: tuple[date, ...] = ()
    missing_dates: tuple[date, ...] = ()
    invalid_dates: tuple[date, ...] = ()
    invalid_reasons: tuple[tuple[date, str], ...] = ()
    rank_change_ready: dict[str, bool] | None = None
    rrg_required: bool = False
    rrg_usable_weeks: int = 0
    rrg_plottable_series: int = 0
    ready: bool = False
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "through_date": self.through_date.isoformat(),
            "formula_version": self.formula_version,
            "supported": self.supported,
            "desired_dates": [item.isoformat() for item in self.desired_dates],
            "valid_dates": [item.isoformat() for item in self.valid_dates],
            "missing_dates": [item.isoformat() for item in self.missing_dates],
            "invalid_dates": [item.isoformat() for item in self.invalid_dates],
            "invalid_reasons": {
                item.isoformat(): reason for item, reason in self.invalid_reasons
            },
            "rank_change_ready": dict(self.rank_change_ready or {}),
            "rrg_required": self.rrg_required,
            "rrg_usable_weeks": self.rrg_usable_weeks,
            "rrg_plottable_series": self.rrg_plottable_series,
            "ready": self.ready,
            "reason": self.reason,
        }


class GroupHistoryReadinessService:
    """Verify formula-scoped Group history without writing to the database."""

    def __init__(
        self,
        *,
        calendar_service: MarketCalendarService | None = None,
        snapshot_reader: GroupRankSnapshotReader | None = None,
        rrg_history_provider=None,
    ) -> None:
        if rrg_history_provider is None:
            raise ValueError("RRG history provider is required")
        self._calendar_service = calendar_service or MarketCalendarService()
        self._snapshot_reader = snapshot_reader or GroupRankSnapshotReader()
        self._rrg_history_provider = rrg_history_provider

    def evaluate(
        self,
        db: Session,
        *,
        target: GroupHistoryTarget,
    ) -> GroupHistoryReadinessReport:
        catalog_entry = get_market_catalog().get(target.market)
        if not catalog_entry.capabilities.group_rankings:
            return GroupHistoryReadinessReport(
                market=target.market,
                through_date=target.through_date,
                formula_version=BALANCED_RS_FORMULA_VERSION,
                supported=False,
                ready=True,
                reason="group_rankings_not_supported",
            )

        start_date = target.through_date - timedelta(
            days=DEFAULT_CALENDAR_DAY_GROUP_RANK_HISTORY_LOOKBACK_DAYS
        )
        desired_dates = tuple(
            sorted(
                dict.fromkeys(
                    self._calendar_service.trading_days(
                        target.market,
                        start_date,
                        target.through_date,
                    )
                )
            )
        )
        window = self._snapshot_reader.inspect_window(
            db,
            market=target.market,
            formula_version=BALANCED_RS_FORMULA_VERSION,
            dates=desired_dates,
            include_top_symbol_names=False,
        )
        snapshots = window.snapshots
        invalid_by_date = window.errors
        valid = [item for item in desired_dates if item in snapshots]
        invalid = [item for item in desired_dates if item in invalid_by_date]
        missing = [
            item
            for item in desired_dates
            if item not in snapshots and item not in invalid_by_date
        ]
        invalid_reasons = [
            (item, invalid_by_date[item]) for item in invalid
        ]

        rank_change_ready = self._rank_change_readiness(
            through_date=target.through_date,
            valid_dates=valid,
        )
        rrg_required = bool(catalog_entry.capabilities.rrg_scopes)
        rrg_usable_weeks = 0
        rrg_plottable_series = 0
        if rrg_required:
            rrg_usable_weeks, rrg_plottable_series = self._rrg_readiness(
                db,
                market=target.market,
                through_date=target.through_date,
            )

        ready = (
            bool(desired_dates)
            and not missing
            and not invalid
            and all(rank_change_ready.values())
            and (
                not rrg_required
                or (
                    rrg_usable_weeks >= MIN_TAIL_WEEKS
                    and rrg_plottable_series > 0
                )
            )
        )
        return GroupHistoryReadinessReport(
            market=target.market,
            through_date=target.through_date,
            formula_version=BALANCED_RS_FORMULA_VERSION,
            supported=True,
            desired_dates=desired_dates,
            valid_dates=tuple(valid),
            missing_dates=tuple(missing),
            invalid_dates=tuple(invalid),
            invalid_reasons=tuple(invalid_reasons),
            rank_change_ready=rank_change_ready,
            rrg_required=rrg_required,
            rrg_usable_weeks=rrg_usable_weeks,
            rrg_plottable_series=rrg_plottable_series,
            ready=ready,
        )

    @staticmethod
    def _rank_change_readiness(
        *,
        through_date: date,
        valid_dates: list[date],
    ) -> dict[str, bool]:
        readiness: dict[str, bool] = {}
        for period, days in CALENDAR_DAY_GROUP_RANK_CHANGE_WINDOWS.items():
            target = through_date - timedelta(days=days)
            readiness[period] = any(
                abs((candidate - target).days)
                <= CALENDAR_DAY_GROUP_RANK_LOOKUP_TOLERANCE_DAYS
                for candidate in valid_dates
                if candidate < through_date
            )
        return readiness

    def _rrg_readiness(
        self,
        db: Session,
        *,
        market: str,
        through_date: date,
    ) -> tuple[int, int]:
        try:
            _latest, _meta, series = (
                self._rrg_history_provider.get_all_groups_history(
                    db,
                    market=market,
                    days=DEFAULT_LOOKBACK_DAYS,
                    as_of_date=through_date,
                )
            )
        except (GroupSnapshotIntegrityError, LookupError, ValueError):
            return 0, 0
        usable_weeks = 0
        plottable = 0
        for observations in series.values():
            daily = [(item[0], float(item[1])) for item in observations]
            usable_weeks = max(usable_weeks, len(bucket_rrg_weekly(daily)))
            if compute_group_rrg(daily) is not None:
                plottable += 1
        return usable_weeks, plottable


__all__ = [
    "GroupHistoryReadinessReport",
    "GroupHistoryReadinessService",
]
