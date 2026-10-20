from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from app.domain.relative_strength import (
    BALANCED_RS_PRICE_BASIS,
    BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
)
from app.services.bootstrap_publication_date import (
    resolve_bootstrap_publication_date,
)


def test_bootstrap_publication_date_uses_recent_balanced_run() -> None:
    repository = SimpleNamespace(
        list_completed_runs=lambda _db, **_kwargs: (
            SimpleNamespace(
                id=42,
                as_of_date=date(2026, 4, 9),
                diagnostics_json={
                    "price_basis": BALANCED_RS_PRICE_BASIS,
                    "rs_snapshot_schema_version": (
                        BALANCED_RS_SNAPSHOT_SCHEMA_VERSION
                    ),
                },
            ),
        ),
        get_latest_completed=lambda *_args, **_kwargs: None,
    )

    resolution = resolve_bootstrap_publication_date(
        object(),
        market="hk",
        requested_date=date(2026, 4, 10),
        repository=repository,
        max_lag_days=3,
    )

    assert resolution.market == "HK"
    assert resolution.selected_date == date(2026, 4, 9)
    assert resolution.market_rs_run_id == 42
    assert resolution.lag_days == 1
    assert resolution.reason_code == "balanced_run_selected"


def test_bootstrap_publication_date_skips_incompatible_newer_balanced_run() -> None:
    repository = SimpleNamespace(
        list_completed_runs=lambda _db, **_kwargs: (
            SimpleNamespace(
                id=43,
                as_of_date=date(2026, 4, 10),
                diagnostics_json={"price_basis": "legacy_close_only"},
            ),
            SimpleNamespace(
                id=42,
                as_of_date=date(2026, 4, 9),
                diagnostics_json={
                    "price_basis": BALANCED_RS_PRICE_BASIS,
                    "rs_snapshot_schema_version": (
                        BALANCED_RS_SNAPSHOT_SCHEMA_VERSION
                    ),
                },
            ),
        ),
        get_latest_completed=lambda *_args, **_kwargs: None,
    )

    resolution = resolve_bootstrap_publication_date(
        object(),
        market="HK",
        requested_date=date(2026, 4, 10),
        repository=repository,
        max_lag_days=3,
    )

    assert resolution.selected_date == date(2026, 4, 9)
    assert resolution.market_rs_run_id == 42
    assert resolution.lag_days == 1
    assert resolution.reason_code == "balanced_run_selected"


def test_bootstrap_publication_date_counts_lag_in_market_sessions() -> None:
    requested_date = date(2026, 9, 8)
    selected_date = date(2026, 9, 4)
    run = SimpleNamespace(
        id=42,
        as_of_date=selected_date,
        diagnostics_json={
            "price_basis": BALANCED_RS_PRICE_BASIS,
            "rs_snapshot_schema_version": BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
        },
    )

    def list_completed_runs(_db, **kwargs):
        return (run,) if kwargs["start_date"] <= selected_date else ()

    repository = SimpleNamespace(
        list_completed_runs=list_completed_runs,
        get_latest_completed=lambda *_args, **_kwargs: run,
    )
    calendar = SimpleNamespace(
        trading_days=lambda _market, _start, _end: [selected_date, requested_date],
    )

    resolution = resolve_bootstrap_publication_date(
        object(),
        market="US",
        requested_date=requested_date,
        repository=repository,
        calendar_service=calendar,
        max_lag_days=1,
    )

    assert resolution.selected_date == selected_date
    assert resolution.market_rs_run_id == 42
    assert resolution.lag_days == 1
    assert resolution.reason_code == "balanced_run_selected"


def test_bootstrap_publication_date_rejects_stale_balanced_run() -> None:
    repository = SimpleNamespace(
        list_completed_runs=lambda _db, **_kwargs: (),
        get_latest_completed=lambda _db, **_kwargs: SimpleNamespace(
            id=42,
            as_of_date=date(2026, 4, 1),
            diagnostics_json={
                "price_basis": BALANCED_RS_PRICE_BASIS,
                "rs_snapshot_schema_version": BALANCED_RS_SNAPSHOT_SCHEMA_VERSION,
            },
        ),
    )

    resolution = resolve_bootstrap_publication_date(
        object(),
        market="HK",
        requested_date=date(2026, 4, 10),
        repository=repository,
        max_lag_days=3,
    )

    assert resolution.selected_date == date(2026, 4, 10)
    assert resolution.market_rs_run_id == 42
    assert resolution.lag_days == 4
    assert resolution.reason_code == "balanced_run_lag_exceeds_policy"


def test_bootstrap_publication_date_rejects_incompatible_window_runs() -> None:
    repository = SimpleNamespace(
        list_completed_runs=lambda _db, **_kwargs: (
            SimpleNamespace(
                id=42,
                as_of_date=date(2026, 4, 10),
                diagnostics_json={},
            ),
        ),
        get_latest_completed=lambda *_args, **_kwargs: None,
    )

    resolution = resolve_bootstrap_publication_date(
        object(),
        market="HK",
        requested_date=date(2026, 4, 10),
        repository=repository,
        max_lag_days=3,
    )

    assert resolution.selected_date == date(2026, 4, 10)
    assert resolution.market_rs_run_id == 42
    assert resolution.lag_days == 0
    assert resolution.reason_code == "balanced_run_incompatible"


def test_bootstrap_publication_date_rejects_incompatible_latest_fallback_run() -> None:
    repository = SimpleNamespace(
        list_completed_runs=lambda _db, **_kwargs: (),
        get_latest_completed=lambda _db, **_kwargs: SimpleNamespace(
            id=42,
            as_of_date=date(2026, 4, 10),
            diagnostics_json={"price_basis": BALANCED_RS_PRICE_BASIS},
        ),
    )

    resolution = resolve_bootstrap_publication_date(
        object(),
        market="HK",
        requested_date=date(2026, 4, 10),
        repository=repository,
        max_lag_days=3,
    )

    assert resolution.selected_date == date(2026, 4, 10)
    assert resolution.market_rs_run_id == 42
    assert resolution.lag_days == 0
    assert resolution.reason_code == "balanced_run_incompatible"
