import json
import re
from pathlib import Path

import pytest

from stockscreen_mcp import __version__

PACKAGE = Path(__file__).resolve().parents[1]


def test_registry_entry_and_bundle_manifest_carry_the_package_version():
    # A release tag only checks __init__; the registry entry and the MCPB bundle must
    # follow it. JSON only, so this runs on every supported Python.
    server = json.loads((PACKAGE / "server.json").read_text())
    manifest = json.loads((PACKAGE / "mcpb" / "manifest.json").read_text())

    assert server["version"] == __version__
    assert [p["version"] for p in server["packages"]] == [__version__]
    assert manifest["version"] == __version__


def test_bundle_pins_the_package_version():
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+; the 3.13 CI job runs this
    bundle = tomllib.loads((PACKAGE / "mcpb" / "pyproject.toml").read_text())["project"]

    assert bundle["version"] == __version__
    assert bundle["dependencies"] == [f"stockscreen-mcp=={__version__}"]


def test_bundle_declares_an_empty_tools_list():
    # Smithery needs exactly `"tools": []`: listed tools are rejected for lacking
    # inputSchema (arcadeai-labs/smithery-cli#787), and a missing key leaves the server
    # card empty, so publish fails with 400 "No values to set" (#770). Hosts list the
    # real tools from the running server.
    manifest = json.loads((PACKAGE / "mcpb" / "manifest.json").read_text())
    assert manifest.get("tools") == []
    # The six tools are discovered at runtime; MCPB says so with tools_generated
    # (default false, which would declare zero tools to strict hosts).
    assert manifest.get("tools_generated") is True


def test_bundle_states_that_it_needs_uv():
    # Both bundles launch `uv run`; the Smithery copy can't ask its host to provide uv
    # (python type), so the requirement must be visible to people installing it.
    manifest = json.loads((PACKAGE / "mcpb" / "manifest.json").read_text())
    assert "Requires uv" in manifest["long_description"]
    assert "https://docs.astral.sh/uv/" in manifest["long_description"]


def test_bundle_is_a_uv_bundle_with_a_separate_smithery_variant(tmp_path):
    # The bundle ships no dependencies, so it must stay MCPB type "uv" (the host installs
    # them from pyproject.toml). Smithery's CLI can't read "uv" yet (smithery-cli#801),
    # so smithery_bundle.py packs a copy retyped "python" for Smithery only.
    manifest = json.loads((PACKAGE / "mcpb" / "manifest.json").read_text())
    assert manifest["server"]["type"] == "uv"

    import importlib.util

    spec = importlib.util.spec_from_file_location("smithery_bundle", PACKAGE / "mcpb" / "smithery_bundle.py")
    smithery_bundle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smithery_bundle)
    staged = smithery_bundle.stage(tmp_path / "stage")

    staged_manifest = json.loads((staged / "manifest.json").read_text())
    assert staged_manifest["server"]["type"] == "python"
    assert {k: v for k, v in staged_manifest.items() if k != "server"} == {
        k: v for k, v in manifest.items() if k != "server"
    }
    assert sorted(p.name for p in staged.iterdir()) == [".mcpbignore", "manifest.json", "pyproject.toml", "server.py"]
    assert json.loads((PACKAGE / "mcpb" / "manifest.json").read_text())["server"]["type"] == "uv"  # untouched


def _smithery_bundle_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("smithery_bundle", PACKAGE / "mcpb" / "smithery_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("version", ["v14.21.3", "v18.20.4", "v19.9.0", "", "not-a-version"])
def test_smithery_packing_refuses_node_older_than_20(version):
    # smithery (the publish CLI) declares engines.node >=20.0.0; this machine's default is v14.
    with pytest.raises(SystemExit, match="Node 20"):
        _smithery_bundle_module().require_node(version)


@pytest.mark.parametrize("version", ["v20.0.0", "v22.18.0", "v24.20.0"])
def test_smithery_packing_accepts_node_20_and_newer(version):
    _smithery_bundle_module().require_node(version)


def test_registry_name_matches_the_readme_ownership_marker():
    # The MCP registry verifies PyPI ownership by finding `mcp-name: <name>` in the README.
    server = json.loads((PACKAGE / "server.json").read_text())
    readme = (PACKAGE / "README.md").read_text()

    assert re.search(rf"mcp-name: {re.escape(server['name'])}(\s|-->)", readme)


def test_license_copy_matches_the_repository_license():
    # A real copy, not a symlink: sdists store symlinks, which dangle when the wheel is built.
    repo_license = PACKAGE.parent / "LICENSE"
    if not repo_license.exists():
        pytest.skip("not inside the repository checkout")
    assert not (PACKAGE / "LICENSE").is_symlink()
    assert (PACKAGE / "LICENSE").read_bytes() == repo_license.read_bytes()
