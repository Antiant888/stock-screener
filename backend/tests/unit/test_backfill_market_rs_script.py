"""Operator CLI tests for balanced Market RS rollout."""

from __future__ import annotations

from argparse import Namespace
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.domain.relative_strength import (
    BALANCED_RS_FORMULA_VERSION,
    GroupSnapshotIdentity,
)
from app.services.market_rs_rollout_contracts import (
    ActivationValidationReport,
    BackfillReport,
)
from app.services.market_rs_rollout_executor import (
    MarketRsActivationExecutionError,
    MarketRsActivationOutcome,
)


def _options(tmp_path: Path, *, activate: bool) -> Namespace:
    return Namespace(
        market="US",
        through_date=date(2026, 4, 10),
        start_date=None,
        static_staging_dir=tmp_path / "stage",
        activate=activate,
    )


def _activation_outcome(tmp_path: Path, *, formula_version: str):
    through_date = date(2026, 4, 10)
    return MarketRsActivationOutcome(
        backfill=BackfillReport(
            market="US",
            formula_version=formula_version,
            requested_start_date=through_date,
            through_date=through_date,
            first_valid_date=through_date,
            candidate_count=1,
            completed_count=1,
            failed_count=0,
            latest_run_id=99,
            group_row_count=1,
            results=(),
        ),
        market="US",
        formula_version=formula_version,
        feature_run_id=99,
        validation=ActivationValidationReport(
            market="US",
            formula_version=formula_version,
            through_date=through_date,
            first_valid_date=through_date,
            candidate_count=1,
            latest_market_rs_run_id=99,
            latest_universe_hash="universe",
            feature_run_id=99,
            feature_universe_hash="universe",
            static_bundle_sha256="bundle",
            errors=(),
        ),
        static_staging_dir=str((tmp_path / "stage").resolve()),
    )


def test_dry_run_prints_report_and_never_activates(monkeypatch, tmp_path):
    from app.scripts import backfill_market_rs as module

    rollout = MagicMock()
    rollout.backfill.return_value.to_dict.return_value = {"ok": True}
    monkeypatch.setattr(module, "get_market_rs_rollout_service", lambda: rollout)
    db = MagicMock()
    monkeypatch.setattr(module, "SessionLocal", lambda: db)

    result = module.execute_rollout(_options(tmp_path, activate=False))

    assert result == {"backfill": {"ok": True}, "activated": False}
    assert rollout.backfill.call_args.kwargs == {
        "market": "US",
        "through_date": date(2026, 4, 10),
        "start_date": None,
    }
    db.close.assert_called_once_with()


def test_activate_delegates_once_and_preserves_outcome(monkeypatch, tmp_path):
    from app.scripts import backfill_market_rs as module

    executor = MagicMock()
    executor.execute.return_value = _activation_outcome(
        tmp_path,
        formula_version="balanced-percentile-v1",
    )
    monkeypatch.setattr(module, "get_market_rs_activation_executor", lambda: executor)
    db = MagicMock()
    monkeypatch.setattr(module, "SessionLocal", lambda: db)

    result = module.execute_rollout(_options(tmp_path, activate=True))

    assert result["activated"] is True
    assert result["formula_version"] == "balanced-percentile-v1"
    executor.execute.assert_called_once()
    request = executor.execute.call_args.kwargs["request"]
    assert request.static_staging_dir == tmp_path / "stage"


def test_activate_rejects_shadow_resume_start_date(tmp_path: Path) -> None:
    from app.scripts import backfill_market_rs as module

    options = _options(tmp_path, activate=True)
    options.start_date = date(2026, 4, 1)

    with pytest.raises(
        module.RolloutCommandFailed,
        match="--start-date is only valid for shadow backfill",
    ):
        module.execute_rollout(options)


def test_publish_live_groups_does_not_duplicate_activation_cache_invalidation(
    monkeypatch,
):
    from app.services import group_rankings_cache, ui_snapshot_service
    from app.wiring import market_rs_activation as module

    bump_epoch = MagicMock()
    publish_bootstrap = MagicMock()
    monkeypatch.setattr(group_rankings_cache, "bump_group_rankings_epoch", bump_epoch)
    monkeypatch.setattr(
        ui_snapshot_service,
        "safe_publish_groups_bootstrap",
        publish_bootstrap,
    )

    module.publish_live_groups(
        GroupSnapshotIdentity(
            market="US",
            as_of_date=date(2026, 4, 10),
            formula_version=BALANCED_RS_FORMULA_VERSION,
        )
    )

    bump_epoch.assert_not_called()
    publish_bootstrap.assert_called_once_with(
        expected_through_date=date(2026, 4, 10),
    )


def test_publish_live_groups_logs_explicit_non_us_skip(monkeypatch):
    from app.wiring import market_rs_activation as module

    info = MagicMock()
    monkeypatch.setattr(module.logger, "info", info)

    module.publish_live_groups(
        GroupSnapshotIdentity(
            market="HK",
            as_of_date=date(2026, 4, 10),
            formula_version=BALANCED_RS_FORMULA_VERSION,
        )
    )

    info.assert_called_once()
    assert info.call_args.kwargs["extra"]["market"] == "HK"


def test_executor_failure_is_exposed_as_command_failure(monkeypatch, tmp_path):
    from app.scripts import backfill_market_rs as module

    executor = MagicMock()
    executor.execute.side_effect = MarketRsActivationExecutionError(
        "required backfill dates failed"
    )
    monkeypatch.setattr(module, "get_market_rs_activation_executor", lambda: executor)
    db = MagicMock()
    monkeypatch.setattr(module, "SessionLocal", lambda: db)

    with pytest.raises(
        module.RolloutCommandFailed, match="required backfill dates failed"
    ):
        module.execute_rollout(_options(tmp_path, activate=True))

    db.close.assert_called_once_with()


def test_main_initializes_runtime_services_before_running(monkeypatch):
    from app.scripts import backfill_market_rs as module

    calls = []
    monkeypatch.setattr(
        module,
        "initialize_process_runtime_services",
        lambda **kwargs: calls.append(("init", kwargs)),
    )
    monkeypatch.setattr(
        module,
        "execute_rollout",
        lambda options: calls.append(("run", options.market)) or {},
    )

    assert module.main(["--market", "KR", "--through-date", "2026-10-21"]) == 0
    assert calls == [
        ("init", {"session_factory": module.SessionLocal}),
        ("run", "KR"),
    ]
