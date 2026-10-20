"""Atomic Market RS + Feature pointer activation integration coverage."""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.services.market_rs_activator as activation_module
from app.domain.feature_store.models import RunStatus
from app.domain.relative_strength import (
    BALANCED_RS_FORMULA_VERSION,
    BALANCED_RS_PRICE_BASIS,
    BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
    GroupSnapshotIdentity,
)
from app.infra.db.models.feature_store import FeatureRun, FeatureRunPointer
from app.infra.db.models.relative_strength import MarketRsRun
from app.infra.db.repositories.feature_run_repo import SqlFeatureRunRepository
from app.infra.db.repositories.market_rs_repo import MarketRsRunRepository
from app.models.industry import IBDGroupRank
from app.services.market_rs_rollout_executor import (
    MarketRsActivationExecutor,
    MarketRsActivationRequest,
)
from app.services.market_rs_rollout_service import (
    ActivationValidationReport,
    MarketRsRolloutService,
)
from app.services.market_rs_static_artifact_validator import (
    MarketRsStaticArtifactValidator,
)


def _seed_activation_candidates(db_session):
    through_date = date(2026, 4, 10)
    rs_run = MarketRsRun(
        market="US",
        as_of_date=through_date,
        formula_version=BALANCED_RS_FORMULA_VERSION,
        status="completed",
        benchmark_symbol="SPY",
        benchmark_as_of_date=through_date,
        universe_hash="universe-a",
        expected_symbol_count=1,
        eligible_symbol_count=1,
        excluded_symbol_count=0,
        diagnostics_json={
            "price_basis": BALANCED_RS_PRICE_BASIS,
            "rs_snapshot_schema_version": BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
        },
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(rs_run)
    db_session.flush()
    db_session.add_all(
        [
            IBDGroupRank(
                market="US",
                industry_group="Software",
                date=through_date,
                rank=1,
                avg_rs_rating=91,
                avg_rs_rating_1m=89,
                avg_rs_rating_3m=86,
                num_stocks=10,
                rs_formula_version=BALANCED_RS_FORMULA_VERSION,
                market_rs_run_id=rs_run.id,
            ),
            IBDGroupRank(
                market="US",
                industry_group="Banks",
                date=through_date,
                rank=2,
                avg_rs_rating=75,
                avg_rs_rating_1m=73,
                avg_rs_rating_3m=70,
                num_stocks=8,
                rs_formula_version=BALANCED_RS_FORMULA_VERSION,
                market_rs_run_id=rs_run.id,
            ),
        ]
    )
    old_feature = FeatureRun(
        as_of_date=date(2026, 4, 9),
        run_type="daily_snapshot",
        status=RunStatus.PUBLISHED.value,
        universe_hash="legacy-feature",
        config_json={"market": "US", "rs_formula_version": "legacy-linear-v1"},
        published_at=datetime.now(timezone.utc),
    )
    candidate = FeatureRun(
        as_of_date=through_date,
        run_type="daily_snapshot",
        status=RunStatus.PUBLISHED.value,
        universe_hash="feature-a",
        config_json={
            "market": "US",
            "rs_formula_version": BALANCED_RS_FORMULA_VERSION,
            "market_rs_run_id": rs_run.id,
            "rs_as_of_date": through_date.isoformat(),
            "rs_universe_size": 1,
        },
        published_at=datetime.now(timezone.utc),
    )
    db_session.add_all([old_feature, candidate])
    db_session.flush()
    db_session.add(
        FeatureRunPointer(
            key="latest_published_market:US",
            run_id=old_feature.id,
        )
    )
    db_session.commit()
    return rs_run.id, candidate.id, old_feature.id


def _validation(
    rs_run_id: int,
    feature_run_id: int,
    manifest_hash: str,
) -> ActivationValidationReport:
    return ActivationValidationReport(
        market="US",
        formula_version=BALANCED_RS_FORMULA_VERSION,
        through_date=date(2026, 4, 10),
        first_valid_date=date(2026, 4, 10),
        candidate_count=1,
        latest_market_rs_run_id=rs_run_id,
        latest_universe_hash="universe-a",
        feature_run_id=feature_run_id,
        feature_universe_hash="feature-a",
        static_bundle_sha256=manifest_hash,
        errors=(),
    )


def _service(repository, feature_factory):
    return MarketRsRolloutService(
        calendar_service=MagicMock(),
        input_loader=MagicMock(),
        market_rs_snapshot_service=MagicMock(),
        market_rs_repository=repository,
        canonical_group_service=MagicMock(),
        feature_run_repository_factory=feature_factory,
    )


def test_activation_repoints_the_feature_pointer_in_one_commit(
    db_session, tmp_path, monkeypatch
):
    rs_run_id, candidate_id, _old_id = _seed_activation_candidates(db_session)
    service = _service(MarketRsRunRepository(), SqlFeatureRunRepository)
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"schema_version":"static-site-v3"}', encoding="utf-8")
    manifest_hash = MarketRsStaticArtifactValidator.bundle_fingerprint(
        tmp_path,
        market="US",
    ).sha256
    monkeypatch.setattr(
        service.validator, "revalidate_static", lambda *args, **kwargs: ()
    )
    monkeypatch.setattr(
        service.validator, "revalidate_runtime", lambda *args, **kwargs: ()
    )

    service.activate(
        db_session,
        market="US",
        formula_version=BALANCED_RS_FORMULA_VERSION,
        feature_run_id=candidate_id,
        validation=_validation(rs_run_id, candidate_id, manifest_hash),
        static_staging_dir=tmp_path,
    )

    db_session.expire_all()
    assert (
        db_session.get(
            FeatureRunPointer,
            "latest_published_market:US",
        ).run_id
        == candidate_id
    )


def test_activation_invalidates_group_cache_after_commit(
    db_session,
    tmp_path,
    monkeypatch,
):
    rs_run_id, candidate_id, _old_id = _seed_activation_candidates(db_session)
    service = _service(MarketRsRunRepository(), SqlFeatureRunRepository)
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"schema_version":"static-site-v3"}', encoding="utf-8")
    manifest_hash = MarketRsStaticArtifactValidator.bundle_fingerprint(
        tmp_path,
        market="US",
    ).sha256
    monkeypatch.setattr(
        service.validator, "revalidate_static", lambda *args, **kwargs: ()
    )
    monkeypatch.setattr(
        service.validator, "revalidate_runtime", lambda *args, **kwargs: ()
    )
    invalidations: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        activation_module,
        "bump_group_rankings_epoch",
        lambda market: invalidations.append((market, db_session.in_transaction())),
    )

    service.activate(
        db_session,
        market="US",
        formula_version=BALANCED_RS_FORMULA_VERSION,
        feature_run_id=candidate_id,
        validation=_validation(rs_run_id, candidate_id, manifest_hash),
        static_staging_dir=tmp_path,
    )

    assert invalidations == [("US", False)]


def test_failure_after_market_pointer_flush_rolls_back_both_pointers(
    db_session, tmp_path, monkeypatch
):
    rs_run_id, candidate_id, old_id = _seed_activation_candidates(db_session)

    class _FailingFeatureRepository(SqlFeatureRunRepository):
        def repoint_published(self, run_id, pointer_key="latest_published"):
            raise RuntimeError("pointer write failed")

    service = _service(MarketRsRunRepository(), _FailingFeatureRepository)
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"schema_version":"static-site-v3"}', encoding="utf-8")
    manifest_hash = MarketRsStaticArtifactValidator.bundle_fingerprint(
        tmp_path,
        market="US",
    ).sha256
    monkeypatch.setattr(
        service.validator, "revalidate_static", lambda *args, **kwargs: ()
    )
    monkeypatch.setattr(
        service.validator, "revalidate_runtime", lambda *args, **kwargs: ()
    )
    invalidations: list[str] = []
    monkeypatch.setattr(
        activation_module,
        "bump_group_rankings_epoch",
        invalidations.append,
    )

    with pytest.raises(RuntimeError, match="pointer write failed"):
        service.activate(
            db_session,
            market="US",
            formula_version=BALANCED_RS_FORMULA_VERSION,
            feature_run_id=candidate_id,
            validation=_validation(rs_run_id, candidate_id, manifest_hash),
            static_staging_dir=tmp_path,
        )

    db_session.expire_all()
    assert (
        db_session.get(
            FeatureRunPointer,
            "latest_published_market:US",
        ).run_id
        == old_id
    )
    assert invalidations == []


def test_shared_executor_activates_exact_group_and_history_identity(
    db_session,
    tmp_path,
    monkeypatch,
):
    from app.tasks import group_history_tasks

    rs_run_id, candidate_id, _old_id = _seed_activation_candidates(db_session)
    service = _service(MarketRsRunRepository(), SqlFeatureRunRepository)
    report = SimpleNamespace(
        ok=True,
        failed_count=0,
        to_dict=lambda: {"failed_count": 0},
    )
    monkeypatch.setattr(
        service,
        "activation_coverage",
        lambda **_kwargs: SimpleNamespace(
            market="US",
            through_date=date(2026, 4, 10),
            required_dates=(date(2026, 4, 10),),
        ),
    )
    monkeypatch.setattr(service, "backfill_activation", lambda *args, **kwargs: report)

    def _export_static(*, static_staging_dir, **_kwargs):
        (static_staging_dir / "manifest.json").write_text(
            '{"schema_version":"static-site-v3"}',
            encoding="utf-8",
        )

    def _validate(*args, static_staging_dir, **kwargs):
        manifest_hash = MarketRsStaticArtifactValidator.bundle_fingerprint(
            static_staging_dir,
            market="US",
        ).sha256
        return _validation(rs_run_id, candidate_id, manifest_hash)

    monkeypatch.setattr(service, "validate_activation", _validate)
    monkeypatch.setattr(
        service.validator,
        "revalidate_static",
        lambda *args, **kwargs: (),
    )
    monkeypatch.setattr(
        service.validator,
        "revalidate_runtime",
        lambda *args, **kwargs: (),
    )
    invalidations = MagicMock()
    live_publish = MagicMock()
    monkeypatch.setattr(
        activation_module,
        "bump_group_rankings_epoch",
        invalidations,
    )
    executor = MarketRsActivationExecutor(
        rollout_service=service,
        feature_snapshot_builder=lambda **_kwargs: candidate_id,
        static_exporter=_export_static,
        live_group_publisher=live_publish,
    )

    outcome = executor.execute(
        db_session,
        request=MarketRsActivationRequest(
            market="US",
            through_date=date(2026, 4, 10),
            static_staging_dir=tmp_path / "stage",
        ),
    )

    db_session.expire_all()
    repository = MarketRsRunRepository()
    assert outcome.market == "US"
    run = repository.get_completed_exact(
        db_session,
        market="US",
        as_of_date=date(2026, 4, 10),
        formula_version=BALANCED_RS_FORMULA_VERSION,
    )
    groups = (
        db_session.query(IBDGroupRank)
        .filter(
            IBDGroupRank.market == "US",
            IBDGroupRank.date == date(2026, 4, 10),
            IBDGroupRank.rs_formula_version == BALANCED_RS_FORMULA_VERSION,
        )
        .all()
    )
    assert run is not None
    assert groups
    assert {row.market_rs_run_id for row in groups} == {run.id}
    assert all(row.avg_rs_rating_1m is not None for row in groups)
    assert all(row.avg_rs_rating_3m is not None for row in groups)

    feature_pointer = db_session.get(
        FeatureRunPointer,
        "latest_published_market:US",
    )
    feature_run = db_session.get(FeatureRun, feature_pointer.run_id)
    assert feature_run.id == candidate_id
    assert feature_run.config_json == {
        "market": "US",
        "rs_formula_version": BALANCED_RS_FORMULA_VERSION,
        "market_rs_run_id": run.id,
        "rs_as_of_date": "2026-04-10",
        "rs_universe_size": 1,
    }

    calendar = MagicMock()
    calendar.last_completed_trading_day.return_value = date(2026, 4, 10)
    monkeypatch.setattr(
        group_history_tasks,
        "get_market_calendar_service",
        lambda: calendar,
    )
    target = group_history_tasks._resolve_current_group_history_target(
        db_session,
        market="US",
    )
    assert target.formula_version == BALANCED_RS_FORMULA_VERSION
    assert target.through_date == date(2026, 4, 10)
    invalidations.assert_called_once_with("US")
    live_publish.assert_called_once_with(
        GroupSnapshotIdentity(
            market="US",
            as_of_date=date(2026, 4, 10),
            formula_version=BALANCED_RS_FORMULA_VERSION,
        )
    )
