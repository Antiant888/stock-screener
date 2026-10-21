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
if [[ "$1" == "manifest" ]]; then
  if [[ -n "${FAKE_MANIFEST_MISSING:-}" && "$*" == *"$FAKE_MANIFEST_MISSING"* ]]; then exit 1; fi
  exit "${FAKE_MANIFEST_EXIT:-0}"
fi
if [[ "$1 $2 $3" == "compose version --short" ]]; then echo "${FAKE_COMPOSE_VERSION:-2.29.1}"; fi
if [[ "$1" == "compose" && " $* " == *" up "* ]]; then exit "${FAKE_UP_EXIT:-0}"; fi
if [[ "$1" == "compose" && " $* " == *" config --services "* ]]; then printf '%b\\n' "${FAKE_SERVICES:-backend}"; fi
if [[ "$1" == "ps" ]]; then [[ -z "${FAKE_CONTAINERS:-}" ]] || printf '%b\\n' "$FAKE_CONTAINERS"; fi
if [[ "$1 $2 $3" == "buildx imagetools inspect" ]]; then
  [[ -n "${FAKE_LATEST_REVISION:-}" ]] || exit 1
  echo "$FAKE_LATEST_REVISION"
fi
exit 0
"""
FAKE_SUDO = """#!/usr/bin/env bash
echo "sudo $*" >> "$FAKE_LOG"
"""
FAKE_SETFACL = """#!/usr/bin/env bash
echo "setfacl $*" >> "$FAKE_LOG"
"""
# Real Python, but records the env file's mode when the script runs its
# secret-writing block (`python - <env file> ...`).
RECORDING_PYTHON = """#!/usr/bin/env bash
if [[ "$1" == "-" ]]; then
  "$REAL_PYTHON" -c 'import os, sys; print("env-mode", oct(os.stat(sys.argv[1]).st_mode & 0o777))' "$2" >> "$FAKE_LOG"
fi
exec "$REAL_PYTHON" "$@"
"""


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("docker", FAKE_DOCKER), ("sudo", FAKE_SUDO), ("setfacl", FAKE_SETFACL), ("python-rec", RECORDING_PYTHON),
    ):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    env_file = tmp_path / ".env.docker"
    log = tmp_path / "calls.log"

    def _run(env_text=None, expect_ok=True, **extra_env):
        if env_text is not None:
            env_file.write_text(env_text)
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_LOG": str(log),
            "STOCKSCREEN_ENV_FILE": str(env_file),
            "STOCKSCREEN_PYTHON": str(bin_dir / "python-rec"),
            "REAL_PYTHON": sys.executable,
            **extra_env,
        }
        for key in ("APP_IMAGE_TAG", "ENABLED_MARKETS", "FRONTEND_PORT", "COMPOSE_PROFILES"):
            if key not in extra_env:
                env.pop(key, None)
        result = subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60,
        )
        if not expect_ok:
            return result
        assert result.returncode == 0, result.stdout + result.stderr
        values = dict(
            line.split("=", 1)
            for line in env_file.read_text().splitlines()
            if line and not line.startswith("#") and "=" in line
        )
        return result, values, log.read_text()

    _run.env_file = env_file
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


def test_shows_a_new_password_even_when_startup_fails(run):
    # The password is saved before startup, so a retry would treat it as existing
    # and never print it: show it as soon as it is generated.
    result = run(expect_ok=False, FAKE_UP_EXIT="1")
    saved = next(
        line.split("=", 1)[1]
        for line in run.env_file.read_text().splitlines()
        if line.startswith("SERVER_AUTH_PASSWORD=")
    )

    assert result.returncode != 0
    assert saved in result.stdout


def test_refuses_compose_that_cannot_parse_the_release_overlay(run):
    # docker-compose.release.yml uses `build: !reset null`; Compose 2.17.x ships
    # compose-go 1.13.4, which has no ResetProcessor (added in 1.13.5 / Compose 2.18.0).
    result = run(expect_ok=False, FAKE_COMPOSE_VERSION="2.17.3")

    assert result.returncode != 0
    assert "Compose 2.18 or newer is required (found: 2.17.3)" in result.stderr
    assert not run.env_file.exists()


def test_accepts_the_first_compose_with_reset_support(run):
    _, values, _ = run(FAKE_COMPOSE_VERSION="2.18.0")

    assert values["APP_IMAGE_TAG"]


def test_env_file_is_private_before_secrets_are_written(run):
    # cp keeps the template's 0644; secrets must never sit in a world-readable file.
    _, _, calls = run()

    # The first `python -` call is the secret-writing block; later ones are the
    # compose wrapper reading settings back.
    modes = [line.split()[1] for line in calls.splitlines() if line.startswith("env-mode ")]
    assert modes[0] == "0o600"
    assert oct(run.env_file.stat().st_mode & 0o777) == "0o600"


def test_rerun_removes_workers_for_markets_no_longer_enabled(run):
    # Compose keeps containers of now-inactive profiles running, even with
    # --remove-orphans, so narrowing ENABLED_MARKETS left the old workers up.
    _, _, calls = run(
        FAKE_SERVICES="backend\\ncelery-userscans\\ncelery-marketjobs-us\\ncelery-userscans-us",
        FAKE_CONTAINERS=(
            "c1 backend\\nc2 celery-marketjobs-us\\nc3 celery-marketjobs-hk\\n"
            "c4 celery-userscans-hk\\nc5 db-backup\\nc6 celery-userscans"
        ),
    )

    assert f"label=com.docker.compose.project.working_dir={REPO_ROOT}" in calls
    # `docker stop` honours the stop_grace_period compose stores as StopTimeout,
    # so a worker mid-task gets its warm shutdown; `rm -f` would SIGKILL it.
    lines = calls.splitlines()
    assert "docker stop c3 c4" in lines
    assert "docker rm c3 c4" in lines
    assert not any(line.startswith("docker rm -f") for line in lines)
    assert lines.index("docker stop c3 c4") < lines.index("docker rm c3 c4")
    assert calls.index("docker rm c3 c4") < calls.index(" up -d ")


def test_rerun_with_the_same_markets_removes_nothing(run):
    _, _, calls = run(
        FAKE_SERVICES="backend\\ncelery-marketjobs-us",
        FAKE_CONTAINERS="c1 backend\\nc2 celery-marketjobs-us",
    )

    assert "docker rm " not in calls
    assert "docker stop " not in calls


def test_explicit_tag_wins(run):
    _, values, calls = run(APP_IMAGE_TAG="v1.6.0")

    assert values["APP_IMAGE_TAG"] == "v1.6.0"
    assert "docker manifest" not in calls


def test_falls_back_to_latest_when_the_checkout_has_no_published_image(run):
    if _checkout_tag().startswith("v"):
        pytest.skip("release checkouts never fall back")
    result, values, _ = run(FAKE_MANIFEST_EXIT="1")

    assert values["APP_IMAGE_TAG"] == "latest"
    assert "no published image for this checkout" in result.stderr


def test_newest_main_build_is_pinned_to_one_commit_for_both_images(run):
    # The two `latest` tags move in separate registry writes; resolving the
    # backend's `latest` to its commit and pulling that commit's sha-* tags
    # keeps the pair matched even mid-promotion.
    if _checkout_tag().startswith("v"):
        pytest.skip("release checkouts never fall back")
    revision = "abcdef1234567890abcdef1234567890abcdef12"
    result, values, calls = run(
        FAKE_MANIFEST_MISSING=_checkout_tag(),  # no image for this checkout
        FAKE_LATEST_REVISION=revision,
    )

    assert values["APP_IMAGE_TAG"] == "sha-abcdef1"
    assert "ghcr.io/xang1234/stockscreenclaude-backend:latest" in calls
    assert "ghcr.io/xang1234/stockscreenclaude-frontend:sha-abcdef1" in calls
    assert "sha-abcdef1" in result.stderr


def test_uses_plain_latest_when_the_latest_revision_cannot_be_read(run):
    if _checkout_tag().startswith("v"):
        pytest.skip("release checkouts never fall back")
    _, values, _ = run(FAKE_MANIFEST_MISSING=_checkout_tag())

    assert values["APP_IMAGE_TAG"] == "latest"


def test_falls_back_when_only_the_backend_image_was_published(run):
    # publish-images pushes the backend before building the frontend; a failed
    # frontend push leaves a sha-* tag that cannot be pulled as a pair.
    if _checkout_tag().startswith("v"):
        pytest.skip("release checkouts never fall back")
    _, values, _ = run(FAKE_MANIFEST_MISSING="stockscreenclaude-frontend")

    assert values["APP_IMAGE_TAG"] == "latest"


@pytest.mark.skipif(sys.platform != "linux", reason="bind-mount ownership only matters on Linux")
def test_linux_grants_the_container_user_access_without_taking_ownership(run):
    if os.getuid() in (0, 1000):
        pytest.skip("data/ is already writable by uid 1000")
    _, _, calls = run()

    # An ACL keeps the user's ownership, so updating the checkout can still rewrite tracked data/ files.
    assert f"setfacl -R -m u:1000:rwX -m d:u:1000:rwX {REPO_ROOT / 'data'}" in calls
    assert "chown" not in calls


def test_fills_quoted_empty_secrets_and_edits_the_last_duplicate_key(run):
    _, _, _ = run(env_text=(
        'SERVER_AUTH_PASSWORD=""\n'
        "APP_IMAGE_TAG=v0.0.1\n"
        "APP_IMAGE_TAG=v0.0.2\n"
    ))
    lines = (run.env_file).read_text().splitlines()

    password = next(line for line in lines if line.startswith("SERVER_AUTH_PASSWORD="))
    assert len(password.split("=", 1)[1]) >= 16
    # Compose uses the last duplicate, so that is the one that must change.
    tags = [line for line in lines if line.startswith("APP_IMAGE_TAG=")]
    assert tags == ["APP_IMAGE_TAG=v0.0.1", f"APP_IMAGE_TAG={_checkout_tag()}"]


def test_release_images_pin_the_only_published_platform():
    # publish-images builds amd64 only; without the pin ARM hosts fail to pull.
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    release = (REPO_ROOT / "docker-compose.release.yml").read_text()

    assert "platforms:" not in ci
    for anchor in ("&backend-release-service", "&frontend-release-service"):
        block = release.split(anchor, 1)[1].split("\n\n", 1)[0]
        assert "platform: linux/amd64" in block
