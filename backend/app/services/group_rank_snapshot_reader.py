from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.domain.relative_strength import (
    GroupSnapshotIdentity,
    RsPublicationIdentity,
    balanced_run_has_current_snapshot_contract,
)
from app.infra.db.models.relative_strength import MarketRsRun
from app.models.industry import IBDGroupRank
from app.services.group_ranking_payloads import (
    annotate_top_symbol_names,
    rank_record_payload,
)


class GroupSnapshotIntegrityError(RuntimeError):
    """Persisted Group rows do not form one coherent snapshot."""


class GroupSnapshotWindowIntegrityError(GroupSnapshotIntegrityError):
    """One or more dates in a snapshot window failed integrity checks."""

    def __init__(
        self,
        *,
        snapshots: dict[date, list[dict[str, Any]]],
        errors: dict[date, str],
    ) -> None:
        self.snapshots = snapshots
        self.errors = errors
        dates = ", ".join(item.isoformat() for item in errors)
        super().__init__(f"Group snapshot window contains invalid dates: {dates}")


@dataclass(frozen=True)
class GroupSnapshotWindowResult:
    snapshots: dict[date, list[dict[str, Any]]]
    errors: dict[date, str]


class GroupSnapshotUnavailable(LookupError):
    def __init__(self, identity: GroupSnapshotIdentity) -> None:
        self.identity = identity
        super().__init__(
            f"Group snapshot unavailable for {identity.market} on "
            f"{identity.as_of_date.isoformat()} ({identity.formula_version})"
        )


class GroupRankSnapshotReader:
    def load_publication(
        self,
        db: Session,
        *,
        publication: RsPublicationIdentity,
        include_top_symbol_names: bool = True,
    ) -> list[dict[str, Any]]:
        rows = self.load_exact(
            db,
            identity=publication.snapshot,
            include_top_symbol_names=include_top_symbol_names,
        )
        if not rows:
            return rows
        actual_run_ids = {row.get("market_rs_run_id") for row in rows}
        if actual_run_ids != {publication.market_rs_run_id}:
            raise GroupSnapshotIntegrityError(
                "Group rows do not reference the expected Market RS run"
            )
        if publication.market_rs_run_id is None:
            return rows
        run = db.get(MarketRsRun, publication.market_rs_run_id)
        if run is None or int(run.eligible_symbol_count) != publication.universe_size:
            raise GroupSnapshotIntegrityError(
                "Group rows do not reference the expected Market RS universe"
            )
        return rows

    def load_exact(
        self,
        db: Session,
        *,
        identity: GroupSnapshotIdentity,
        include_top_symbol_names: bool = True,
    ) -> list[dict[str, Any]]:
        records = (
            db.query(IBDGroupRank)
            .filter(
                IBDGroupRank.market == identity.market,
                IBDGroupRank.date == identity.as_of_date,
                IBDGroupRank.rs_formula_version == identity.formula_version,
            )
            .order_by(IBDGroupRank.rank, IBDGroupRank.industry_group)
            .all()
        )
        runs_by_id = self._load_market_runs(db, records=records)
        self._validate(
            identity=identity,
            records=records,
            runs_by_id=runs_by_id,
        )
        payload = self._payload(records)
        if include_top_symbol_names:
            annotate_top_symbol_names(db, payload)
        return payload

    def load_window(
        self,
        db: Session,
        *,
        market: str,
        formula_version: str,
        dates: tuple[date, ...] | list[date],
        include_top_symbol_names: bool = True,
    ) -> dict[date, list[dict[str, Any]]]:
        result = self.inspect_window(
            db,
            market=market,
            formula_version=formula_version,
            dates=dates,
            include_top_symbol_names=include_top_symbol_names,
        )
        if result.errors:
            raise GroupSnapshotWindowIntegrityError(
                snapshots=result.snapshots,
                errors=result.errors,
            )
        return result.snapshots

    def inspect_window(
        self,
        db: Session,
        *,
        market: str,
        formula_version: str,
        dates: tuple[date, ...] | list[date],
        include_top_symbol_names: bool = True,
    ) -> GroupSnapshotWindowResult:
        normalized_market = str(market or "").strip().upper()
        normalized_formula = str(formula_version or "").strip()
        selected_dates = tuple(sorted(dict.fromkeys(dates)))
        if not selected_dates:
            return GroupSnapshotWindowResult(snapshots={}, errors={})
        records = (
            db.query(IBDGroupRank)
            .filter(
                IBDGroupRank.market == normalized_market,
                IBDGroupRank.rs_formula_version == normalized_formula,
                IBDGroupRank.date.in_(selected_dates),
            )
            .order_by(
                IBDGroupRank.date,
                IBDGroupRank.rank,
                IBDGroupRank.industry_group,
            )
            .all()
        )
        records_by_date: dict[date, list[IBDGroupRank]] = defaultdict(list)
        for record in records:
            records_by_date[record.date].append(record)
        runs_by_id = self._load_market_runs(db, records=records)
        snapshots: dict[date, list[dict[str, Any]]] = {}
        errors: dict[date, str] = {}
        for snapshot_date in selected_dates:
            date_records = records_by_date.get(snapshot_date, [])
            if not date_records:
                continue
            identity = GroupSnapshotIdentity(
                normalized_market,
                snapshot_date,
                normalized_formula,
            )
            try:
                self._validate(
                    identity=identity,
                    records=date_records,
                    runs_by_id=runs_by_id,
                )
            except GroupSnapshotIntegrityError as exc:
                errors[snapshot_date] = str(exc)
                continue
            snapshots[snapshot_date] = self._payload(date_records)
        if include_top_symbol_names and snapshots:
            annotate_top_symbol_names(
                db,
                [row for snapshot in snapshots.values() for row in snapshot],
            )
        return GroupSnapshotWindowResult(snapshots=snapshots, errors=errors)

    @staticmethod
    def _payload(records: list[IBDGroupRank]) -> list[dict[str, Any]]:
        return [
            rank_record_payload(
                record,
                pct_rs_above_80=(
                    round(
                        100.0
                        * int(record.num_stocks_rs_above_80 or 0)
                        / int(record.num_stocks),
                        1,
                    )
                    if record.num_stocks
                    else None
                ),
            )
            for record in records
        ]

    @staticmethod
    def _load_market_runs(
        db: Session,
        *,
        records: list[IBDGroupRank],
    ) -> dict[int, MarketRsRun]:
        run_ids = {
            int(record.market_rs_run_id)
            for record in records
            if record.market_rs_run_id is not None
        }
        if not run_ids:
            return {}
        runs = db.query(MarketRsRun).filter(MarketRsRun.id.in_(run_ids)).all()
        return {int(run.id): run for run in runs}

    def load_rank_map(
        self,
        db: Session,
        *,
        identity: GroupSnapshotIdentity,
    ) -> dict[str, int]:
        return {
            str(row["industry_group"]): int(row["rank"])
            for row in self.load_exact(
                db,
                identity=identity,
                include_top_symbol_names=False,
            )
        }

    def available_dates(
        self,
        db: Session,
        *,
        market: str,
        formula_version: str,
        through_date: date,
    ) -> tuple[date, ...]:
        rows = (
            db.query(IBDGroupRank.date)
            .filter(
                IBDGroupRank.market == str(market).strip().upper(),
                IBDGroupRank.rs_formula_version == str(formula_version).strip(),
                IBDGroupRank.date <= through_date,
            )
            .distinct()
            .order_by(IBDGroupRank.date)
            .all()
        )
        return tuple(row[0] for row in rows)

    @staticmethod
    def _validate(
        *,
        identity: GroupSnapshotIdentity,
        records: list[IBDGroupRank],
        runs_by_id: dict[int, MarketRsRun],
    ) -> None:
        if not records:
            return
        ranks = [int(record.rank) for record in records]
        if ranks != list(range(1, len(records) + 1)):
            raise GroupSnapshotIntegrityError("Group ranks are not contiguous")
        if any(
            record.market != identity.market
            or record.date != identity.as_of_date
            or record.rs_formula_version != identity.formula_version
            for record in records
        ):
            raise GroupSnapshotIntegrityError("Group rows do not match their identity")
        run_ids = {record.market_rs_run_id for record in records}
        if None in run_ids or len(run_ids) != 1:
            raise GroupSnapshotIntegrityError(
                "Balanced Group rows mix Market RS run IDs"
            )
        run = runs_by_id.get(int(next(iter(run_ids))))
        if (
            run is None
            or run.status != "completed"
            or run.market != identity.market
            or run.as_of_date != identity.as_of_date
            or run.formula_version != identity.formula_version
        ):
            raise GroupSnapshotIntegrityError(
                "Group rows reference the wrong Market RS run"
            )
        if not balanced_run_has_current_snapshot_contract(run):
            raise GroupSnapshotIntegrityError(
                "Group rows reference a Market RS run with an incompatible "
                "balanced snapshot contract"
            )
