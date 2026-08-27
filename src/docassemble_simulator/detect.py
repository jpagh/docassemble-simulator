"""Compatibility facade for workspace and Interview catalog discovery."""

from __future__ import annotations

from pathlib import Path

from docassemble_simulator.catalog import (
    guess_main_interview,
    list_interviews,
    list_packages,
    resolve_interview,
)
from docassemble_simulator.preflight import ensure_importable


def find_package_root(root: str | Path | None = None) -> Path:
    """Find the workspace containing a ``docassemble/`` package directory."""
    start = Path(root or Path.cwd()).resolve()
    if not start.is_dir():
        raise SystemExit(f"error: root is not a directory: {start}")
    for candidate in (start, *start.parents):
        da_dir = candidate / "docassemble"
        if da_dir.is_dir() and any(
            path.is_dir() and (path / "__init__.py").exists()
            for path in da_dir.iterdir()
        ):
            return candidate
    raise SystemExit(
        "error: no docassemble package found here or in parent directories "
        f"(looked for a 'docassemble/' package dir starting at {start})"
    )


__all__ = [
    "ensure_importable",
    "find_package_root",
    "guess_main_interview",
    "list_interviews",
    "list_packages",
    "resolve_interview",
]
