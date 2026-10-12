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


def test_bundle_runtime_is_one_smithery_recognises():
    # smithery-cli src/lib/mcpb.ts detectBundleRuntime only knows python/node/binary
    # (or a bun command) and rejects MCPB's "uv" type. Hosts still launch the server
    # through mcp_config, which runs `uv run` either way.
    manifest = json.loads((PACKAGE / "mcpb" / "manifest.json").read_text())
    assert manifest["server"]["type"] == "python"
    assert manifest["server"]["mcp_config"]["command"] == "uv"


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
