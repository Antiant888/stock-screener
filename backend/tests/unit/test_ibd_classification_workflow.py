"""IBD Classification workflow trigger/concurrency guardrails."""

from __future__ import annotations

import re
from pathlib import Path

import yaml


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SCHEDULED_MARKETS = ["US", "JP", "IN", "HK", "KR", "TW", "CN", "CA", "DE", "SG", "AU", "MY"]
_SCHEDULED_CRONS = [
    "30 14 * * 6",
    "30 17 * * 6",
    "30 20 * * 6",
    "30 23 * * 6",
    "30 3 * * 0",
    "30 6 * * 0",
    "30 9 * * 0",
    "30 12 * * 0",
    "30 15 * * 0",
    "30 18 * * 0",
    "30 21 * * 0",
    "30 0 * * 1",
]


def _workflow() -> dict:
    return yaml.safe_load(
        (_PROJECT_ROOT / ".github/workflows/ibd-classification.yml").read_text(
            encoding="utf-8"
        )
    )


def _select_markets_script(workflow: dict) -> str:
    return workflow["jobs"]["select_markets"]["steps"][0]["run"]


def test_ibd_workflow_uses_scheduled_single_market_runs_instead_of_workflow_run():
    workflow = _workflow()

    trigger = workflow[True]

    assert "workflow_run" not in trigger
    assert [entry["cron"] for entry in trigger["schedule"]] == _SCHEDULED_CRONS


def test_ibd_scheduled_crons_map_to_single_markets_in_order():
    workflow = _workflow()

    selector = _select_markets_script(workflow)

    for cron, market in zip(_SCHEDULED_CRONS, _SCHEDULED_MARKETS, strict=True):
        assert f'schedule:"{cron}") markets=\'["{market}"]\' ;;' in selector


def test_ibd_scheduled_crons_avoid_new_york_dst_transition_hour():
    workflow = _workflow()

    trigger = workflow[True]
    sunday_transition_hour_crons = [
        entry["cron"]
        for entry in trigger["schedule"]
        if entry.get("timezone") == "America/New_York"
        and re.match(r"\S+\s+2\s+\*\s+\*\s+0$", entry["cron"])
    ]

    assert sunday_transition_hour_crons == []


def test_ibd_classify_jobs_serialize_same_market_across_trigger_types():
    workflow = _workflow()

    classify_concurrency = workflow["jobs"]["classify"]["concurrency"]

    assert classify_concurrency["group"] == (
        "${{ github.workflow }}-classify-${{ matrix.market }}"
    )
    assert classify_concurrency["cancel-in-progress"] is False
