from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]


def test_license_copy_matches_the_repository_license():
    # A real copy, not a symlink: sdists store symlinks, which dangle when the wheel is built.
    repo_license = PACKAGE.parent / "LICENSE"
    if not repo_license.exists():
        pytest.skip("not inside the repository checkout")
    assert not (PACKAGE / "LICENSE").is_symlink()
    assert (PACKAGE / "LICENSE").read_bytes() == repo_license.read_bytes()
