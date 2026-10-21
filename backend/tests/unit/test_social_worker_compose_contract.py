"""Security and topology contract for the optional Social worker."""

from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = ROOT / "backend" / "Dockerfile"
BASE_COMPOSE = ROOT / "docker-compose.yml"
XUI_COMPOSE = ROOT / "docker-compose.social-xui.yml"


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _environment_map(service: dict) -> dict[str, str]:
    environment = service.get("environment") or {}
    if isinstance(environment, dict):
        return environment
    return dict(item.split("=", 1) for item in environment)


def test_public_social_worker_is_opt_in_fail_closed_and_queue_is_isolated():
    base = _yaml(BASE_COMPOSE)
    worker = base_worker = base["services"]["celery-social"]

    assert worker["profiles"] == ["social"]
    assert str(worker["user"]) not in {"root", "0", "0:0"}
    assert worker["build"]["target"] == "public"
    environment = _environment_map(base_worker)
    assert environment["SOCIAL_SIGNALS_MODE"] == "${SOCIAL_SIGNALS_MODE:-off}"
    assert environment["SOCIAL_INGEST_PROVIDER"] == "${SOCIAL_INGEST_PROVIDER:-disabled}"
    assert not any(key.startswith("SOCIAL_XUI_") for key in environment)

    services = base["services"]
    consumers = [
        name
        for name, service in services.items()
        if "social_ingestion" in str(service.get("command", ""))
    ]
    assert consumers == ["celery-social"]
    assert all("xui-reader" not in str(service.get("volumes", [])) for service in base["services"].values())


def test_dockerfile_keeps_private_install_out_of_default_public_target():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    # The last stage is the default build target.
    assert dockerfile.rstrip().endswith("FROM runtime-base AS public")
    public_build = dockerfile.split("FROM builder AS social-xui-builder", 1)[0]
    assert "xui-reader" not in public_build.lower()
    assert re.search(r"FROM runtime-base AS social-xui[\s\S]*USER stockscanner", dockerfile)
    assert not re.search(r"(?:ARG|ENV)\s+.*(?:SSH_PRIVATE|GITHUB_TOKEN|SESSION_TOKEN)", dockerfile, re.I)
    assert not re.search(r"COPY\s+.*(?:\.ssh|xui-reader|config\.toml)", dockerfile, re.I)


def test_docker_context_excludes_private_checkout_credentials_and_profiles():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert ".ssh" in ignored
    assert "**/.ssh" in ignored
    assert "xui-reader" in ignored
    assert "data/xui-reader" in ignored
