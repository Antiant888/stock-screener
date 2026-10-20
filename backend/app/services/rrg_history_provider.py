"""History providers for Relative Rotation Graph inputs."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Protocol, Sequence, Tuple

from app.domain.relative_strength import BALANCED_RS_FORMULA_VERSION
from app.services.group_rank_snapshot_reader import GroupRankSnapshotReader

RRGHistoryResult = Tuple[
    str | None,
    dict[str, dict[str, Any]],
    dict[str, list[tuple[date, float, int]]],
]


class RRGHistoryProvider(Protocol):
    """Source RRG-ready group-ranking history for one market."""

    def get_all_groups_history(
        self,
        db: Any,
        *,
        market: str,
        days: int,
        as_of_date: date | None = None,
    ) -> RRGHistoryResult:
        """Return latest date, current ranking metadata, and daily RS series."""


class StoredGroupRankHistoryProvider:
    """Read the stored balanced Group history for one Market."""

    def __init__(
        self,
        snapshot_reader: GroupRankSnapshotReader | None = None,
    ) -> None:
        self._snapshot_reader = snapshot_reader or GroupRankSnapshotReader()

    def get_all_groups_history(
        self,
        db: Any,
        *,
        market: str,
        days: int,
        as_of_date: date | None = None,
    ) -> RRGHistoryResult:
        normalized_market = str(market or "").strip().upper()
        through_date = as_of_date or date.max
        dates = self._snapshot_reader.available_dates(
            db,
            market=normalized_market,
            formula_version=BALANCED_RS_FORMULA_VERSION,
            through_date=through_date,
        )
        if not dates:
            return None, {}, {}
        latest_day = dates[-1]
        cutoff = latest_day - timedelta(days=days)
        selected_dates = tuple(item for item in dates if cutoff <= item <= latest_day)
        window = self._snapshot_reader.inspect_window(
            db,
            market=normalized_market,
            formula_version=BALANCED_RS_FORMULA_VERSION,
            dates=selected_dates,
            include_top_symbol_names=False,
        )
        snapshots_by_date = window.snapshots
        selected_dates = tuple(
            item for item in selected_dates if item in snapshots_by_date
        )
        if not selected_dates:
            return None, {}, {}
        latest_day = selected_dates[-1]
        snapshots = [
            (snapshot_date, snapshots_by_date.get(snapshot_date, []))
            for snapshot_date in selected_dates
        ]
        current = snapshots[-1][1]
        latest_date = latest_day.isoformat()
        meta = {row["industry_group"]: row for row in current}
        rows = [
            (
                str(row["industry_group"]),
                snapshot_date,
                float(row["avg_rs_rating"]),
                int(row.get("num_stocks") or 0),
            )
            for snapshot_date, snapshot in snapshots
            for row in snapshot
        ]
        return latest_date, meta, _collect_group_series(rows)


def _collect_group_series(
    rows: Sequence[Tuple[str, date, float, int | None]],
) -> dict[str, list[tuple[date, float, int]]]:
    series: dict[str, list[tuple[date, float, int]]] = defaultdict(list)
    for group, d, rs, ns in rows:
        series[group].append((d, float(rs), int(ns or 0)))
    return dict(series)


__all__ = [
    "RRGHistoryProvider",
    "RRGHistoryResult",
    "StoredGroupRankHistoryProvider",
]
