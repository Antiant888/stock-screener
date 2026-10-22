"""Operator CLI for shadow backfill and guarded balanced Market RS activation."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys
from typing import Any

from app.database import SessionLocal
from app.services.market_rs_rollout_executor import (
    MarketRsActivationExecutionError,
    MarketRsActivationRequest,
)
from app.wiring.bootstrap import (
    get_market_rs_activation_executor,
    get_market_rs_rollout_service,
    initialize_process_runtime_services,
)


class RolloutCommandFailed(RuntimeError):
    pass


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid ISO date: {value}") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill and optionally activate balanced Market RS",
    )
    parser.add_argument("--market", required=True)
    parser.add_argument("--through-date", required=True, type=_parse_date)
    parser.add_argument("--start-date", type=_parse_date)
    parser.add_argument("--static-staging-dir", type=Path)
    parser.add_argument("--activate", action="store_true")
    return parser


def execute_rollout(options: argparse.Namespace) -> dict[str, Any]:
    if options.activate and options.start_date is not None:
        raise RolloutCommandFailed(
            "--start-date is only valid for shadow backfill; activation uses "
            "the required bounded history window"
        )
    db = SessionLocal()
    try:
        if not options.activate:
            report = get_market_rs_rollout_service().backfill(
                db,
                market=options.market,
                through_date=options.through_date,
                start_date=options.start_date,
            )
            return {"backfill": report.to_dict(), "activated": False}
        if options.static_staging_dir is None:
            raise RolloutCommandFailed("--activate requires --static-staging-dir")
        try:
            outcome = get_market_rs_activation_executor().execute(
                db,
                request=MarketRsActivationRequest(
                    market=options.market,
                    through_date=options.through_date,
                    static_staging_dir=options.static_staging_dir,
                ),
            )
        except MarketRsActivationExecutionError as exc:
            raise RolloutCommandFailed(str(exc)) from exc
        return outcome.to_dict()
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    options = _build_parser().parse_args(argv)
    # Not prepare_runtime(): it migrates to head, and migration 0068 sends
    # operators here precisely when that upgrade is refused.
    initialize_process_runtime_services(session_factory=SessionLocal)
    try:
        payload = execute_rollout(options)
    except RolloutCommandFailed as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2))
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True))
    backfill = payload.get("backfill") or {}
    return 1 if int(backfill.get("failed_count", 0) or 0) > 0 else 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    sys.exit(main())
