"""Pack a Smithery-only copy of this MCPB bundle.

Smithery's CLI can't read MCPB's "uv" server type yet (arcadeai-labs/smithery-cli#801),
so this stages a copy whose manifest says "python" and packs it. Smithery launches the
server from mcp_config (`uv run ...`) either way; the bundle in this directory stays a
correct "uv" bundle for Claude Desktop and other MCPB hosts.

    python smithery_bundle.py stockscreen-mcp-smithery.mcpb
    smithery mcp publish stockscreen-mcp-smithery.mcpb -n davidten7/stockscreen-mcp
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent
FILES = (".mcpbignore", "manifest.json", "pyproject.toml", "server.py")
# smithery (the publish CLI) declares engines.node >=20.0.0.
MIN_NODE_MAJOR = 20


def require_node(version: str) -> None:
    """Exit with a clear message unless `node --version` output is Node 20 or newer."""
    major = version.strip().lstrip("v").split(".")[0]
    if not major.isdigit() or int(major) < MIN_NODE_MAJOR:
        found = version.strip() or "no node on PATH"
        sys.exit(
            f"Node {MIN_NODE_MAJOR}+ is required to pack and publish to Smithery (found: {found}). "
            "With nvm: nvm use 22"
        )


def stage(target: Path) -> Path:
    """Copy the bundle files into `target` with the manifest retyped for Smithery."""
    target.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        shutil.copy2(BUNDLE / name, target / name)
    manifest = json.loads((target / "manifest.json").read_text())
    manifest["server"]["type"] = "python"
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return target


def node_version() -> str:
    try:
        return subprocess.run(["node", "--version"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return ""


def main() -> None:
    require_node(node_version())
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "stockscreen-mcp-smithery.mcpb").resolve()
    with tempfile.TemporaryDirectory() as tmp:
        staged = stage(Path(tmp) / "bundle")
        subprocess.run(["npx", "-y", "@anthropic-ai/mcpb", "pack", str(staged), str(output)], check=True)
    print(f"Smithery bundle: {output}")


if __name__ == "__main__":
    main()
