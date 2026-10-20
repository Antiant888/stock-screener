"""Transactional ownership tests for runtime bootstrap dispatches."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.models.app_settings  # noqa: F401
from app.database import Base
from app.domain.relative_strength import (
    BALANCED_RS_FORMULA_VERSION,
    BALANCED_RS_PRICE_BASIS,
    BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
)
from app.infra.db.models.feature_store import FeatureRun, FeatureRunPointer
from app.infra.db.models.relative_strength import MarketRsRun
from app.services.bootstrap_dispatch_lifecycle import (
    BootstrapDispatchLifecycle,
    BootstrapMarketCompletion,
)
from app.services.bootstrap_run_manifest import (
    BootstrapRunManifest,
    BootstrapRunManifestRepository,
)
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


def _publish_balanced_snapshot(db, market: str = "US") -> None:
    as_of = date(2026, 5, 1)
    rs_run = MarketRsRun(
        market=market,
        as_of_date=as_of,
        formula_version=BALANCED_RS_FORMULA_VERSION,
        status="completed",
        benchmark_symbol="SPY",
        benchmark_as_of_date=as_of,
        universe_hash="hash",
        expected_symbol_count=1,
        eligible_symbol_count=1,
        excluded_symbol_count=0,
        diagnostics_json={
            "price_basis": BALANCED_RS_PRICE_BASIS,
            "rs_snapshot_schema_version": BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
        },
    )
    db.add(rs_run)
    db.flush()
    feature_run = FeatureRun(
        as_of_date=as_of,
        run_type="daily_snapshot",
        status="published",
        published_at=datetime(2026, 5, 1, 21, 0, tzinfo=timezone.utc),
        config_json={
            "market": market,
            "rs_formula_version": BALANCED_RS_FORMULA_VERSION,
            "market_rs_run_id": rs_run.id,
            "rs_as_of_date": as_of.isoformat(),
            "rs_universe_size": 1,
        },
    )
    db.add(feature_run)
    db.flush()
    db.add(
        FeatureRunPointer(
            key=f"latest_published_market:{market}", run_id=feature_run.id
        )
    )
    db.commit()


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
    _publish_balanced_snapshot(db_session)

    BootstrapDispatchLifecycle(repository=repository).finish_market(
        db_session,
        dispatch_id="dispatch-current",
        completion=BootstrapMarketCompletion.ready(
            market="US",
            primary=True,
            expected_formula_version=BALANCED_RS_FORMULA_VERSION,
        ),
    )

    manifest = repository.load(db_session)
    assert manifest is not None
    assert manifest.pending_balanced_activation_markets == ("HK",)
    assert manifest.completed_markets == ("US",)


def test_missing_balanced_publication_preserves_pending_activation(db_session) -> None:
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
        completion=BootstrapMarketCompletion.ready(
            market="US",
            primary=True,
            expected_formula_version=BALANCED_RS_FORMULA_VERSION,
        ),
    )

    assert updated.pending_balanced_activation_markets == ("US",)
    assert updated.completed_markets == ("US",)


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
