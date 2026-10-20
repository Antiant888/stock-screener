"""Single-owner execution and finalization for Group history repair."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from sqlalchemy.orm import Session

from app.domain.group_history import GroupHistoryTarget
from app.domain.relative_strength import BALANCED_RS_FORMULA_VERSION
from app.services.group_history_bootstrap_service import (
    GroupHistoryBootstrapService,
    GroupHistoryBootstrapStatus,
)
from app.services.group_history_reconciliation import (
    GroupHistoryReconciliationRepository,
    GroupHistoryReservation,
    GroupHistoryReconciliationStatus,
)

logger = logging.getLogger(__name__)


class GroupHistoryExecutionService:
    """Repair one immutable target and publish its result exactly once."""

    def __init__(
        self,
        *,
        bootstrap_service: GroupHistoryBootstrapService,
        reconciliation_repository: GroupHistoryReconciliationRepository,
        bump_epoch: Callable[[str], None],
        publish_snapshot: Callable[[GroupHistoryTarget], object | None],
        mark_started: Callable[..., object],
        mark_completed: Callable[..., object],
        mark_failed: Callable[..., object],
        resolve_current_target: Callable[..., GroupHistoryTarget] | None = None,
    ) -> None:
        self._bootstrap_service = bootstrap_service
        self._reconciliation_repository = reconciliation_repository
        self._bump_epoch = bump_epoch
        self._publish_snapshot = publish_snapshot
        self._mark_started = mark_started
        self._mark_completed = mark_completed
        self._mark_failed = mark_failed
        self._resolve_current_target = resolve_current_target

    def execute_bootstrap(
        self,
        db: Session,
        *,
        target: GroupHistoryTarget,
        task_name: str,
        task_id: str | None,
        strict: bool = True,
        activity_lifecycle: str | None = None,
    ) -> dict:
        return self._execute(
            db,
            target=target,
            reservation=None,
            task_name=task_name,
            task_id=task_id,
            strict=strict,
            lifecycle=activity_lifecycle or "bootstrap",
        )

    def execute_reconciliation(
        self,
        db: Session,
        *,
        reservation: GroupHistoryReservation,
        task_name: str,
        task_id: str | None,
    ) -> dict:
        return self._execute(
            db,
            target=reservation.target,
            reservation=reservation,
            task_name=task_name,
            task_id=task_id,
            strict=False,
            lifecycle="group_history_reconciliation",
        )

    def _execute(
        self,
        db: Session,
        *,
        target: GroupHistoryTarget,
        reservation: GroupHistoryReservation | None,
        task_name: str,
        task_id: str | None,
        strict: bool,
        lifecycle: str,
    ) -> dict:
        fresh_bootstrap = reservation is None
        activity = {
            "market": target.market,
            "stage_key": "group_history",
            "lifecycle": lifecycle,
            "task_name": task_name,
            "task_id": task_id,
        }
        failure_recorded = False
        try:
            if (
                reservation is not None
                and not self._reconciliation_repository.transition(
                    db,
                    reservation=reservation,
                    expected_statuses={
                        GroupHistoryReconciliationStatus.DISPATCHING,
                        GroupHistoryReconciliationStatus.QUEUED,
                    },
                    status=GroupHistoryReconciliationStatus.REPAIRING,
                )
            ):
                return self._superseded_payload(reservation, "reservation_lost")
            if reservation is not None and not self._reservation_is_current(
                db,
                reservation=reservation,
            ):
                return self._record_superseded(
                    db,
                    reservation=reservation,
                    reason="target_changed_before_repair",
                )
            self._mark_activity_safely(
                db,
                callback=self._mark_started,
                activity=activity,
                message="Checking Group ranking history",
            )
            result = self._bootstrap_service.ensure(db, target=target)
            payload = result.as_dict()
            payload["timestamp"] = datetime.now().isoformat()
            payload["cache_invalidated"] = False
            payload["ui_snapshot_published"] = target.market != "US"

            if result.status is GroupHistoryBootstrapStatus.INCOMPLETE:
                message = f"Group history remains incomplete for {target.market}"
                if reservation is not None:
                    self._record_incomplete(
                        db,
                        reservation=reservation,
                        payload=payload,
                        message=message,
                    )
                self._mark_failure_safely(db, activity=activity, message=message)
                failure_recorded = True
                if strict:
                    raise RuntimeError(message)
                return payload

            if result.status is GroupHistoryBootstrapStatus.READY:
                reservation, superseded = self._prepare_finalization(
                    db,
                    target=target,
                    reservation=reservation,
                    fresh_bootstrap=fresh_bootstrap,
                    strict=strict,
                )
                if superseded is not None:
                    return superseded
                assert reservation is not None
                self._bump_epoch(target.market)
                payload["cache_invalidated"] = True
                if target.market == "US":
                    published = self._publish_snapshot(target)
                    payload["ui_snapshot_published"] = published is not None
                    if published is None:
                        message = "US Group bootstrap snapshot publication failed"
                        payload["status"] = "incomplete"
                        if reservation is not None:
                            self._record_incomplete(
                                db,
                                reservation=reservation,
                                payload=payload,
                                message=message,
                                expected_statuses={
                                    GroupHistoryReconciliationStatus.FINALIZING
                                },
                            )
                        self._mark_failure_safely(
                            db,
                            activity=activity,
                            message=message,
                        )
                        failure_recorded = True
                        if strict:
                            raise RuntimeError(message)
                        return payload
                if not self._target_is_current(db, target=target):
                    superseded = self._record_superseded(
                        db,
                        reservation=reservation,
                        reason="target_changed_after_publication",
                        expected_statuses={
                            GroupHistoryReconciliationStatus.FINALIZING
                        },
                    )
                    if fresh_bootstrap and strict:
                        raise RuntimeError(
                            "Group history target changed after publication"
                        )
                    return superseded

            if reservation is not None:
                expected_status = (
                    GroupHistoryReconciliationStatus.FINALIZING
                    if result.status is GroupHistoryBootstrapStatus.READY
                    else GroupHistoryReconciliationStatus.REPAIRING
                )
                if not self._reconciliation_repository.transition(
                    db,
                    reservation=reservation,
                    expected_statuses={expected_status},
                    status=GroupHistoryReconciliationStatus.READY,
                    counts=payload.get("after"),
                ):
                    superseded = self._superseded_payload(
                        reservation,
                        "reservation_lost_after_finalization",
                    )
                    if fresh_bootstrap and strict:
                        raise RuntimeError(
                            "Group history finalization reservation lost"
                        )
                    return superseded
            self._mark_activity_safely(
                db,
                callback=self._mark_completed,
                activity=activity,
                message="Group ranking history ready",
            )
            return payload
        except Exception as exc:
            db.rollback()
            if reservation is not None:
                self._mark_reconciliation_failed_safely(
                    db,
                    reservation=reservation,
                    error=str(exc),
                )
            if not failure_recorded:
                self._mark_failure_safely(
                    db,
                    activity=activity,
                    message=str(exc),
                )
            raise

    def _record_incomplete(
        self,
        db: Session,
        *,
        reservation: GroupHistoryReservation,
        payload: dict,
        message: str,
        expected_statuses: set[GroupHistoryReconciliationStatus] | None = None,
    ) -> None:
        self._reconciliation_repository.transition(
            db,
            reservation=reservation,
            expected_statuses=expected_statuses
            or {GroupHistoryReconciliationStatus.REPAIRING},
            status=GroupHistoryReconciliationStatus.INCOMPLETE,
            counts=payload.get("after"),
            error=message,
        )

    def _mark_failure_safely(
        self,
        db: Session,
        *,
        activity: dict[str, object],
        message: str,
    ) -> None:
        db.rollback()
        try:
            self._mark_failed(db, **activity, message=message)
        except Exception:
            db.rollback()
            logger.warning("Failed to record Group history failure", exc_info=True)

    @staticmethod
    def _mark_activity_safely(
        db: Session,
        *,
        callback: Callable[..., object],
        activity: dict[str, object],
        message: str,
    ) -> None:
        try:
            callback(db, **activity, message=message)
        except Exception:
            db.rollback()
            logger.warning("Failed to record Group history activity", exc_info=True)

    def _mark_reconciliation_failed_safely(
        self,
        db: Session,
        *,
        reservation: GroupHistoryReservation,
        error: str,
    ) -> None:
        try:
            self._reconciliation_repository.transition(
                db,
                reservation=reservation,
                expected_statuses={
                    GroupHistoryReconciliationStatus.REPAIRING,
                    GroupHistoryReconciliationStatus.FINALIZING,
                },
                status=GroupHistoryReconciliationStatus.FAILED,
                error=error,
            )
        except Exception:
            db.rollback()
            logger.warning(
                "Failed to record Group history reconciliation failure",
                exc_info=True,
            )

    def _reservation_is_current(
        self,
        db: Session,
        *,
        reservation: GroupHistoryReservation,
    ) -> bool:
        if not self._reconciliation_repository.owns(
            db,
            reservation=reservation,
        ):
            return False
        if self._resolve_current_target is None:
            return True
        return self._target_is_current(db, target=reservation.target)

    def _target_is_current(
        self,
        db: Session,
        *,
        target: GroupHistoryTarget,
    ) -> bool:
        if self._resolve_current_target is None:
            return True
        return self._resolve_current_target(db, market=target.market) == target

    def _prepare_finalization(
        self,
        db: Session,
        *,
        target: GroupHistoryTarget,
        reservation: GroupHistoryReservation | None,
        fresh_bootstrap: bool,
        strict: bool,
    ) -> tuple[GroupHistoryReservation | None, dict | None]:
        if fresh_bootstrap:
            if not self._target_is_current(db, target=target):
                return None, self._fresh_bootstrap_superseded(
                    target=target,
                    reason="target_changed_before_finalization",
                    strict=strict,
                )
            reservation = self._reconciliation_repository.reserve_finalization(
                db,
                target=target,
            )
            if reservation is None:
                return None, self._fresh_bootstrap_superseded(
                    target=target,
                    reason="finalization_reservation_unavailable",
                    strict=strict,
                )

        assert reservation is not None
        expected_status = GroupHistoryReconciliationStatus.FINALIZING
        if not fresh_bootstrap:
            expected_status = GroupHistoryReconciliationStatus.REPAIRING
        if not self._reservation_is_current(db, reservation=reservation):
            superseded = self._record_superseded(
                db,
                reservation=reservation,
                reason="target_changed_before_finalization",
                expected_statuses={expected_status},
            )
            if fresh_bootstrap and strict:
                raise RuntimeError("Group history target changed before finalization")
            return None, superseded
        if not fresh_bootstrap:
            if not self._reconciliation_repository.transition(
                db,
                reservation=reservation,
                expected_statuses={expected_status},
                status=GroupHistoryReconciliationStatus.FINALIZING,
            ):
                return None, self._superseded_payload(
                    reservation,
                    "reservation_lost_before_finalization",
                )
        return reservation, None

    def _record_superseded(
        self,
        db: Session,
        *,
        reservation: GroupHistoryReservation,
        reason: str,
        expected_statuses: set[GroupHistoryReconciliationStatus] | None = None,
    ) -> dict:
        self._reconciliation_repository.transition(
            db,
            reservation=reservation,
            expected_statuses=expected_statuses
            or {GroupHistoryReconciliationStatus.REPAIRING},
            status=GroupHistoryReconciliationStatus.INCOMPLETE,
            error=reason,
        )
        return self._superseded_payload(reservation, reason)

    @classmethod
    def _fresh_bootstrap_superseded(
        cls,
        *,
        target: GroupHistoryTarget,
        reason: str,
        strict: bool,
    ) -> dict:
        if strict:
            raise RuntimeError(f"Group history {reason.replace('_', ' ')}")
        return cls._target_superseded_payload(target, reason)

    @classmethod
    def _superseded_payload(
        cls,
        reservation: GroupHistoryReservation,
        reason: str,
    ) -> dict:
        return cls._target_superseded_payload(
            reservation.target,
            reason,
        )

    @staticmethod
    def _target_superseded_payload(
        target: GroupHistoryTarget,
        reason: str,
    ) -> dict:
        return {
            "status": "superseded",
            "reason": reason,
            "market": target.market,
            "formula_version": BALANCED_RS_FORMULA_VERSION,
            "through_date": target.through_date.isoformat(),
        }


__all__ = [
    "GroupHistoryExecutionService",
]
