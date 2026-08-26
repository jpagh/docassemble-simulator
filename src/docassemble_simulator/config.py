"""Discover and merge simulator TOML configuration."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from docassemble_simulator.bootstrap import deep_merge

PROJECT_CANDIDATES = (
    "simulator.local.toml",
    ".simulator.local.toml",
    "simulator.toml",
    ".simulator.toml",
    "simulator/config.toml",
    ".simulator/config.toml",
    ".config/simulator.toml",
    ".config/simulator/config.local.toml",
    ".config/simulator/config.toml",
)


def global_config_path() -> Path:
    configured = os.environ.get("DOCASSEMBLE_SIMULATOR_CONFIG")
    if configured:
        return Path(configured).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "docassemble-simulator" / "config.toml"
    return Path.home() / ".config" / "docassemble-simulator" / "config.toml"


def discover_config_files(
    package_root: str | Path, *, global_path: str | Path | None = None
) -> list[Path]:
    """Return project config files in descending precedence order."""
    root = Path(package_root).resolve()
    excluded = (
        (Path(global_path) if global_path else global_config_path())
        .expanduser()
        .resolve()
    )
    files: list[Path] = []
    for directory in (root, *root.parents):
        for candidate in PROJECT_CANDIDATES:
            path = directory / candidate
            if path.is_file() and path.resolve() != excluded:
                files.append(path)
    return files


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    """Translate TOML-friendly names to docassemble's server-config names."""
    normalized = dict(config)
    if "jinja-data" in normalized:
        value = normalized.pop("jinja-data")
        if (
            "jinja data" in normalized
            and isinstance(normalized["jinja data"], dict)
            and isinstance(value, dict)
        ):
            merged: dict[str, Any] = {}
            deep_merge(merged, normalized["jinja data"])
            deep_merge(merged, value)
            normalized["jinja data"] = merged
        else:
            normalized["jinja data"] = value
    return normalized


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as err:
        raise ValueError(f"could not parse simulator config {path}: {err}") from err
    if not isinstance(value, dict):
        raise TypeError(f"simulator config {path} must be a TOML table")
    return normalize_config(value)


def load_config(
    package_root: str | Path, *, global_path: str | Path | None = None
) -> dict[str, Any]:
    """Load global and project configs; nearest project files win."""
    global_file = (
        Path(global_path) if global_path else global_config_path()
    ).expanduser()
    layers: list[Path] = []
    if global_file.is_file():
        layers.append(global_file)
    # Files are returned nearest/highest first, so merge them in reverse.
    layers.extend(
        reversed(discover_config_files(package_root, global_path=global_file))
    )
    merged: dict[str, Any] = {}
    for path in layers:
        deep_merge(merged, _read_toml(path))
    return normalize_config(merged)


__all__ = [
    "PROJECT_CANDIDATES",
    "discover_config_files",
    "global_config_path",
    "load_config",
    "normalize_config",
]
