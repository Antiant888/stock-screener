"""Resolve the publication date shared by implicit bootstrap stages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.config import settings
from app.domain.relative_strength import (
    BALANCED_RS_FORMULA_VERSION,
    balanced_run_has_current_snapshot_contract,
)
from app.infra.db.repositories.market_rs_repo import MarketRsRunRepository
from app.services.market_calendar_service import MarketCalendarService
from app.services.market_session_lag import market_session_lag, session_window_start


@dataclass(frozen=True)
class BootstrapPublicationDateResolution:
    market: str
    requested_date: date
    selected_date: date
    formula_version: str | None
    market_rs_run_id: int | None
    lag_days: int | None
    reason_code: str


def _max_lag_days(configured: int | None) -> int:
    if configured is None:
        configured = settings.market_rs_bootstrap_benchmark_max_lag_days
    return max(0, int(configured))


def _resolution(
    *,
    market: str,
    requested_date: date,
    selected_date: date,
    formula_version: str | None,
    reason_code: str,
    market_rs_run_id: int | None = None,
    lag_days: int | None = None,
) -> BootstrapPublicationDateResolution:
    return BootstrapPublicationDateResolution(
        market=market,
        requested_date=requested_date,
        selected_date=selected_date,
        formula_version=formula_version,
        market_rs_run_id=market_rs_run_id,
        lag_days=lag_days,
        reason_code=reason_code,
    )


def resolve_bootstrap_publication_date(
    db: Session,
    *,
    market: str,
    requested_date: date,
    repository: MarketRsRunRepository | None = None,
    calendar_service: MarketCalendarService | None = None,
    max_lag_days: int | None = None,
) -> BootstrapPublicationDateResolution:
    """Align implicit bootstrap stages to the balanced RS publication.

    Fresh bootstrap activation may intentionally publish balanced Market RS one
    or two sessions behind the calendar while benchmark data catches up. Later
    bootstrap stages that did not receive an explicit date must use that same
    publication date instead of independently resolving the calendar date.
    """

    normalized_market = str(market).strip().upper()
    repo = repository or MarketRsRunRepository()
    max_lag_sessions = _max_lag_days(max_lag_days)
    calendar = calendar_service or MarketCalendarService()
    window_start = session_window_start(
        calendar,
        market=normalized_market,
        through_date=requested_date,
        max_lag_sessions=max_lag_sessions,
    )
    completed_runs = repo.list_completed_runs(
        db,
        market=normalized_market,
        formula_version=BALANCED_RS_FORMULA_VERSION,
        start_date=window_start,
        through_date=requested_date,
    )
    latest_run = completed_runs[0] if completed_runs else None
    run = next(
        (
            candidate
            for candidate in completed_runs
            if balanced_run_has_current_snapshot_contract(candidate)
        ),
        None,
    )
    if run is None and latest_run is not None:
        return _resolution(
            market=normalized_market,
            requested_date=requested_date,
            selected_date=requested_date,
            formula_version=BALANCED_RS_FORMULA_VERSION,
            market_rs_run_id=getattr(latest_run, "id", None),
            lag_days=market_session_lag(
                calendar,
                market=normalized_market,
                start_date=latest_run.as_of_date,
                end_date=requested_date,
            ),
            reason_code="balanced_run_incompatible",
        )
    if run is None:
        latest_run = repo.get_latest_completed(
            db,
            market=normalized_market,
            formula_version=BALANCED_RS_FORMULA_VERSION,
            through_date=requested_date,
        )
        if latest_run is not None and not balanced_run_has_current_snapshot_contract(
            latest_run
        ):
            return _resolution(
                market=normalized_market,
                requested_date=requested_date,
                selected_date=requested_date,
                formula_version=BALANCED_RS_FORMULA_VERSION,
                market_rs_run_id=getattr(latest_run, "id", None),
                lag_days=market_session_lag(
                    calendar,
                    market=normalized_market,
                    start_date=latest_run.as_of_date,
                    end_date=requested_date,
                ),
                reason_code="balanced_run_incompatible",
            )
    if run is None and latest_run is None:
        return _resolution(
            market=normalized_market,
            requested_date=requested_date,
            selected_date=requested_date,
            formula_version=BALANCED_RS_FORMULA_VERSION,
            reason_code="balanced_run_unavailable",
        )
    if run is None:
        run = latest_run

    selected_date = run.as_of_date
    lag_days = market_session_lag(
        calendar,
        market=normalized_market,
        start_date=selected_date,
        end_date=requested_date,
    )
    if lag_days < 0 or lag_days > max_lag_sessions:
        return _resolution(
            market=normalized_market,
            requested_date=requested_date,
            selected_date=requested_date,
            formula_version=BALANCED_RS_FORMULA_VERSION,
            market_rs_run_id=getattr(run, "id", None),
            lag_days=lag_days,
            reason_code="balanced_run_lag_exceeds_policy",
        )

    return _resolution(
        market=normalized_market,
        requested_date=requested_date,
        selected_date=selected_date,
        formula_version=BALANCED_RS_FORMULA_VERSION,
        market_rs_run_id=getattr(run, "id", None),
        lag_days=lag_days,
        reason_code=(
            "requested_date_ready" if lag_days == 0 else "balanced_run_selected"
        ),
    )


def resolve_bootstrap_publication_date_with_session(
    session_factory: Callable[[], Session],
    *,
    market: str,
    requested_date: date,
) -> BootstrapPublicationDateResolution:
    db = session_factory()
    try:
        return resolve_bootstrap_publication_date(
            db,
            market=market,
            requested_date=requested_date,
        )
    finally:
        db.close()


__all__ = [
    "BootstrapPublicationDateResolution",
    "resolve_bootstrap_publication_date",
    "resolve_bootstrap_publication_date_with_session",
]
