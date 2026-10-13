"""Production overlay contract for the Celery worker tier.

The prod overlay once listed only the US/HK/JP/TW market workers, so the other
markets ran with no CPU or memory limit (and the daily snapshot sized its
process pool from every host core). These tests pin that every worker the
base file defines gets limits and a health check that pings its own node.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
BASE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
PROD = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"))

# Workers whose daily pipeline stages are CPU-bound. A cap below one core
# throttles a single-threaded Python process to a fraction of its speed.
PIPELINE_WORKER = re.compile(r"celery-(marketjobs-[a-z]+|datafetch)$")


def _celery_workers() -> dict[str, dict]:
    return {
        name: service
        for name, service in BASE["services"].items()
        if name.startswith("celery-") and "worker" in str(service.get("command", ""))
    }


def _node_name(command: str) -> str:
    match = re.search(r"-n\s+([\w-]+)@%h", command)
    assert match, f"no -n <node>@%h in worker command: {command!r}"
    return match.group(1)


def test_every_base_worker_has_prod_limits():
    # 12 markets x (market jobs + user scans), plus general, data fetch,
    # social and shared user scans; fewer means the filter stopped matching.
    assert len(_celery_workers()) >= 28
    missing = [name for name in _celery_workers() if name not in PROD["services"]]
    assert not missing, f"workers without a docker-compose.prod.yml entry: {missing}"
    for name in _celery_workers():
        limits = PROD["services"][name]["deploy"]["resources"]["limits"]
        assert limits.get("cpus") and limits.get("memory"), name


def test_prod_healthcheck_pings_the_workers_own_node():
    for name, service in _celery_workers().items():
        prod = PROD["services"][name]
        test = " ".join(prod["healthcheck"]["test"])
        node = prod.get("environment", {}).get("WORKER_NODE_NAME")
        resolved = test.replace("$$WORKER_NODE_NAME", str(node))
        assert f"-d {_node_name(service['command'])}@$$HOSTNAME" in resolved, (
            f"{name}: health check {test!r} (WORKER_NODE_NAME={node!r}) does not ping "
            f"node {_node_name(service['command'])!r}"
        )


def test_pipeline_workers_get_at_least_one_cpu():
    for name in _celery_workers():
        if PIPELINE_WORKER.match(name):
            cpus = float(PROD["services"][name]["deploy"]["resources"]["limits"]["cpus"])
            assert cpus >= 1, f"{name} is capped at {cpus} CPU"
