"""Transactional ownership boundary for local runtime bootstrap dispatches."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.relative_strength import BALANCED_RS_FORMULA_VERSION
from app.services.bootstrap_run_manifest import (
    BootstrapAlreadyRunning,
    BootstrapMarketOutcome,
    BootstrapQueueState,
    BootstrapRunManifest,
    BootstrapRunManifestRepository,
)
from app.services.market_activity_service import (
    stage_current_market_activity_failed,
    stage_market_activity_failed,
)
from app.services.runtime_preferences_service import (
    get_runtime_preferences,
    stage_runtime_preferences,
)


SessionFactory = Callable[[], Session]


@dataclass(frozen=True)
class BootstrapMarketCompletion:
    market: str
    succeeded: bool
    primary: bool
    failure_stage_key: str | None = None
    failure_message: str | None = None
    expected_formula_version: str | None = None

    @classmethod
    def ready(
        cls,
        *,
        market: str,
        primary: bool,
        expected_formula_version: str | None = None,
    ) -> BootstrapMarketCompletion:
        return cls(
            market=market,
            succeeded=True,
            primary=primary,
            expected_formula_version=expected_formula_version,
        )

    @classmethod
    def failed(
        cls,
        *,
        market: str,
        primary: bool,
        stage_key: str | None,
        message: str,
    ) -> BootstrapMarketCompletion:
        return cls(
            market=market,
            succeeded=False,
            primary=primary,
            failure_stage_key=stage_key,
            failure_message=message,
        )


def _is_manifest_key_conflict(exc: IntegrityError) -> bool:
    original = exc.orig
    constraint_name = getattr(getattr(original, "diag", None), "constraint_name", None)
    if constraint_name in {"app_settings_key_key", "ix_app_settings_key"}:
        return True
    message = str(original).lower()
    return "unique" in message and "app_settings.key" in message


class BootstrapDispatchLifecycle:
    def __init__(
        self,
        *,
        repository: BootstrapRunManifestRepository | None = None,
    ) -> None:
        self.repository = repository or BootstrapRunManifestRepository()

    def claim(
        self,
        db: Session,
        *,
        manifest: BootstrapRunManifest,
    ) -> BootstrapRunManifest:
        claimed = manifest.with_renewed_ownership()
        try:
            stage_runtime_preferences(
                db,
                primary_market=claimed.primary_market,
                enabled_markets=list(claimed.enabled_markets),
                bootstrap_state="running",
            )
            self.repository.begin_dispatch(db, claimed)
            db.commit()
        except BootstrapAlreadyRunning:
            db.rollback()
            raise
        except IntegrityError as exc:
            db.rollback()
            if _is_manifest_key_conflict(exc):
                raise BootstrapAlreadyRunning(
                    "Another bootstrap dispatch claimed ownership concurrently."
                ) from exc
            raise
        except Exception:
            db.rollback()
            raise
        return claimed

    def finish_market(
        self,
        db: Session,
        *,
        dispatch_id: str | None,
        completion: BootstrapMarketCompletion,
    ) -> BootstrapRunManifest:
        market = str(completion.market).upper()
        previous_outcome: BootstrapMarketOutcome | None = None
        balanced_activation_completed = (
            completion.expected_formula_version == BALANCED_RS_FORMULA_VERSION
        )

        try:

            def finish(current: BootstrapRunManifest) -> BootstrapRunManifest:
                nonlocal previous_outcome
                previous_outcome = current.market_outcome(market)
                return current.finish_market(
                    market=market,
                    succeeded=completion.succeeded,
                    balanced_activation_completed=balanced_activation_completed,
                )

            updated = self.repository.update_dispatch(
                db,
                dispatch_id=dispatch_id,
                transform=finish,
            )
            effective_outcome = updated.market_outcome(market)
            if effective_outcome is None:
                raise RuntimeError(
                    f"Bootstrap market {market} has no terminal outcome after completion."
                )
            if completion.primary and previous_outcome is None:
                preferences = get_runtime_preferences(db)
                stage_runtime_preferences(
                    db,
                    primary_market=preferences.primary_market,
                    enabled_markets=preferences.enabled_markets,
                    bootstrap_state=(
                        "ready"
                        if effective_outcome == BootstrapMarketOutcome.COMPLETED
                        else "failed"
                    ),
                )
            if (
                previous_outcome is None
                and effective_outcome == BootstrapMarketOutcome.FAILED
            ):
                failure_kwargs = {
                    "market": market,
                    "lifecycle": "bootstrap",
                    "task_name": "runtime_bootstrap",
                    "task_id": None,
                    "message": completion.failure_message or "Bootstrap failed",
                }
                if completion.failure_stage_key is None:
                    stage_current_market_activity_failed(db, **failure_kwargs)
                else:
                    stage_market_activity_failed(
                        db,
                        stage_key=completion.failure_stage_key,
                        **failure_kwargs,
                    )
            db.commit()
        except Exception:
            db.rollback()
            raise
        return updated

    def update_manifest(
        self,
        db: Session,
        *,
        manifest: BootstrapRunManifest,
    ) -> BootstrapRunManifest:
        if manifest.dispatch_id is None:
            raise ValueError("Bootstrap manifest updates require a dispatch_id.")
        try:
            updated = self.repository.update_dispatch(
                db,
                dispatch_id=manifest.dispatch_id,
                transform=lambda current: replace(
                    current,
                    primary_market=manifest.primary_market,
                    enabled_markets=manifest.enabled_markets,
                    primary_task_id=manifest.primary_task_id,
                    market_task_ids=manifest.market_task_ids,
                    queue_state=manifest.queue_state,
                    queued_at=datetime.now(timezone.utc).isoformat(),
                    ownership_expires_at=(
                        manifest.with_renewed_ownership().ownership_expires_at
                    ),
                ),
            )
            if updated.queue_state == BootstrapQueueState.FAILED:
                preferences = get_runtime_preferences(db)
                stage_runtime_preferences(
                    db,
                    primary_market=preferences.primary_market,
                    enabled_markets=preferences.enabled_markets,
                    bootstrap_state="failed",
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
        return updated


class PersistedBootstrapDispatchStore:
    """Open one short transaction for each queue-publication transition."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        lifecycle: BootstrapDispatchLifecycle | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.lifecycle = lifecycle or BootstrapDispatchLifecycle()

    def claim(self, manifest: BootstrapRunManifest) -> BootstrapRunManifest:
        db = self.session_factory()
        try:
            return self.lifecycle.claim(db, manifest=manifest)
        finally:
            db.close()

    def update(self, manifest: BootstrapRunManifest) -> BootstrapRunManifest:
        db = self.session_factory()
        try:
            return self.lifecycle.update_manifest(db, manifest=manifest)
        finally:
            db.close()


__all__ = [
    "BootstrapDispatchLifecycle",
    "BootstrapMarketCompletion",
    "PersistedBootstrapDispatchStore",
]
