"""Transactional ownership tests for runtime bootstrap dispatches."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.models.app_settings  # noqa: F401
from app.database import Base
from app.services.bootstrap_dispatch_lifecycle import (
    BootstrapDispatchLifecycle,
    BootstrapMarketCompletion,
)
from app.services.bootstrap_run_manifest import (
    BootstrapRunManifest,
    BootstrapRunManifestRepository,
)
from app.services.runtime_bootstrap_readiness import BALANCED_PUBLICATION_MISSING
from app.services.runtime_preferences_service import get_runtime_preferences


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_expired_dispatch_ownership_can_be_reclaimed(db_session) -> None:
    repository = BootstrapRunManifestRepository()
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    repository.begin_dispatch(
        db_session,
        BootstrapRunManifest.create(
            primary_market="US",
            enabled_markets=("US",),
            dispatch_id="dispatch-expired",
            queue_state="queueing",
            ownership_expires_at=expired_at.isoformat(),
        ),
    )
    db_session.commit()

    BootstrapDispatchLifecycle().claim(
        db_session,
        manifest=BootstrapRunManifest.create(
            primary_market="HK",
            enabled_markets=("HK",),
            dispatch_id="dispatch-current",
            queue_state="queueing",
        ),
    )

    current = repository.load(db_session)
    assert current is not None
    assert current.dispatch_id == "dispatch-current"
    assert current.ownership_expires_at is not None


def test_expired_dispatch_remains_current_until_it_is_reclaimed(db_session) -> None:
    repository = BootstrapRunManifestRepository()
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    repository.begin_dispatch(
        db_session,
        BootstrapRunManifest.create(
            primary_market="US",
            enabled_markets=("US",),
            dispatch_id="dispatch-current",
            queue_state="queued",
            ownership_expires_at=expired_at.isoformat(),
        ),
    )
    db_session.commit()

    assert repository.is_current_dispatch(
        db_session,
        dispatch_id="dispatch-current",
    )

    updated = BootstrapDispatchLifecycle(repository=repository).finish_market(
        db_session,
        dispatch_id="dispatch-current",
        completion=BootstrapMarketCompletion.ready(
            market="US",
            primary=True,
        ),
    )

    assert updated.queue_state.value == "completed"
    assert get_runtime_preferences(db_session).bootstrap_state == "ready"


def test_primary_failure_is_one_atomic_dispatch_transition(
    db_session, monkeypatch
) -> None:
    repository = BootstrapRunManifestRepository()
    BootstrapDispatchLifecycle().claim(
        db_session,
        manifest=BootstrapRunManifest.create(
            primary_market="US",
            enabled_markets=("US",),
            dispatch_id="dispatch-current",
            queue_state="queued",
        ),
    )

    commits = 0
    real_commit = db_session.commit

    def counted_commit() -> None:
        nonlocal commits
        commits += 1
        real_commit()

    monkeypatch.setattr(db_session, "commit", counted_commit)

    BootstrapDispatchLifecycle(repository=repository).finish_market(
        db_session,
        dispatch_id="dispatch-current",
        completion=BootstrapMarketCompletion.failed(
            market="US",
            primary=True,
            stage_key="core",
            message="Bootstrap core data incomplete",
        ),
    )

    manifest = repository.load(db_session)
    assert manifest is not None
    assert manifest.queue_state.value == "failed"
    assert manifest.failed_markets == ("US",)
    assert get_runtime_preferences(db_session).bootstrap_state == "failed"
    assert commits == 1


def test_balanced_completion_consumes_pending_activation_in_same_transition(
    db_session,
) -> None:
    repository = BootstrapRunManifestRepository()
    BootstrapDispatchLifecycle().claim(
        db_session,
        manifest=BootstrapRunManifest.create(
            primary_market="US",
            enabled_markets=("US", "HK"),
            dispatch_id="dispatch-current",
            fresh_install=True,
            queue_state="queued",
        ),
    )

    BootstrapDispatchLifecycle(repository=repository).finish_market(
        db_session,
        dispatch_id="dispatch-current",
        completion=BootstrapMarketCompletion.ready(
            market="US",
            primary=True,
            balanced_publication_committed=True,
        ),
    )

    manifest = repository.load(db_session)
    assert manifest is not None
    assert manifest.pending_balanced_activation_markets == ("HK",)
    assert manifest.completed_markets == ("US",)


def test_missing_balanced_publication_fails_the_market_and_keeps_it_pending(
    db_session,
) -> None:
    repository = BootstrapRunManifestRepository()
    lifecycle = BootstrapDispatchLifecycle(repository=repository)
    lifecycle.claim(
        db_session,
        manifest=BootstrapRunManifest.create(
            primary_market="US",
            enabled_markets=("US",),
            dispatch_id="dispatch-current",
            fresh_install=True,
            queue_state="queued",
        ),
    )

    updated = lifecycle.finish_market(
        db_session,
        dispatch_id="dispatch-current",
        completion=BootstrapMarketCompletion.failed(
            market="US",
            primary=True,
            stage_key=BALANCED_PUBLICATION_MISSING.stage_key,
            message=BALANCED_PUBLICATION_MISSING.activity_message,
        ),
    )

    assert updated.pending_balanced_activation_markets == ("US",)
    assert updated.completed_markets == ()
    assert updated.failed_markets == ("US",)
    assert get_runtime_preferences(db_session).bootstrap_state == "failed"


def test_first_terminal_market_outcome_wins_on_contradictory_redelivery(
    db_session,
) -> None:
    repository = BootstrapRunManifestRepository()
    lifecycle = BootstrapDispatchLifecycle(repository=repository)
    lifecycle.claim(
        db_session,
        manifest=BootstrapRunManifest.create(
            primary_market="US",
            enabled_markets=("US",),
            dispatch_id="dispatch-current",
            queue_state="queued",
        ),
    )

    lifecycle.finish_market(
        db_session,
        dispatch_id="dispatch-current",
        completion=BootstrapMarketCompletion.ready(market="US", primary=True),
    )
    updated = lifecycle.finish_market(
        db_session,
        dispatch_id="dispatch-current",
        completion=BootstrapMarketCompletion.failed(
            market="US",
            primary=True,
            stage_key="core",
            message="late contradictory callback",
        ),
    )

    assert updated.completed_markets == ("US",)
    assert updated.failed_markets == ()
    assert updated.queue_state.value == "completed"
    assert get_runtime_preferences(db_session).bootstrap_state == "ready"


def test_market_completion_rejects_a_market_outside_the_dispatch(db_session) -> None:
    repository = BootstrapRunManifestRepository()
    lifecycle = BootstrapDispatchLifecycle(repository=repository)
    lifecycle.claim(
        db_session,
        manifest=BootstrapRunManifest.create(
            primary_market="US",
            enabled_markets=("US",),
            dispatch_id="dispatch-current",
            queue_state="queued",
        ),
    )

    with pytest.raises(ValueError, match="enabled market"):
        lifecycle.finish_market(
            db_session,
            dispatch_id="dispatch-current",
            completion=BootstrapMarketCompletion.ready(
                market="HK",
                primary=False,
            ),
        )


def test_terminal_transition_rolls_back_every_staged_change(
    db_session, monkeypatch
) -> None:
    repository = BootstrapRunManifestRepository()
    BootstrapDispatchLifecycle().claim(
        db_session,
        manifest=BootstrapRunManifest.create(
            primary_market="US",
            enabled_markets=("US",),
            dispatch_id="dispatch-current",
            queue_state="queued",
        ),
    )
    original_update = repository.update_dispatch

    def fail_after_staging(*args, **kwargs):
        original_update(*args, **kwargs)
        raise RuntimeError("manifest persistence failed")

    monkeypatch.setattr(repository, "update_dispatch", fail_after_staging)

    with pytest.raises(RuntimeError, match="manifest persistence failed"):
        BootstrapDispatchLifecycle(repository=repository).finish_market(
            db_session,
            dispatch_id="dispatch-current",
            completion=BootstrapMarketCompletion.failed(
                market="US",
                primary=True,
                stage_key="core",
                message="Bootstrap core data incomplete",
            ),
        )

    db_session.expire_all()
    manifest = BootstrapRunManifestRepository().load(db_session)
    assert manifest is not None
    assert manifest.queue_state.value == "queued"
    assert manifest.failed_markets == ()
    assert get_runtime_preferences(db_session).bootstrap_state == "running"


def test_unrelated_integrity_error_is_not_reported_as_concurrent_dispatch(
    db_session, monkeypatch
) -> None:
    unrelated = IntegrityError(
        "insert runtime preference",
        {},
        Exception("NOT NULL constraint failed: app_settings.value"),
    )

    monkeypatch.setattr(
        "app.services.bootstrap_dispatch_lifecycle.stage_runtime_preferences",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(unrelated),
    )

    with pytest.raises(IntegrityError) as raised:
        BootstrapDispatchLifecycle().claim(
            db_session,
            manifest=BootstrapRunManifest.create(
                primary_market="US",
                enabled_markets=("US",),
                dispatch_id="dispatch-current",
                queue_state="queueing",
            ),
        )

    assert raised.value is unrelated
