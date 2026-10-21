"""Idempotent six-month Group history repair."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.orm import Session

from app.domain.group_history import GroupHistoryTarget
from app.domain.relative_strength import (
    BALANCED_RS_FORMULA_VERSION,
    GroupSnapshotIdentity,
)
from app.services.group_history_readiness_service import (
    GroupHistoryReadinessReport,
)


class GroupHistoryBootstrapStatus(StrEnum):
    READY = "ready"
    INCOMPLETE = "incomplete"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class GroupHistoryBootstrapResult:
    status: GroupHistoryBootstrapStatus
    market: str
    through_date: date
    formula_version: str
    before: GroupHistoryReadinessReport
    after: GroupHistoryReadinessReport
    processed_dates: tuple[date, ...] = ()
    failed_dates: tuple[date, ...] = ()
    errors: tuple[tuple[date, str], ...] = ()
    skipped_valid: int = 0
    policy_counts: dict[str, int] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "market": self.market,
            "through_date": self.through_date.isoformat(),
            "formula_version": self.formula_version,
            "processed_dates": [item.isoformat() for item in self.processed_dates],
            "failed_dates": [item.isoformat() for item in self.failed_dates],
            "errors": {item.isoformat(): message for item, message in self.errors},
            "skipped_valid": self.skipped_valid,
            "policy_counts": dict(self.policy_counts or {}),
            "before": self.before.as_dict(),
            "after": self.after.as_dict(),
        }


class GroupHistoryBootstrapService:
    """Repair only missing or integrity-invalid Group snapshot identities."""

    def __init__(
        self,
        *,
        readiness_service,
        snapshot_coordinator,
        universe_resolver,
    ) -> None:
        self._readiness_service = readiness_service
        self._snapshot_coordinator = snapshot_coordinator
        self._universe_resolver = universe_resolver

    def ensure(
        self,
        db: Session,
        *,
        target: GroupHistoryTarget,
    ) -> GroupHistoryBootstrapResult:
        before = self._readiness_service.evaluate(db, target=target)
        if not before.supported:
            return GroupHistoryBootstrapResult(
                status=GroupHistoryBootstrapStatus.SKIPPED,
                market=target.market,
                through_date=target.through_date,
                formula_version=BALANCED_RS_FORMULA_VERSION,
                before=before,
                after=before,
            )
        if before.ready:
            return GroupHistoryBootstrapResult(
                status=GroupHistoryBootstrapStatus.READY,
                market=target.market,
                through_date=target.through_date,
                formula_version=BALANCED_RS_FORMULA_VERSION,
                before=before,
                after=before,
                skipped_valid=len(before.valid_dates),
            )
        invalid_dates = set(before.invalid_dates)
        target_dates = tuple(sorted(set(before.missing_dates) | invalid_dates))
        processed: list[date] = []
        failed: list[date] = []
        errors: list[tuple[date, str]] = []
        policies: Counter[str] = Counter()
        for target_date in target_dates:
            identity = GroupSnapshotIdentity(
                target.market,
                target_date,
                BALANCED_RS_FORMULA_VERSION,
            )
            try:
                if target_date in invalid_dates:
                    self._snapshot_coordinator.repair_snapshot(db, identity=identity)
                else:
                    self._snapshot_coordinator.ensure_snapshot(db, identity=identity)
                db.commit()
            except SoftTimeLimitExceeded:
                db.rollback()
                raise
            except Exception as exc:
                db.rollback()
                failed.append(target_date)
                errors.append((target_date, str(exc)))
                continue
            processed.append(target_date)
            policy = self._universe_resolver.policy_for(
                target.market,
                target_date,
            )
            if policy:
                policies[policy] += 1

        after = self._readiness_service.evaluate(db, target=target)
        return GroupHistoryBootstrapResult(
            status=(
                GroupHistoryBootstrapStatus.READY
                if after.ready
                else GroupHistoryBootstrapStatus.INCOMPLETE
            ),
            market=target.market,
            through_date=target.through_date,
            formula_version=BALANCED_RS_FORMULA_VERSION,
            before=before,
            after=after,
            processed_dates=tuple(processed),
            failed_dates=tuple(failed),
            errors=tuple(errors),
            skipped_valid=len(before.valid_dates),
            policy_counts=dict(policies),
        )


__all__ = [
    "GroupHistoryBootstrapResult",
    "GroupHistoryBootstrapService",
    "GroupHistoryBootstrapStatus",
]
