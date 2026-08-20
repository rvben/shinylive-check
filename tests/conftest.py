"""Session fixtures: export the fixture apps once with a pinned shinylive."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Pinned: the suite must pass or fail because of the checker, not because a shinylive
# release changed what an export looks like. Bump deliberately.
SHINYLIVE_VERSION = "0.8.11"

FIXTURE_APPS = Path(__file__).parent / "fixtures" / "apps"


def export_app(name: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    site = tmp_path_factory.mktemp(f"export-{name}") / "site"
    proc = subprocess.run(
        ["uvx", f"shinylive=={SHINYLIVE_VERSION}", "export", str(FIXTURE_APPS / name), str(site)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"shinylive export failed for {name}:\n{proc.stdout}\n{proc.stderr}")
    return site


@pytest.fixture(scope="session")
def readme() -> str:
    """The README as text, for the tests that hold the docs to the contract."""
    return (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def ok_site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return export_app("ok", tmp_path_factory)


@pytest.fixture(scope="session")
def boot_error_site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return export_app("boot_error", tmp_path_factory)


@pytest.fixture(scope="session")
def missing_package_site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return export_app("missing_package", tmp_path_factory)


def run_cli(
    *args: str,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float = 600,
) -> subprocess.CompletedProcess[str]:
    """The CLI exactly as a user runs it: a subprocess judged by exit code and output.

    ``timeout`` is the harness giving up, not the tool's own ``--timeout``. The
    default is long enough that a slow cold boot is never mistaken for a hang; a
    case about the tool bounding its own run passes something short, so a check
    that never returns is reported as one instead of after ten minutes.
    """
    return subprocess.run(
        [sys.executable, "-m", "shinylive_check", *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        cwd=None if cwd is None else str(cwd),
        timeout=timeout,
    )
