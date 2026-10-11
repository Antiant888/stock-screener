from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_backend_ci_uses_runtime_requirements_without_optional_theme_ml_stack():
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    )

    for job_name in ("backend", "backend-unit"):
        steps = workflow["jobs"][job_name]["steps"]
        setup_uv_step = next(
            step
            for step in steps
            if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
        )
        cache_dependency_glob = setup_uv_step["with"]["cache-dependency-glob"]
        assert "backend/requirements-runtime.txt" in cache_dependency_glob
        assert "backend/requirements-test.txt" in cache_dependency_glob
        assert "backend/requirements.txt" not in cache_dependency_glob

        install_step = next(
            step for step in steps if step.get("name") == "Install dependencies"
        )
        assert install_step["run"] == (
            "uv pip install --system -r backend/requirements-runtime.txt "
            "-r backend/requirements-test.txt"
        )


def test_static_site_workflows_install_backend_requirements_with_uv():
    # pip took ~2 minutes per static job; uv with a cache takes seconds (#504).
    for name in ("static-site.yml", "static-site-publish.yml"):
        workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / name).read_text())
        installing = {
            job_name: job["steps"]
            for job_name, job in workflow["jobs"].items()
            if any("backend/requirements" in str(step.get("run", "")) for step in job.get("steps", []))
        }
        assert installing, name
        for job_name, steps in installing.items():
            runs = [str(step.get("run", "")) for step in steps]
            install = "uv pip install --system -r backend/requirements.txt"
            assert install in runs, (name, job_name)
            assert not any("pip install" in run and not run.startswith("uv ") for run in runs), (name, job_name)
            uses = [str(step.get("uses", "")) for step in steps]
            setup = next((i for i, use in enumerate(uses) if use.startswith("astral-sh/setup-uv@")), None)
            assert setup is not None and setup < runs.index(install), (name, job_name)
            glob = steps[setup]["with"]["cache-dependency-glob"]
            assert "backend/requirements.txt" in glob and "backend/requirements-server.txt" in glob


def test_backend_unit_shards_use_cache_free_equal_weight_split_and_exclude_opt_in_markers():
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    )
    steps = workflow["jobs"]["backend-unit"]["steps"]
    names = [step.get("name") for step in steps]

    assert "Safe test collection" not in names
    assert "Restore backend unit duration cache" not in names
    assert "Save backend unit duration cache" not in names
    shard_script = next(
        step["run"]
        for step in steps
        if str(step.get("name", "")).startswith(
            "Comprehensive backend unit suite"
        )
    )
    assert 'make gate-unit-files > "${gate_unit_file}"' in shard_script
    assert 'ignore_args+=(--ignore "${path}")' in shard_script
    assert "python -m pytest tests/unit \\" in shard_script
    assert '-m "not live_service and not load" \\' in shard_script
    assert (
        "Intentionally cache-free: without duration data, pytest-split gives "
        "each test equal weight and all shards compute the same partition."
    ) in shard_script
    assert "--splits 4 \\" in shard_script
    assert '--group "${{ matrix.shard }}" \\' in shard_script
    assert "--splitting-algorithm least_duration \\" in shard_script
    assert "--collect-only" not in shard_script
    assert "index % 4" not in shard_script
