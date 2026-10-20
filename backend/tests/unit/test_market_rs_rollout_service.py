"""Guarded balanced Market RS rollout tests."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.config import settings
from app.domain.feature_store.models import RunStatus
from app.domain.relative_strength import (
    BALANCED_RS_FORMULA_VERSION,
    BALANCED_RS_PRICE_BASIS,
    BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
    HORIZON_SESSIONS,
)
from app.models.stock import StockPrice
from app.services.market_rs_activation_coverage import MarketRsActivationCoverage
from app.services.market_rs_inputs import MarketRsInputUnavailable
from app.services.market_rs_rollout_contracts import (
    MarketRsActivationArtifactPolicy,
)
from app.services.market_rs_rollout_service import (
    ActivationValidationReport,
    MarketRsActivationRejected,
    MarketRsRolloutService,
)
from app.services.market_rs_static_artifact_validator import (
    MarketRsStaticArtifactValidator,
)


def _service(
    *,
    calendar=None,
    loader=None,
    repository=None,
    snapshot=None,
    groups=None,
    feature_factory=None,
):
    kwargs = {}
    if feature_factory is not None:
        kwargs["feature_run_repository_factory"] = feature_factory
    return MarketRsRolloutService(
        calendar_service=calendar or MagicMock(),
        input_loader=loader or MagicMock(),
        market_rs_snapshot_service=snapshot or MagicMock(),
        market_rs_repository=repository or MagicMock(),
        canonical_group_service=groups or MagicMock(),
        **kwargs,
    )


def test_candidate_dates_start_at_first_probe_with_two_eligible_stocks():
    sessions = [date(2026, 4, 8), date(2026, 4, 9), date(2026, 4, 10)]
    calendar = MagicMock()
    calendar.trading_days.return_value = sessions
    loader = MagicMock()
    loader.load.side_effect = [
        MarketRsInputUnavailable(
            "too early",
            reason_code="session_anchors_unavailable",
            diagnostics={},
        ),
        SimpleNamespace(excess_returns_by_symbol={"AAA": {}, "BBB": {}}),
    ]
    service = _service(calendar=calendar, loader=loader)
    service.backfill_service._earliest_available_price_date = (  # type: ignore[method-assign]
        lambda _db, _market: sessions[0]
    )

    assert (
        service.earliest_backfillable_date(
            MagicMock(),
            market="us",
            through_date=sessions[-1],
        )
        == sessions[1]
    )
    assert service.candidate_dates(
        MagicMock(),
        market="US",
        through_date=sessions[-1],
        first_valid_date=sessions[1],
    ) == (sessions[1], sessions[2])


def test_earliest_backfillable_probe_starts_at_activation_coverage() -> None:
    coverage_start = date(2026, 1, 23)
    through_date = date(2026, 7, 29)
    calendar = MagicMock()
    calendar.trading_days.return_value = [coverage_start, through_date]
    loader = MagicMock()
    loader.load.return_value = SimpleNamespace(
        excess_returns_by_symbol={"AAA": {}, "BBB": {}},
    )
    service = _service(calendar=calendar, loader=loader)
    service.backfill_service._earliest_available_price_date = (  # type: ignore[method-assign]
        lambda _db, _market: date(2024, 1, 2)
    )

    assert (
        service.earliest_backfillable_date(
            MagicMock(),
            market="US",
            through_date=through_date,
            probe_start_date=coverage_start,
        )
        == coverage_start
    )
    calendar.trading_days.assert_called_once_with(
        "US",
        coverage_start,
        through_date,
    )


def test_earliest_backfillable_date_does_not_hide_unexpected_loader_errors():
    session = date(2026, 4, 10)
    calendar = MagicMock()
    calendar.trading_days.return_value = [session]
    loader = MagicMock()
    loader.load.side_effect = RuntimeError("database connection lost")
    service = _service(calendar=calendar, loader=loader)
    service.backfill_service._earliest_available_price_date = (  # type: ignore[method-assign]
        lambda _db, _market: session
    )

    with pytest.raises(RuntimeError, match="database connection lost"):
        service.earliest_backfillable_date(
            MagicMock(),
            market="US",
            through_date=session,
        )


def test_rollout_service_resolves_bootstrap_through_date_to_nearby_benchmark_data(
    db_session,
) -> None:
    selected_date = date(2026, 7, 30)
    anchors = {
        0: selected_date,
        21: date(2026, 6, 30),
        63: date(2026, 4, 29),
        126: date(2026, 1, 29),
        189: date(2025, 10, 29),
        252: date(2025, 7, 30),
    }
    calendar = MagicMock()
    calendar.session_anchors.return_value = anchors
    service = _service(calendar=calendar)
    db_session.add_all(
        [
            StockPrice(
                symbol="^HSI",
                date=date(2026, 7, 31),
                close=24600.0,
                adj_close=None,
            ),
            *(
                StockPrice(
                    symbol="^HSI",
                    date=anchor_date,
                    close=24500.0,
                    adj_close=24500.0,
                )
                for anchor_date in set(anchors.values())
            ),
            StockPrice(
                symbol="2800.HK",
                date=date(2026, 7, 29),
                close=24.0,
                adj_close=24.0,
            ),
        ]
    )
    db_session.commit()

    resolution = service.resolve_bootstrap_through_date(
        db_session,
        market="HK",
        requested_through_date=date(2026, 7, 31),
    )

    assert resolution.market == "HK"
    assert resolution.requested_through_date == date(2026, 7, 31)
    assert resolution.selected_through_date == selected_date
    assert resolution.benchmark_through_date == selected_date
    assert resolution.benchmark_lag_days == 1
    assert resolution.reason_code == "benchmark_ready_lag"
    calendar.session_anchors.assert_called_once_with(
        "HK",
        selected_date,
        offsets=tuple(HORIZON_SESSIONS.values()),
    )


def test_rollout_service_counts_bootstrap_benchmark_lag_in_market_sessions(
    db_session,
    monkeypatch,
) -> None:
    requested_date = date(2026, 9, 8)
    selected_date = date(2026, 9, 4)
    anchors = {
        0: selected_date,
        21: date(2026, 8, 6),
        63: date(2026, 6, 9),
        126: date(2026, 3, 9),
        189: date(2025, 12, 5),
        252: date(2025, 9, 4),
    }
    calendar = MagicMock()
    calendar.trading_days.return_value = [selected_date, requested_date]
    calendar.session_anchors.return_value = anchors
    monkeypatch.setattr(settings, "market_rs_bootstrap_benchmark_max_lag_days", 1)
    service = _service(calendar=calendar)
    db_session.add_all(
        [
            *(
                StockPrice(
                    symbol="^HSI",
                    date=anchor_date,
                    close=24500.0,
                    adj_close=24500.0,
                )
                for anchor_date in set(anchors.values())
            ),
        ]
    )
    db_session.commit()

    resolution = service.resolve_bootstrap_through_date(
        db_session,
        market="HK",
        requested_through_date=requested_date,
    )

    assert resolution.selected_through_date == selected_date
    assert resolution.benchmark_through_date == selected_date
    assert resolution.benchmark_lag_days == 1
    assert resolution.reason_code == "benchmark_ready_lag"


def test_rollout_service_bounds_bootstrap_benchmark_ready_date_search(
    db_session,
    monkeypatch,
) -> None:
    requested_date = date(2026, 8, 1)
    recent_date = date(2026, 7, 31)
    stale_date = date(2026, 7, 20)
    stale_anchors = {
        0: stale_date,
        21: date(2026, 6, 19),
        63: date(2026, 4, 20),
        126: date(2026, 1, 20),
        189: date(2025, 10, 20),
        252: date(2025, 7, 21),
    }
    calendar = MagicMock()
    calendar.session_anchors.return_value = {
        0: recent_date,
        21: date(2026, 6, 30),
        63: date(2026, 4, 29),
        126: date(2026, 1, 29),
        189: date(2025, 10, 29),
        252: date(2025, 7, 30),
    }
    monkeypatch.setattr(settings, "market_rs_bootstrap_benchmark_max_lag_days", 3)
    service = _service(calendar=calendar)
    db_session.add_all(
        [
            StockPrice(
                symbol="^HSI",
                date=recent_date,
                close=24600.0,
                adj_close=24600.0,
            ),
            *(
                StockPrice(
                    symbol="^HSI",
                    date=anchor_date,
                    close=24000.0,
                    adj_close=24000.0,
                )
                for anchor_date in set(stale_anchors.values())
            ),
        ]
    )
    db_session.commit()

    resolution = service.resolve_bootstrap_through_date(
        db_session,
        market="HK",
        requested_through_date=requested_date,
    )

    assert resolution.selected_through_date == requested_date
    assert resolution.benchmark_through_date is None
    assert resolution.reason_code == "benchmark_date_unavailable"
    calendar.session_anchors.assert_called_once_with(
        "HK",
        recent_date,
        offsets=tuple(HORIZON_SESSIONS.values()),
    )


def test_backfill_resumes_completed_stock_run_and_reports_all_failures(monkeypatch):
    dates = (date(2026, 4, 8), date(2026, 4, 9), date(2026, 4, 10))
    completed_run = SimpleNamespace(id=10, eligible_symbol_count=2)
    retried_run = SimpleNamespace(id=11, eligible_symbol_count=2)
    absent_run = SimpleNamespace(id=12, eligible_symbol_count=2)
    repository = MagicMock()
    repository.get_completed_exact.side_effect = [completed_run, None, None]
    snapshot = MagicMock()
    snapshot.calculate.side_effect = [completed_run, retried_run, absent_run]
    groups = MagicMock()
    groups.calculate_and_store.side_effect = [
        [{"market_rs_run_id": 10}],
        RuntimeError("group aggregation failed"),
        [{"market_rs_run_id": 12}, {"market_rs_run_id": 12}],
    ]
    service = _service(repository=repository, snapshot=snapshot, groups=groups)
    monkeypatch.setattr(
        service.backfill_service,
        "earliest_backfillable_date",
        lambda *a, **k: dates[0],
    )
    monkeypatch.setattr(
        service.backfill_service,
        "candidate_dates",
        lambda *a, **k: dates,
    )

    report = service.backfill(MagicMock(), market="US", through_date=dates[-1])

    assert report.candidate_count == 3
    assert report.completed_count == 2
    assert report.failed_count == 1
    assert report.latest_run_id == 12
    assert [item.as_of_date for item in report.results] == list(dates)
    assert report.results[1].reason_code == "group_calculation_failed"
    assert [
        call.kwargs["as_of_date"] for call in snapshot.calculate.call_args_list
    ] == [
        dates[0],
        dates[1],
        dates[2],
    ]
    assert all(
        call.kwargs["rebuild_incompatible"] is True
        for call in snapshot.calculate.call_args_list
    )


def test_backfill_labels_snapshot_rebuild_failure_as_stock_calculation(monkeypatch):
    calculation_date = date(2026, 4, 10)
    repository = MagicMock()
    repository.get_completed_exact.return_value = SimpleNamespace(
        id=10,
        eligible_symbol_count=2,
    )
    snapshot = MagicMock()
    snapshot.calculate.side_effect = RuntimeError("incompatible stock price basis")
    groups = MagicMock()
    service = _service(repository=repository, snapshot=snapshot, groups=groups)
    monkeypatch.setattr(
        service.backfill_service,
        "earliest_backfillable_date",
        lambda *a, **k: calculation_date,
    )
    monkeypatch.setattr(
        service.backfill_service,
        "candidate_dates",
        lambda *a, **k: (calculation_date,),
    )

    report = service.backfill(
        MagicMock(),
        market="US",
        through_date=calculation_date,
    )

    assert report.results[0].reason_code == "stock_calculation_runtime_error"
    groups.calculate_and_store.assert_not_called()


def test_backfill_reuses_completed_strict_run_without_fallback_rebuild(monkeypatch):
    calculation_date = date(2026, 4, 10)
    run = SimpleNamespace(
        id=42,
        eligible_symbol_count=2,
        diagnostics_json={
            "price_basis": BALANCED_RS_PRICE_BASIS,
            "rs_snapshot_schema_version": BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
        },
    )
    repository = MagicMock()
    repository.get_completed_exact.return_value = run
    snapshot = MagicMock()
    snapshot.calculate.side_effect = AssertionError(
        "fallback rebuild must not run for an existing strict snapshot"
    )
    groups = MagicMock()
    groups.calculate_and_store.return_value = [{"market_rs_run_id": 42}]
    service = _service(repository=repository, snapshot=snapshot, groups=groups)
    monkeypatch.setattr(
        service.backfill_service,
        "earliest_backfillable_date",
        lambda *a, **k: calculation_date,
    )
    monkeypatch.setattr(
        service.backfill_service,
        "candidate_dates",
        lambda *a, **k: (calculation_date,),
    )

    report = service.backfill(
        MagicMock(),
        market="US",
        through_date=calculation_date,
    )

    assert report.completed_count == 1
    assert report.latest_run_id == 42
    snapshot.calculate.assert_not_called()
    groups.calculate_and_store.assert_called_once()


def test_backfill_rebuilds_completed_incompatible_price_basis(monkeypatch):
    calculation_date = date(2026, 4, 10)
    incompatible_run = SimpleNamespace(
        id=42,
        eligible_symbol_count=2,
        diagnostics_json={"price_basis": "legacy_close_only"},
    )
    rebuilt_run = SimpleNamespace(
        id=43,
        eligible_symbol_count=3,
        diagnostics_json={
            "price_basis": BALANCED_RS_PRICE_BASIS,
            "rs_snapshot_schema_version": BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
        },
    )
    repository = MagicMock()
    repository.get_completed_exact.return_value = incompatible_run
    snapshot = MagicMock()
    snapshot.calculate.return_value = rebuilt_run
    groups = MagicMock()
    groups.calculate_and_store.return_value = [{"market_rs_run_id": 43}]
    service = _service(repository=repository, snapshot=snapshot, groups=groups)
    monkeypatch.setattr(
        service.backfill_service,
        "earliest_backfillable_date",
        lambda *a, **k: calculation_date,
    )
    monkeypatch.setattr(
        service.backfill_service,
        "candidate_dates",
        lambda *a, **k: (calculation_date,),
    )
    db = MagicMock()

    report = service.backfill(
        db,
        market="US",
        through_date=calculation_date,
    )

    assert report.completed_count == 1
    assert report.latest_run_id == 43
    assert report.results[0].market_rs_run_id == 43
    assert report.results[0].eligible_symbol_count == 3
    snapshot.calculate.assert_called_once_with(
        db,
        market="US",
        as_of_date=calculation_date,
        formula_version=BALANCED_RS_FORMULA_VERSION,
        rebuild_incompatible=True,
    )
    groups.calculate_and_store.assert_called_once_with(
        db,
        market="US",
        as_of_date=calculation_date,
        formula_version=BALANCED_RS_FORMULA_VERSION,
    )


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(SoftTimeLimitExceeded(), id="soft-timeout"),
        pytest.param(
            OperationalError(
                "select 1",
                {},
                Exception("database system is not yet accepting connections"),
            ),
            id="transient-database-error",
        ),
    ],
)
def test_backfill_propagates_infrastructure_interruption(monkeypatch, error):
    calculation_date = date(2026, 4, 10)
    repository = MagicMock()
    repository.get_completed_exact.return_value = None
    snapshot = MagicMock()
    snapshot.calculate.side_effect = error
    service = _service(repository=repository, snapshot=snapshot)
    monkeypatch.setattr(
        service.backfill_service,
        "earliest_backfillable_date",
        lambda *a, **k: calculation_date,
    )
    monkeypatch.setattr(
        service.backfill_service,
        "candidate_dates",
        lambda *a, **k: (calculation_date,),
    )
    db = MagicMock()

    with pytest.raises(type(error)) as raised:
        service.backfill(db, market="US", through_date=calculation_date)

    assert raised.value is error
    db.rollback.assert_called_once_with()


def test_activation_backfill_attempts_every_required_date() -> None:
    required_dates = (date(2026, 1, 23), date(2026, 7, 29))
    latest_run = SimpleNamespace(id=42, eligible_symbol_count=2)
    repository = MagicMock()
    repository.get_completed_exact.return_value = None
    snapshot = MagicMock()
    snapshot.calculate.side_effect = [
        MarketRsInputUnavailable(
            "missing anchors",
            reason_code="session_anchors_unavailable",
            diagnostics={},
        ),
        latest_run,
    ]
    groups = MagicMock()
    groups.calculate_and_store.return_value = [{"market_rs_run_id": 42}]
    service = _service(repository=repository, snapshot=snapshot, groups=groups)

    coverage = MarketRsActivationCoverage(
        market="US",
        through_date=required_dates[-1],
        required_dates=required_dates,
    )
    report = service.backfill_activation(
        MagicMock(),
        coverage=coverage,
    )

    assert [result.as_of_date for result in report.results] == list(required_dates)
    assert report.failed_count == 1
    assert report.results[0].reason_code == "session_anchors_unavailable"
    assert [
        call.kwargs["as_of_date"] for call in snapshot.calculate.call_args_list
    ] == list(required_dates)


def test_activation_validation_checks_every_required_date(
    monkeypatch, tmp_path
) -> None:
    required_dates = (date(2026, 1, 23), date(2026, 7, 29))
    service = _service()
    validate_run = MagicMock(side_effect=[None, SimpleNamespace(id=42)])
    monkeypatch.setattr(service.validator, "_validate_run_and_groups", validate_run)
    feature_repository = MagicMock()
    feature_repository.get_run.side_effect = LookupError("not needed")
    service.validator.feature_run_repository_factory = lambda _db: feature_repository

    coverage = MarketRsActivationCoverage(
        market="US",
        through_date=required_dates[-1],
        required_dates=required_dates,
    )
    validation = service.validate_activation(
        MagicMock(),
        coverage=coverage,
        feature_run_id=99,
        static_staging_dir=tmp_path,
    )

    assert [
        call.kwargs["calculation_date"] for call in validate_run.call_args_list
    ] == list(required_dates)
    assert validation.first_valid_date == required_dates[0]
    assert validation.candidate_count == 2


def test_live_activation_validation_records_rrg_builder_failure(monkeypatch):
    through_date = date(2026, 4, 10)
    run = SimpleNamespace(
        id=42,
        universe_hash="universe-a",
        eligible_symbol_count=2,
    )
    feature_repository = MagicMock()
    feature_repository.get_run.return_value = SimpleNamespace(
        id=99,
        status=RunStatus.PUBLISHED,
        as_of_date=through_date,
        universe_hash="feature-a",
        config={
            "market": "US",
            "rs_formula_version": BALANCED_RS_FORMULA_VERSION,
            "market_rs_run_id": 42,
            "rs_as_of_date": "2026-04-10",
            "rs_universe_size": 2,
        },
    )
    service = _service(feature_factory=lambda _db: feature_repository)
    monkeypatch.setattr(
        service.validator,
        "_validate_run_and_groups",
        lambda *args, **kwargs: run,
    )

    class _BrokenRRGSource:
        def __init__(self, **_kwargs):
            pass

        def build(self, **_kwargs):
            raise SQLAlchemyError("connection failed")

    monkeypatch.setattr(
        "app.services.market_rs_activation_validator."
        "StaticGroupsRRGDatabasePayloadSource",
        _BrokenRRGSource,
    )

    validation = service.validate_activation(
        MagicMock(),
        coverage=MarketRsActivationCoverage(
            market="US",
            through_date=through_date,
            required_dates=(through_date,),
        ),
        feature_run_id=99,
        static_staging_dir=None,
        artifact_policy=MarketRsActivationArtifactPolicy.LIVE_RUNTIME,
    )

    assert validation.ok is False
    assert validation.errors == ("Balanced RRG validation failed: connection failed",)
    assert validation.rrg_status is None


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(SoftTimeLimitExceeded(), id="soft-timeout"),
        pytest.param(
            OperationalError(
                "select 1",
                {},
                Exception("database connection dropped"),
            ),
            id="database-interruption",
        ),
    ],
)
def test_static_activation_validation_propagates_infrastructure_interruption(
    monkeypatch,
    tmp_path,
    error,
):
    through_date = date(2026, 4, 10)
    run = SimpleNamespace(
        id=42,
        universe_hash="universe-a",
        eligible_symbol_count=2,
    )
    feature_repository = MagicMock()
    feature_repository.get_run.return_value = SimpleNamespace(
        id=99,
        status=RunStatus.PUBLISHED,
        as_of_date=through_date,
        universe_hash="feature-a",
        config={
            "market": "US",
            "rs_formula_version": BALANCED_RS_FORMULA_VERSION,
            "market_rs_run_id": 42,
            "rs_as_of_date": "2026-04-10",
            "rs_universe_size": 2,
        },
    )
    service = _service(feature_factory=lambda _db: feature_repository)
    monkeypatch.setattr(
        service.validator,
        "_validate_run_and_groups",
        lambda *args, **kwargs: run,
    )
    service.validator.static_validator = MagicMock()
    service.validator.static_validator.validate.side_effect = error

    with pytest.raises(type(error)) as raised:
        service.validate_activation(
            MagicMock(),
            coverage=MarketRsActivationCoverage(
                market="US",
                through_date=through_date,
                required_dates=(through_date,),
            ),
            feature_run_id=99,
            static_staging_dir=tmp_path,
        )

    assert raised.value is error


def test_backfill_completes_without_groups_when_market_lacks_capability(monkeypatch):
    calculation_date = date(2026, 4, 10)
    run = SimpleNamespace(id=42, eligible_symbol_count=2)
    repository = MagicMock()
    repository.get_completed_exact.return_value = run
    snapshot = MagicMock()
    snapshot.calculate.return_value = run
    groups = MagicMock()
    groups.calculate_and_store.return_value = []
    service = _service(repository=repository, snapshot=snapshot, groups=groups)
    monkeypatch.setattr(
        service.backfill_service,
        "earliest_backfillable_date",
        lambda *a, **k: calculation_date,
    )
    monkeypatch.setattr(
        service.backfill_service,
        "candidate_dates",
        lambda *a, **k: (calculation_date,),
    )

    report = service.backfill(
        MagicMock(),
        market="DE",
        through_date=calculation_date,
    )

    assert report.completed_count == 1
    assert report.failed_count == 0
    assert report.latest_run_id == 42
    assert report.group_row_count == 0
    assert report.results[0].group_market_rs_run_id is None
    groups.calculate_and_store.assert_not_called()


def test_activation_validation_skips_groups_when_market_lacks_capability(
    monkeypatch,
):
    calculation_date = date(2026, 4, 10)
    run = SimpleNamespace(id=42, eligible_symbol_count=0, rows=[])
    repository = MagicMock()
    repository.get_completed_exact.return_value = run
    service = _service(repository=repository)
    monkeypatch.setattr(
        "app.services.market_rs_activation_validator."
        "balanced_run_has_current_snapshot_contract",
        lambda _run: True,
    )
    monkeypatch.setattr(
        "app.services.market_rs_activation_validator."
        "IBDIndustryService.get_group_memberships",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("group memberships must not be loaded")
        ),
    )
    errors: list[str] = []

    result = service.validator._validate_run_and_groups(
        MagicMock(),
        market="DE",
        calculation_date=calculation_date,
        errors=errors,
    )

    assert result is run
    assert errors == []


def test_rejected_activation_rolls_back_without_moving_either_pointer(tmp_path):
    repository = MagicMock()
    feature_repository = MagicMock()
    service = _service(repository=repository)
    db = MagicMock()
    validation = ActivationValidationReport(
        market="US",
        formula_version=BALANCED_RS_FORMULA_VERSION,
        through_date=date(2026, 4, 10),
        first_valid_date=date(2026, 4, 8),
        candidate_count=3,
        latest_market_rs_run_id=42,
        latest_universe_hash="universe-a",
        feature_run_id=99,
        feature_universe_hash="feature-a",
        static_bundle_sha256="bundle-a",
        errors=("candidate trading-date gap",),
    )

    with pytest.raises(MarketRsActivationRejected, match="candidate trading-date gap"):
        service.activate(
            db,
            market="US",
            formula_version=BALANCED_RS_FORMULA_VERSION,
            feature_run_id=99,
            validation=validation,
            static_staging_dir=tmp_path,
        )

    feature_repository.repoint_published.assert_not_called()
    db.commit.assert_not_called()


def test_activation_rejects_manifest_changed_after_validation(tmp_path):
    repository = MagicMock()
    service = _service(repository=repository)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text('{"schema_version":"static-site-v3"}', encoding="utf-8")
    validated_hash = MarketRsStaticArtifactValidator.bundle_fingerprint(
        tmp_path,
        market="US",
    ).sha256
    manifest_path.write_text('{"schema_version":"changed"}', encoding="utf-8")
    validation = ActivationValidationReport(
        market="US",
        formula_version=BALANCED_RS_FORMULA_VERSION,
        through_date=date(2026, 4, 10),
        first_valid_date=date(2026, 4, 8),
        candidate_count=3,
        latest_market_rs_run_id=42,
        latest_universe_hash="universe-a",
        feature_run_id=99,
        feature_universe_hash="feature-a",
        static_bundle_sha256=validated_hash,
        errors=(),
    )

    with pytest.raises(MarketRsActivationRejected, match="changed after validation"):
        service.activate(
            MagicMock(),
            market="US",
            formula_version=BALANCED_RS_FORMULA_VERSION,
            feature_run_id=99,
            validation=validation,
            static_staging_dir=tmp_path,
        )


def test_activation_rejects_market_bundle_file_changed_after_validation(
    tmp_path,
    monkeypatch,
):
    repository = MagicMock()
    service = _service(repository=repository)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text('{"schema_version":"static-site-v3"}', encoding="utf-8")
    groups_path = tmp_path / "markets" / "us" / "groups.json"
    groups_path.parent.mkdir(parents=True)
    groups_path.write_text('{"rankings":[]}', encoding="utf-8")
    bundle_hash = MarketRsStaticArtifactValidator.bundle_fingerprint(
        tmp_path,
        market="US",
    ).sha256
    validation = ActivationValidationReport(
        market="US",
        formula_version=BALANCED_RS_FORMULA_VERSION,
        through_date=date(2026, 4, 10),
        first_valid_date=date(2026, 4, 8),
        candidate_count=3,
        latest_market_rs_run_id=42,
        latest_universe_hash="universe-a",
        feature_run_id=99,
        feature_universe_hash="feature-a",
        static_bundle_sha256=bundle_hash,
        errors=(),
    )
    monkeypatch.setattr(service.validator, "revalidate_static", lambda *a, **k: ())
    groups_path.write_text('{"rankings":[{"rank":1}]}', encoding="utf-8")

    with pytest.raises(MarketRsActivationRejected, match="bundle changed"):
        service.activate(
            MagicMock(),
            market="US",
            formula_version=BALANCED_RS_FORMULA_VERSION,
            feature_run_id=99,
            validation=validation,
            static_staging_dir=tmp_path,
        )


def test_validation_collects_feature_and_static_errors_without_short_circuiting(
    monkeypatch,
    tmp_path,
):
    through_date = date(2026, 4, 10)
    run = SimpleNamespace(
        id=42,
        universe_hash="universe-a",
        eligible_symbol_count=2,
        rows=[],
    )
    feature_repository = MagicMock()
    feature_repository.get_run.return_value = SimpleNamespace(
        id=99,
        status=SimpleNamespace(value="completed"),
        as_of_date=date(2026, 4, 9),
        universe_hash="feature-a",
        config={},
    )
    service = _service(feature_factory=lambda _db: feature_repository)
    monkeypatch.setattr(
        service.backfill_service,
        "earliest_backfillable_date",
        lambda *args, **kwargs: through_date,
    )
    monkeypatch.setattr(
        service.backfill_service,
        "candidate_dates",
        lambda *args, **kwargs: (through_date,),
    )
    monkeypatch.setattr(
        service.validator,
        "_validate_run_and_groups",
        lambda *args, **kwargs: run,
    )

    coverage = MarketRsActivationCoverage(
        market="US",
        through_date=through_date,
        required_dates=(through_date,),
    )
    validation = service.validate_activation(
        MagicMock(),
        coverage=coverage,
        feature_run_id=99,
        static_staging_dir=tmp_path / "missing-stage",
    )

    assert validation.ok is False
    assert any(
        "not published for the activation date" in error for error in validation.errors
    )
    assert any("rs_formula_version" in error for error in validation.errors)
    assert any(
        "Missing staged static-site-v3 manifest" in error for error in validation.errors
    )


def test_successful_activation_revalidates_then_commits_feature_pointer(
    monkeypatch, tmp_path
):
    events: list[str] = []
    repository = MagicMock()
    repository.get_completed_exact.return_value = SimpleNamespace(
        id=42,
        universe_hash="universe-a",
        eligible_symbol_count=2,
    )
    feature_repository = MagicMock()
    feature_repository.get_run.return_value = SimpleNamespace(
        id=99,
        status=SimpleNamespace(value="published"),
        universe_hash="feature-a",
        as_of_date=date(2026, 4, 10),
        config={
            "market": "US",
            "rs_formula_version": BALANCED_RS_FORMULA_VERSION,
            "market_rs_run_id": 42,
            "rs_as_of_date": "2026-04-10",
            "rs_universe_size": 2,
        },
    )
    feature_repository.repoint_published.side_effect = lambda *a, **k: events.append(
        "feature"
    )
    service = _service(
        repository=repository,
        feature_factory=lambda _db: feature_repository,
    )
    db = MagicMock()
    db.commit.side_effect = lambda: events.append("commit")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text('{"schema_version":"static-site-v3"}', encoding="utf-8")
    bundle_hash = MarketRsStaticArtifactValidator.bundle_fingerprint(
        tmp_path,
        market="US",
    ).sha256
    revalidate = MagicMock(return_value=())
    monkeypatch.setattr(service.validator, "revalidate_static", revalidate)
    runtime_revalidate = MagicMock(return_value=())
    monkeypatch.setattr(
        service.validator,
        "revalidate_runtime",
        runtime_revalidate,
    )
    validation = ActivationValidationReport(
        market="US",
        formula_version=BALANCED_RS_FORMULA_VERSION,
        through_date=date(2026, 4, 10),
        first_valid_date=date(2026, 4, 8),
        candidate_count=3,
        latest_market_rs_run_id=42,
        latest_universe_hash="universe-a",
        feature_run_id=99,
        feature_universe_hash="feature-a",
        static_bundle_sha256=bundle_hash,
        errors=(),
    )

    service.activate(
        db,
        market="US",
        formula_version=BALANCED_RS_FORMULA_VERSION,
        feature_run_id=99,
        validation=validation,
        static_staging_dir=tmp_path,
    )

    assert events == ["feature", "commit"]
    feature_repository.repoint_published.assert_called_once_with(
        99,
        pointer_key="latest_published_market:US",
    )
    revalidate.assert_called_once()
    runtime_revalidate.assert_called_once_with(
        db,
        market="US",
        through_date=date(2026, 4, 10),
    )


def test_live_activation_revalidates_runtime_groups_before_commit(monkeypatch):
    through_date = date(2026, 4, 10)
    repository = MagicMock()
    repository.get_completed_exact.return_value = SimpleNamespace(
        id=42,
        universe_hash="universe-a",
        eligible_symbol_count=2,
    )
    feature_repository = MagicMock()
    feature_repository.get_run.return_value = SimpleNamespace(
        id=99,
        status=SimpleNamespace(value="published"),
        universe_hash="feature-a",
        as_of_date=through_date,
        config={
            "market": "US",
            "rs_formula_version": BALANCED_RS_FORMULA_VERSION,
            "market_rs_run_id": 42,
            "rs_as_of_date": "2026-04-10",
            "rs_universe_size": 2,
        },
    )
    service = _service(
        repository=repository,
        feature_factory=lambda _db: feature_repository,
    )
    db = MagicMock()

    def reject_missing_group_rows(*_args, errors, **_kwargs):
        errors.append("Missing eligible Group rows for 2026-04-10: Software.")
        return repository.get_completed_exact.return_value

    monkeypatch.setattr(
        service.validator,
        "_validate_run_and_groups",
        reject_missing_group_rows,
    )
    validation = ActivationValidationReport(
        market="US",
        formula_version=BALANCED_RS_FORMULA_VERSION,
        through_date=through_date,
        first_valid_date=through_date,
        candidate_count=1,
        latest_market_rs_run_id=42,
        latest_universe_hash="universe-a",
        feature_run_id=99,
        feature_universe_hash="feature-a",
        static_bundle_sha256=None,
        errors=(),
        artifact_policy=MarketRsActivationArtifactPolicy.LIVE_RUNTIME,
    )

    with pytest.raises(MarketRsActivationRejected, match="Missing eligible Group rows"):
        service.activate(
            db,
            market="US",
            formula_version=BALANCED_RS_FORMULA_VERSION,
            feature_run_id=99,
            validation=validation,
            static_staging_dir=None,
        )

    db.commit.assert_not_called()
    db.rollback.assert_called_once_with()


def test_live_activation_revalidates_runtime_rrg_before_commit(monkeypatch):
    through_date = date(2026, 4, 10)
    repository = MagicMock()
    repository.get_completed_exact.return_value = SimpleNamespace(
        id=42,
        universe_hash="universe-a",
        eligible_symbol_count=2,
    )
    feature_repository = MagicMock()
    feature_repository.get_run.return_value = SimpleNamespace(
        id=99,
        status=SimpleNamespace(value="published"),
        universe_hash="feature-a",
        as_of_date=through_date,
        config={
            "market": "US",
            "rs_formula_version": BALANCED_RS_FORMULA_VERSION,
            "market_rs_run_id": 42,
            "rs_as_of_date": "2026-04-10",
            "rs_universe_size": 2,
        },
    )
    service = _service(
        repository=repository,
        feature_factory=lambda _db: feature_repository,
    )
    db = MagicMock()

    def accept_runtime_groups(*_args, **_kwargs):
        return repository.get_completed_exact.return_value

    def reject_rrg_payload(*_args, errors, **_kwargs):
        errors.append(
            "Balanced RRG history is insufficient for guarded activation: "
            "Only 11 usable weeks are available."
        )
        return "insufficient_balanced_history"

    monkeypatch.setattr(
        service.validator,
        "_validate_run_and_groups",
        accept_runtime_groups,
    )
    monkeypatch.setattr(service.validator, "_validate_live_rrg", reject_rrg_payload)
    validation = ActivationValidationReport(
        market="US",
        formula_version=BALANCED_RS_FORMULA_VERSION,
        through_date=through_date,
        first_valid_date=through_date,
        candidate_count=1,
        latest_market_rs_run_id=42,
        latest_universe_hash="universe-a",
        feature_run_id=99,
        feature_universe_hash="feature-a",
        static_bundle_sha256=None,
        errors=(),
        artifact_policy=MarketRsActivationArtifactPolicy.LIVE_RUNTIME,
        rrg_status="available",
    )

    with pytest.raises(MarketRsActivationRejected, match="Balanced RRG history"):
        service.activate(
            db,
            market="US",
            formula_version=BALANCED_RS_FORMULA_VERSION,
            feature_run_id=99,
            validation=validation,
            static_staging_dir=None,
        )

    db.commit.assert_not_called()
    db.rollback.assert_called_once_with()
