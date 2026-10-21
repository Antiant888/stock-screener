from pathlib import Path

import yaml


_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _workflow():
    return yaml.safe_load(
        (_PROJECT_ROOT / ".github/workflows/market-calendar-audit.yml").read_text(
            encoding="utf-8"
        )
    )


def test_calendar_audit_workflow_runs_weekly_and_manually():
    workflow = _workflow()
    triggers = workflow.get("on") or workflow.get(True)

    assert "workflow_dispatch" in triggers
    assert len(triggers["schedule"]) == 1
    assert triggers["schedule"][0]["cron"] == "10 7 * * 6"
