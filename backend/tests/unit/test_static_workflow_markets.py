"""Static-site workflow market matrix coverage."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from app.domain.markets import market_registry


_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _static_build_market_steps() -> list[dict]:
    payload = yaml.safe_load(
        (_PROJECT_ROOT / ".github/workflows/static-site.yml").read_text(
            encoding="utf-8"
        )
    )
    return payload["jobs"]["build-market"]["steps"]


def _weekly_reference_matrix_markets(path: str) -> list[str]:
    """Parse the weekly-reference matrix from its ``|| '[...]'`` dispatch form."""
    content = (_PROJECT_ROOT / path).read_text(encoding="utf-8")
    match = re.search(r"\|\|\s*'(\[[^']+\])'", content)
    assert match is not None, f"{path} does not declare a market matrix"
    return list(json.loads(match.group(1)))


def _static_site_market_group(name: str) -> list[str]:
    """Parse a market-group JSON array assigned in the select-markets job.

    The static-site workflow no longer uses a literal matrix; build-market
    consumes ``fromJSON(needs.select-markets.outputs.markets)`` and the
    select-markets job derives the group from shell vars like ``ASIA='[...]'``.
    """
    content = (_PROJECT_ROOT / ".github/workflows/static-site.yml").read_text(encoding="utf-8")
    match = re.search(rf"^\s*{name}='(\[[^']*\])'", content, re.MULTILINE)
    assert match is not None, f"static-site.yml does not define a {name} market group"
    return list(json.loads(match.group(1)))


def test_static_site_all_group_and_weekly_reference_cover_supported_markets():
    expected = list(market_registry.supported_market_codes())

    # The dispatch "all" group must stay in lockstep with the registry so a
    # manual full rebuild covers every supported market in the canonical order.
    assert _static_site_market_group("ALL") == expected
    assert _weekly_reference_matrix_markets(".github/workflows/weekly-reference-data.yml") == expected


def test_static_site_schedule_groups_partition_supported_markets():
    asia = _static_site_market_group("ASIA")
    us = _static_site_market_group("US")
    all_markets = _static_site_market_group("ALL")

    # The two scheduled groups must contain the intended exact markets...
    assert asia == ["HK", "IN", "JP", "KR", "TW", "CN", "SG", "MY", "AU"]
    assert us == ["US", "CA", "DE"]

    # ...and together partition the full market set (disjoint + exhaustive), so
    # every supported market is published by exactly one scheduled run.
    assert set(asia).isdisjoint(us)
    assert set(asia) | set(us) == set(all_markets)
    assert set(all_markets) == set(market_registry.supported_market_codes())


def _build_market_step(name: str) -> dict:
    return next(step for step in _static_build_market_steps() if step.get("name") == name)


def _step_branch(predicate: str, step: str = "Export market static data bundle") -> str:
    """Return the shell lines guarded by ``predicate`` in a build-market step."""
    run = _build_market_step(step)["run"]
    assert predicate in run, predicate
    return re.split(r"\n\s*(?:else|fi)\n", run.split(predicate, 1)[1], maxsplit=1)[0]


def test_static_workflow_does_not_replace_rrg_history_after_restore_failure():
    publish = _build_market_step("Publish rolling RRG history")
    assert "steps.restore-rrg-history.outputs.safe_to_publish == 'true'" in publish["if"]


def test_static_workflow_never_advances_breadth_metadata_after_unsafe_restore():
    publish = _build_market_step("Publish breadth contributor metadata")
    assert "restore-breadth-contributor-metadata.outputs.safe_to_publish == 'true'" in publish["if"]


def test_static_workflow_publishes_breadth_metadata_before_market_artifact():
    names = [step.get("name") for step in _static_build_market_steps()]
    assert names.index("Publish breadth contributor metadata") < names.index("Upload market artifact")
    upload = _build_market_step("Upload market artifact")
    assert "publish-breadth-contributor-metadata.outputs.metadata_state_published" in upload["if"]


def test_market_export_failure_fails_the_job():
    assert not _build_market_step("Export market static data bundle").get("continue-on-error")


def test_failed_rrg_restore_keeps_the_last_good_market_artifact():
    branch = _step_branch('[ "$RRG_RESTORE_STATUS" = "failed" ]')
    assert "has_artifact=false" in branch
    assert "exit 0" in branch
    assert "steps.export-market.outputs.has_artifact == 'true'" in _build_market_step("Upload market artifact")["if"]


def test_rewound_export_never_publishes_rrg_history():
    branch = _step_branch("using (benchmark-backed|previous-session) as-of date")
    assert "rrg_history_publishable=false" in branch
    publish = _build_market_step("Publish rolling RRG history")
    assert "steps.export-market.outputs.rrg_history_publishable == 'true'" in publish["if"]


def test_incomplete_daily_price_bundle_is_never_uploaded():
    assert "--require-complete" in _build_market_step("Build daily price bundle")["run"]
    branch = _step_branch("is below required", "Build daily price bundle")
    assert "price_bundle_ready=false" in branch
    assert "price_bundle_ready=true" not in branch
    upload = _build_market_step("Upload daily price assets")
    assert "steps.build-daily-price-bundle.outputs.price_bundle_ready == 'true'" in upload["if"]


def test_breadth_history_is_restored_before_export_and_published_only_with_an_artifact():
    names = [step.get("name") for step in _static_build_market_steps()]
    assert names.index("Restore breadth history") < names.index("Export market static data bundle")
    publish = _build_market_step("Publish breadth history")
    assert "steps.export-market.outputs.has_artifact == 'true'" in publish["if"]


def test_weekly_reference_never_builds_one_market_twice_at_once():
    # The later job waits, so it seeds from the bundle (and fundamentals
    # cursor) the earlier one published.
    workflow = yaml.safe_load(
        (_PROJECT_ROOT / ".github/workflows/weekly-reference-data.yml").read_text(encoding="utf-8")
    )
    publish_concurrency = workflow["jobs"]["publish"]["concurrency"]
    assert publish_concurrency["group"] == "${{ github.workflow }}-publish-${{ matrix.market }}"
    assert publish_concurrency["cancel-in-progress"] is False
    # A third same-market job queues too instead of cancelling the pending one.
    assert publish_concurrency["queue"] == "max"
