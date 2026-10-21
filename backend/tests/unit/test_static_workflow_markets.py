"""Static-site workflow market matrix coverage."""

from __future__ import annotations

import json
import os
import re
import subprocess
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


def test_only_the_benchmark_backed_rewind_sets_its_output():
    # The warning text is pinned in test_export_static_site_refresh.py.
    assert "benchmark_backed_rewind=true" in _step_branch(
        'grep -qF "using benchmark-backed as-of date"'
    )
    # The deliberate previous-session coverage rewind stays a plain warning.
    assert "benchmark_backed_rewind" not in _step_branch(
        "using (benchmark-backed|previous-session) as-of date"
    )


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


def test_market_without_artifact_still_forwards_its_price_bundle():
    # Exit 79 skips the market artifact, but refreshed daily prices still ship.
    branch = _step_branch('[ "$status" -eq 79 ]')
    assert "has_price_bundle=$has_price_bundle" in branch
    assert "has_price_bundle=false" not in branch


def test_checkpointed_price_stage_uploads_its_checkpoint_and_fails_the_job():
    assert "price_checkpointed=true" in _step_branch('[ "$status" -eq 80 ]')
    gate = "steps.export-market.outputs.price_checkpointed == 'true'"
    assert gate in _build_market_step("Upload price-stage checkpoint")["if"]
    fail = _build_market_step("Fail after price-stage checkpoint")
    assert gate in fail["if"]
    assert "exit 1" in fail["run"]


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


_FAIL_REQUIRED_MARKET_STEP = "Fail a required market that published no current artifact"


def _fail_step_required_markets(step: dict) -> list[str]:
    """The required-market list the step's ``if`` tests ``matrix.market`` against."""
    match = re.search(r"contains\(fromJSON\('(\[[^']*\])'\), matrix\.market\)", step["if"])
    assert match is not None, step["if"]
    return list(json.loads(match.group(1)))


def test_required_market_without_a_current_artifact_fails_its_job_last():
    from app.services.static_market_publish_policy import REQUIRED_STATIC_MARKETS

    assert "not_trading_day=true" in _step_branch('[ "$status" -eq 78 ]')
    assert "price_checkpointed=true" in _step_branch('[ "$status" -eq 80 ]')
    assert _build_market_step("Upload market artifact")["id"] == "upload-market-artifact"

    workflow = yaml.safe_load(
        (_PROJECT_ROOT / ".github/workflows/static-site.yml").read_text(encoding="utf-8")
    )
    build_market = workflow["jobs"]["build-market"]
    fail = build_market["steps"][-1]
    assert fail["name"] == _FAIL_REQUIRED_MARKET_STEP
    assert _fail_step_required_markets(fail) == sorted(REQUIRED_STATIC_MARKETS)
    # Keyed on the upload itself, so a failed RRG restore, unpublished breadth
    # metadata or a failed upload count too, and on the benchmark-backed
    # rewind, whose upload succeeds a session behind; a non-trading day and
    # the checkpointed price stage (which already fails) are the exceptions.
    assert fail["if"] == (
        "${{ !cancelled() && contains(fromJSON('[\"US\"]'), matrix.market)"
        " && steps.export-market.outputs.not_trading_day != 'true'"
        " && steps.export-market.outputs.price_checkpointed != 'true'"
        " && (steps.upload-market-artifact.outcome != 'success'"
        " || steps.export-market.outputs.benchmark_backed_rewind == 'true') }}"
    )
    # A failed US job must not stop the other markets or the publisher.
    assert build_market["strategy"]["fail-fast"] is False
    assert "always()" in workflow["jobs"]["wake-publisher"]["if"]
    assert "build-market" in workflow["jobs"]["wake-publisher"]["needs"]


def _run_fail_required_market_step(
    tmp_path: Path,
    market: str,
    *,
    snapshot_failure: dict | None = None,
    status: dict | None = None,
    has_artifact: str = "false",
    upload_outcome: str = "skipped",
    export_outcome: str = "success",
    benchmark_backed_rewind: str = "",
) -> tuple[subprocess.CompletedProcess, str]:
    """Run the step's shell as GitHub would, with its expressions substituted."""
    data_dir = tmp_path / "static-data"
    script = (
        _build_market_step(_FAIL_REQUIRED_MARKET_STEP)["run"]
        .replace("${{ matrix.market }}", market)
        .replace("${{ steps.export-market.outcome }}", export_outcome)
        .replace("${{ steps.export-market.outputs.has_artifact }}", has_artifact)
        .replace(
            "${{ steps.export-market.outputs.benchmark_backed_rewind }}", benchmark_backed_rewind
        )
        .replace("${{ steps.upload-market-artifact.outcome }}", upload_outcome)
        .replace("/tmp/static-data", str(data_dir))
    )
    assert "${{" not in script, script
    market_lower = market.lower()
    for payload, path in (
        (snapshot_failure, data_dir / "diagnostics" / market_lower / "snapshot-failure.json"),
        (status, data_dir / "status" / market_lower / "status.json"),
    ):
        if payload is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
    summary = tmp_path / "step-summary.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text("", encoding="utf-8")
    completed = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        env={**os.environ, "GITHUB_STEP_SUMMARY": str(summary)},
        capture_output=True,
        text=True,
        check=False,
    )
    # Bytes, so a carriage return the step wrote survives into the assertion.
    return completed, summary.read_bytes().decode("utf-8")


def test_fail_required_market_step_reports_the_snapshot_failure_reason(tmp_path):
    completed, summary = _run_fail_required_market_step(
        tmp_path,
        "US",
        snapshot_failure={
            "reason": "market_rs_not_ready",
            "failure_diagnostics": {"reason_code": "benchmark_missing_current_session"},
        },
        status={"status": "failed", "reason": "no_current_artifact"},
    )

    message = (
        "Required market US uploaded no current static artifact "
        "(market_rs_not_ready: benchmark_missing_current_session)."
    )
    assert completed.returncode == 1, completed.stderr
    assert f"::error::{message}" in completed.stdout
    assert summary.startswith(message)
    assert "static-market-diagnostics-US" in summary


def test_fail_required_market_step_falls_back_to_the_status_reason_then_the_upload_outcome(tmp_path):
    completed, summary = _run_fail_required_market_step(
        tmp_path, "US", status={"status": "failed", "reason": "export_failed"}
    )
    assert completed.returncode == 1
    assert "::error::Required market US uploaded no current static artifact (export_failed)." in completed.stdout

    # An artifact that was built but never uploaded (e.g. breadth metadata not
    # published) has no diagnostics or failure status to quote.
    completed, summary = _run_fail_required_market_step(
        tmp_path / "upload", "US", has_artifact="true", upload_outcome="skipped"
    )
    assert completed.returncode == 1
    assert (
        "(export step success, has_artifact=true, upload step skipped; see the job log)."
        in completed.stdout
    )
    assert summary.startswith("Required market US uploaded no current static artifact")


def test_fail_required_market_step_explains_an_export_that_never_ran(tmp_path):
    # An earlier step failed, so the export never wrote its outputs.
    completed, summary = _run_fail_required_market_step(
        tmp_path, "US", has_artifact="", export_outcome="skipped", upload_outcome="skipped"
    )

    assert completed.returncode == 1
    message = (
        "Required market US uploaded no current static artifact "
        "(export step skipped, has_artifact=unset, upload step skipped; see the job log)."
    )
    assert f"::error::{message}" in completed.stdout
    assert summary.startswith(message)


def test_fail_required_market_step_fails_a_benchmark_backed_rewind_that_uploaded(tmp_path):
    completed, summary = _run_fail_required_market_step(
        tmp_path,
        "US",
        has_artifact="true",
        upload_outcome="success",
        benchmark_backed_rewind="true",
    )

    message = (
        "Required market US published its artifact a session behind: the export rewound "
        "to the benchmark-backed as-of date because no benchmark candidate had the current "
        "session. See the export step log; rerun once the benchmark's bar is available."
    )
    assert completed.returncode == 1, completed.stderr
    assert f"::error::{message}" in completed.stdout
    assert summary.startswith(message)
    assert "uploaded no current" not in completed.stdout


def test_fail_required_market_step_escapes_the_annotation_but_not_the_summary(tmp_path):
    completed, summary = _run_fail_required_market_step(
        tmp_path,
        "US",
        snapshot_failure={"reason": "coverage 86%\r\nbelow 90%"},
    )

    assert completed.returncode == 1
    assert (
        "::error::Required market US uploaded no current static artifact "
        "(coverage 86%25%0D%0Abelow 90%25)."
    ) in completed.stdout
    assert "Required market US uploaded no current static artifact (coverage 86%\r\nbelow 90%)." in summary
