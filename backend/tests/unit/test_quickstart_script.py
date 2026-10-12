"""scripts/quickstart.sh against a fake docker: the config it writes and the compose calls it makes."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "quickstart.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
if [[ "$1" == "manifest" ]]; then exit "${FAKE_MANIFEST_EXIT:-0}"; fi
exit 0
"""
FAKE_SUDO = """#!/usr/bin/env bash
echo "sudo $*" >> "$FAKE_LOG"
"""


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("sudo", FAKE_SUDO)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    env_file = tmp_path / ".env.docker"
    log = tmp_path / "calls.log"

    def _run(**extra_env):
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_LOG": str(log),
            "STOCKSCREEN_ENV_FILE": str(env_file),
            "STOCKSCREEN_PYTHON": sys.executable,
            **extra_env,
        }
        for key in ("APP_IMAGE_TAG", "ENABLED_MARKETS", "FRONTEND_PORT", "COMPOSE_PROFILES"):
            if key not in extra_env:
                env.pop(key, None)
        result = subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        values = dict(
            line.split("=", 1)
            for line in env_file.read_text().splitlines()
            if line and not line.startswith("#") and "=" in line
        )
        return result, values, log.read_text()

    return _run


def _checkout_tag():
    head = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "describe", "--tags", "--exact-match", "--match", "v*", "HEAD"],
        capture_output=True, text=True,
    )
    if head.returncode == 0:
        return head.stdout.strip()
    sha = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    return f"sha-{sha[:7]}"


def test_first_run_writes_a_working_config_and_starts_the_release_stack(run):
    result, values, calls = run()

    assert values["BACKEND_IMAGE"] == "ghcr.io/xang1234/stockscreenclaude-backend"
    assert values["FRONTEND_IMAGE"] == "ghcr.io/xang1234/stockscreenclaude-frontend"
    assert values["APP_IMAGE_TAG"] == _checkout_tag()
    assert len(values["SERVER_AUTH_PASSWORD"]) >= 16
    assert len(values["SERVER_AUTH_SESSION_SECRET"]) >= 32
    assert values["SERVER_AUTH_SESSION_SECRET"] != values["SERVER_AUTH_PASSWORD"]
    # Everything else in the documented template is kept.
    assert values["ENABLED_MARKETS"] == "US"
    assert "OPTIONS_ANALYTICS_ENABLED" in values

    files = "-f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.release.yml"
    assert re.search(rf"docker compose --env-file \S+ {files} pull\n", calls)
    assert re.search(rf"docker compose --env-file \S+ {files} up -d --no-build --wait --wait-timeout \d+\n", calls)
    assert values["SERVER_AUTH_PASSWORD"] in result.stdout
    assert "http://localhost\n" in result.stdout


def test_rerun_keeps_secrets_and_user_settings(run):
    _, first, _ = run(ENABLED_MARKETS="US,HK", FRONTEND_PORT="8080")
    result, second, _ = run()

    assert second["SERVER_AUTH_PASSWORD"] == first["SERVER_AUTH_PASSWORD"]
    assert second["SERVER_AUTH_SESSION_SECRET"] == first["SERVER_AUTH_SESSION_SECRET"]
    assert second["ENABLED_MARKETS"] == "US,HK"
    assert "http://localhost:8080" in result.stdout
    # A password the user already has is not printed again.
    assert first["SERVER_AUTH_PASSWORD"] not in result.stdout


def test_explicit_tag_wins(run):
    _, values, calls = run(APP_IMAGE_TAG="v1.6.0")

    assert values["APP_IMAGE_TAG"] == "v1.6.0"
    assert "docker manifest" not in calls


def test_falls_back_to_latest_when_the_checkout_has_no_published_image(run):
    if _checkout_tag().startswith("v"):
        pytest.skip("release checkouts never fall back")
    result, values, _ = run(FAKE_MANIFEST_EXIT="1")

    assert values["APP_IMAGE_TAG"] == "latest"
    assert "latest" in result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="bind-mount ownership only matters on Linux")
def test_linux_hands_data_to_the_container_user(run):
    if os.getuid() in (0, 1000):
        pytest.skip("data/ is already writable by uid 1000")
    _, _, calls = run()

    assert f"sudo chown -R 1000:1000 {REPO_ROOT / 'data'}" in calls


def test_release_images_pin_the_only_published_platform():
    # publish-images builds amd64 only; without the pin ARM hosts fail to pull.
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    release = (REPO_ROOT / "docker-compose.release.yml").read_text()

    assert "platforms:" not in ci
    for anchor in ("&backend-release-service", "&frontend-release-service"):
        block = release.split(anchor, 1)[1].split("\n\n", 1)[0]
        assert "platform: linux/amd64" in block


def test_frontend_healthcheck_targets_ipv4():
    # busybox wget resolves localhost to [::1]; nginx listens on IPv4 only, so
    # the check never passed and `up --wait` failed on the frontend.
    import yaml

    prod = yaml.safe_load((REPO_ROOT / "docker-compose.prod.yml").read_text())
    test = " ".join(prod["services"]["frontend"]["healthcheck"]["test"])

    assert "http://127.0.0.1:80/nginx-health" in test
    assert "localhost" not in test


def test_base_compose_does_not_cancel_the_production_worker_healthchecks():
    # `healthcheck: disable: true` survives the prod overlay merge, so the
    # overlay's celery ping checks never ran and `up --wait` refused to wait.
    import yaml

    base = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())

    disabled = sorted(
        name for name, service in base["services"].items()
        if (service.get("healthcheck") or {}).get("disable")
    )
    assert disabled == []
