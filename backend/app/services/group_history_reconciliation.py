"""Versioned persistence for background Group history reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
import json
from typing import Any, Collection
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.app_settings import AppSetting
from app.domain.group_history import GroupHistoryTarget
from app.domain.relative_strength import BALANCED_RS_FORMULA_VERSION


GROUP_HISTORY_RECONCILIATION_SCHEMA_VERSION = 2
GROUP_HISTORY_RECONCILIATION_CATEGORY = "group_history_reconciliation"
GROUP_HISTORY_RECONCILIATION_KEY_PREFIX = (
    f"runtime.group_history.v{GROUP_HISTORY_RECONCILIATION_SCHEMA_VERSION}."
)
GROUP_HISTORY_RECONCILIATION_ACTIVE_TIMEOUT = timedelta(hours=6)
GROUP_HISTORY_RECONCILIATION_QUEUED_TIMEOUT = timedelta(days=1)


class GroupHistoryReconciliationStatus(StrEnum):
    DISPATCHING = "dispatching"
    QUEUED = "queued"
    REPAIRING = "repairing"
    FINALIZING = "finalizing"
    READY = "ready"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


@dataclass(frozen=True)
class GroupHistoryReservation:
    target: GroupHistoryTarget
    reservation_id: str


@dataclass(frozen=True)
class GroupHistoryReconciliationMarker:
    market: str
    formula_version: str
    through_date: date
    status: GroupHistoryReconciliationStatus
    reservation_id: str
    updated_at: str
    counts: dict[str, Any] | None = None
    error: str | None = None

    @property
    def target(self) -> GroupHistoryTarget:
        return GroupHistoryTarget(
            market=self.market,
            through_date=self.through_date,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": GROUP_HISTORY_RECONCILIATION_SCHEMA_VERSION,
            "market": self.market,
            "formula_version": self.formula_version,
            "through_date": self.through_date.isoformat(),
            "status": self.status.value,
            "reservation_id": self.reservation_id,
            "updated_at": self.updated_at,
            "counts": dict(self.counts or {}),
            "error": self.error,
        }


class GroupHistoryReconciliationRepository:
    @staticmethod
    def key(market: str) -> str:
        return f"{GROUP_HISTORY_RECONCILIATION_KEY_PREFIX}{str(market).upper()}"

    def load(
        self,
        db: Session,
        *,
        market: str,
    ) -> GroupHistoryReconciliationMarker | None:
        marker, _encoded = self._load_record(db, market=market)
        return marker

    def _load_record(
        self,
        db: Session,
        *,
        market: str,
    ) -> tuple[GroupHistoryReconciliationMarker | None, str | None]:
        setting = (
            db.query(AppSetting)
            .filter(AppSetting.key == self.key(market))
            .one_or_none()
        )
        if setting is None:
            return None, None
        try:
            payload = json.loads(setting.value)
            if not isinstance(payload, dict):
                return None, setting.value
            if (
                int(payload.get("schema_version"))
                != GROUP_HISTORY_RECONCILIATION_SCHEMA_VERSION
            ):
                return None, setting.value
            marker = GroupHistoryReconciliationMarker(
                market=str(payload["market"]).upper(),
                formula_version=str(payload["formula_version"]),
                through_date=date.fromisoformat(str(payload["through_date"])),
                status=GroupHistoryReconciliationStatus(str(payload["status"])),
                reservation_id=str(payload["reservation_id"]),
                updated_at=str(payload["updated_at"]),
                counts=(
                    dict(payload["counts"])
                    if isinstance(payload.get("counts"), dict)
                    else None
                ),
                error=(str(payload["error"]) if payload.get("error") else None),
            )
            return marker, setting.value
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None, setting.value

    def reserve(
        self,
        db: Session,
        *,
        target: GroupHistoryTarget,
    ) -> GroupHistoryReservation | None:
        return self._reserve(
            db,
            target=target,
            status=GroupHistoryReconciliationStatus.DISPATCHING,
        )

    def reserve_finalization(
        self,
        db: Session,
        *,
        target: GroupHistoryTarget,
    ) -> GroupHistoryReservation | None:
        """Fence finalization, adopting a same-target pending dispatch."""
        for _attempt in range(2):
            reservation = self._reserve(
                db,
                target=target,
                status=GroupHistoryReconciliationStatus.FINALIZING,
            )
            if reservation is not None:
                return reservation
            current = self.load(db, market=target.market)
            if not (
                current is not None
                and current.target == target
                and current.status
                in {
                    GroupHistoryReconciliationStatus.DISPATCHING,
                    GroupHistoryReconciliationStatus.QUEUED,
                }
            ):
                return None
        return None

    def _reserve(
        self,
        db: Session,
        *,
        target: GroupHistoryTarget,
        status: GroupHistoryReconciliationStatus,
    ) -> GroupHistoryReservation | None:
        existing, observed_value = self._load_record(
            db,
            market=target.market,
        )
        adopting_matching_queue = bool(
            status is GroupHistoryReconciliationStatus.FINALIZING
            and existing is not None
            and existing.target == target
            and existing.status
            in {
                GroupHistoryReconciliationStatus.DISPATCHING,
                GroupHistoryReconciliationStatus.QUEUED,
            }
        )
        if (
            existing is not None
            and existing.status
            in {
                GroupHistoryReconciliationStatus.DISPATCHING,
                GroupHistoryReconciliationStatus.QUEUED,
                GroupHistoryReconciliationStatus.REPAIRING,
                GroupHistoryReconciliationStatus.FINALIZING,
            }
            and not self._is_stale(existing)
            and (
                existing.target == target
                or existing.status is GroupHistoryReconciliationStatus.FINALIZING
            )
            and not adopting_matching_queue
        ):
            return None

        reservation = GroupHistoryReservation(
            target=target,
            reservation_id=str(uuid4()),
        )
        marker = self._marker(
            target=target,
            status=status,
            reservation_id=reservation.reservation_id,
        )
        encoded = json.dumps(marker.as_dict(), sort_keys=True)
        key = self.key(target.market)
        if observed_value is not None:
            result = db.execute(
                update(AppSetting)
                .where(
                    AppSetting.key == key,
                    AppSetting.value == observed_value,
                )
                .values(
                    value=encoded,
                    category=GROUP_HISTORY_RECONCILIATION_CATEGORY,
                )
            )
            if result.rowcount != 1:
                db.rollback()
                return None
            db.commit()
            return reservation

        try:
            db.add(
                AppSetting(
                    key=key,
                    value=encoded,
                    category=GROUP_HISTORY_RECONCILIATION_CATEGORY,
                    description=f"Group history repair state for {target.market}",
                )
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            return None
        return reservation

    @staticmethod
    def _is_stale(marker: GroupHistoryReconciliationMarker) -> bool:
        try:
            updated_at = datetime.fromisoformat(marker.updated_at)
        except ValueError:
            return True
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        timeout = (
            GROUP_HISTORY_RECONCILIATION_QUEUED_TIMEOUT
            if marker.status is GroupHistoryReconciliationStatus.QUEUED
            else GROUP_HISTORY_RECONCILIATION_ACTIVE_TIMEOUT
        )
        return datetime.now(timezone.utc) - updated_at > timeout

    def owns(
        self,
        db: Session,
        *,
        reservation: GroupHistoryReservation,
    ) -> bool:
        marker = self.load(db, market=reservation.target.market)
        return bool(
            marker is not None
            and marker.target == reservation.target
            and marker.reservation_id == reservation.reservation_id
        )

    def transition(
        self,
        db: Session,
        *,
        reservation: GroupHistoryReservation,
        expected_statuses: Collection[GroupHistoryReconciliationStatus],
        status: GroupHistoryReconciliationStatus,
        counts: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        current, observed_value = self._load_record(
            db,
            market=reservation.target.market,
        )
        if (
            current is None
            or observed_value is None
            or current.target != reservation.target
            or current.reservation_id != reservation.reservation_id
            or current.status not in expected_statuses
        ):
            return False
        marker = self._marker(
            target=reservation.target,
            status=status,
            reservation_id=reservation.reservation_id,
            counts=counts,
            error=error,
        )
        encoded = json.dumps(marker.as_dict(), sort_keys=True)
        result = db.execute(
            update(AppSetting)
            .where(
                AppSetting.key == self.key(reservation.target.market),
                AppSetting.value == observed_value,
            )
            .values(
                value=encoded,
                category=GROUP_HISTORY_RECONCILIATION_CATEGORY,
            )
        )
        if result.rowcount != 1:
            db.rollback()
            return False
        db.commit()
        return True

    @staticmethod
    def _marker(
        *,
        target: GroupHistoryTarget,
        status: GroupHistoryReconciliationStatus,
        reservation_id: str,
        counts: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> GroupHistoryReconciliationMarker:
        return GroupHistoryReconciliationMarker(
            market=target.market,
            formula_version=BALANCED_RS_FORMULA_VERSION,
            through_date=target.through_date,
            status=status,
            reservation_id=reservation_id,
            updated_at=datetime.now(timezone.utc).isoformat(),
            counts=counts,
            error=error,
        )


__all__ = [
    "GROUP_HISTORY_RECONCILIATION_SCHEMA_VERSION",
    "GROUP_HISTORY_RECONCILIATION_ACTIVE_TIMEOUT",
    "GROUP_HISTORY_RECONCILIATION_QUEUED_TIMEOUT",
    "GroupHistoryReservation",
    "GroupHistoryReconciliationMarker",
    "GroupHistoryReconciliationRepository",
    "GroupHistoryReconciliationStatus",
]
