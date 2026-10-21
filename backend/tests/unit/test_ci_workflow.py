from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_releases_wait_for_the_quickstart_install_smoke():
    jobs = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]
    smoke = jobs["quickstart-smoke"]

    assert smoke["needs"] == ["publish-images"]
    assert any("scripts/run_quickstart_smoke.sh" in str(step.get("run", "")) for step in smoke["steps"])
    assert "quickstart-smoke" in jobs["release"]["needs"]


def test_latest_images_move_only_after_both_are_published():
    # quickstart.sh falls back to `latest` as a backend/frontend pair. Tagging
    # `latest` in each build step published a new backend `latest` before the
    # frontend was even built, so a failed frontend left a mismatched pair.
    steps = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]["publish-images"]["steps"]
    names = [step.get("name", "") for step in steps]

    for meta in ("Backend image metadata", "Frontend image metadata"):
        inputs = steps[names.index(meta)]["with"]
        assert "value=latest" not in inputs["tags"], meta
        # metadata-action's default `latest=auto` also emits `latest` for
        # type=ref,event=tag and type=semver, i.e. on every v* tag push.
        assert "latest=false" in inputs.get("flavor", ""), meta

    promote = names.index("Promote latest after both images are published")
    assert promote > names.index("Build and push backend image")
    assert promote > names.index("Build and push frontend image")
    assert "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)" in steps[promote]["if"]


def test_stockscreen_mcp_is_released_by_tag_with_trusted_publishing():
    release = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "stockscreen-mcp-release.yml").read_text())
    # PyYAML parses the bare `on` key as True.
    assert release[True]["push"]["tags"] == ["stockscreen-mcp-v*"]
    publish = release["jobs"]["publish"]
    assert publish["needs"] == "build"
    assert publish["environment"]["name"] == "pypi"
    assert publish["permissions"] == {"id-token": "write"}  # OIDC only, no stored token


def test_stockscreen_mcp_registers_in_the_mcp_registry_after_pypi_with_a_verified_publisher():
    workflows = REPO_ROOT / ".github" / "workflows"
    release = yaml.safe_load((workflows / "stockscreen-mcp-release.yml").read_text())["jobs"]
    registry_call = release["mcp-registry"]
    # The registry verifies the PyPI package, so it must run after the upload.
    assert registry_call["needs"] == "publish"
    assert registry_call["uses"] == "./.github/workflows/stockscreen-mcp-registry.yml"

    job = yaml.safe_load((workflows / "stockscreen-mcp-registry.yml").read_text())["jobs"]["registry"]
    # The publisher binary is pinned and checksum-verified, never releases/latest.
    assert job["env"]["MCP_PUBLISHER_VERSION"].startswith("v")
    assert len(job["env"]["MCP_PUBLISHER_SHA256"]) == 64
    script = " ".join(str(step.get("run", "")) for step in job["steps"])
    assert "releases/latest" not in script
    assert "sha256sum --check" in script
