"""Discover and merge simulator TOML configuration."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from docassemble_simulator.bootstrap import deep_merge

PROJECT_CANDIDATES = (
    # Keep this order in descending precedence.  The local form is listed
    # first so reversing the result for merging makes it win its sibling.
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

# Settings in this table are simulator policy, not docassemble configuration.
# They are deliberately conservative so a package works without a config file.
SIMULATOR_DEFAULTS: dict[str, Any] = {
    "missing_runtime": "install",
    "background_actions": "foreground",
    "render_bindings": {},
    "offline": False,
}

DOCASSEMBLE_DEFAULTS: dict[str, Any] = {
    "db": {"database name": "docassemble-simulator", "driver": "sqlite"},
    "redis": "redis://localhost:6399",
    "debug": True,
    "host": "localhost",
    "locale": "en_US",
    "country": "US",
}


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


def simulator_settings(config: dict[str, Any] | None) -> dict[str, Any]:
    """Return normalized simulator-owned settings, including defaults.

    Both the documented ``[simulator]`` table and the early top-level
    ``render-bindings`` spelling are accepted.  Keeping this normalization at
    the composition boundary means interview execution never needs to know
    about TOML or command-line configuration.
    """
    settings = dict(SIMULATOR_DEFAULTS)
    source = (config or {}).get("simulator", {})
    if isinstance(source, dict):
        aliases = {
            "missing-runtime-installation": "missing_runtime",
            "missing_runtime_installation": "missing_runtime",
            "install-missing-runtime": "missing_runtime",
            "install_missing_runtime": "missing_runtime",
            "background-action-mode": "background_actions",
            "background_action_mode": "background_actions",
            "background-actions": "background_actions",
            "background_actions": "background_actions",
            "render-bindings": "render_bindings",
            "render_bindings": "render_bindings",
            "offline": "offline",
        }
        for key, value in source.items():
            settings[aliases.get(key, key)] = value
    # The binding table was introduced before the reserved table.  Retain it
    # as a compatible input while documenting the reserved form going forward.
    if isinstance((config or {}).get("render-bindings"), dict):
        settings["render_bindings"] = (config or {})["render-bindings"]
    mode = str(settings["background_actions"]).lower()
    if mode in {"stub", "disabled", "off", "none"}:
        settings["background_actions"] = "disabled"
    elif mode not in {"foreground", "disabled"}:
        raise ValueError(
            "simulator.background_actions must be 'foreground' or 'disabled'"
        )
    offline = settings.get("offline", False)
    settings["offline"] = str(offline).lower() in {"1", "true", "yes", "on"}
    if str(settings["missing_runtime"]).lower() in {"off", "false", "no", "never"}:
        settings["missing_runtime"] = "disabled"
    elif str(settings["missing_runtime"]).lower() in {"auto", "true", "yes", "install"}:
        settings["missing_runtime"] = "install"
    else:
        raise ValueError("simulator.missing_runtime must be 'install' or 'disabled'")
    if not isinstance(settings["render_bindings"], dict):
        raise TypeError("simulator.render_bindings must be a TOML table")
    return settings


def pass_through_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Remove simulator-only tables before handing configuration to docassemble."""
    result = dict(config or {})
    result.pop("simulator", None)
    result.pop("render-bindings", None)
    result.pop("render_bindings", None)
    return result


def redact_config(value: Any, *, key: str = "") -> Any:
    """Redact likely credentials without printing their values."""
    sensitive = re.compile(
        r"(?:pass(word)?|secret|token|api.?key|credential|private.?key)", re.IGNORECASE
    )
    if sensitive.search(key):
        return "<redacted>"
    if isinstance(value, str) and re.search(r"//[^/@:]+:[^/@]+@", value):
        return re.sub(r"//[^/@:]+:[^/@]+@", "//<redacted>@", value)
    if isinstance(value, dict):
        return {str(k): redact_config(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_config(item, key=key) for item in value]
    return value


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
    "DOCASSEMBLE_DEFAULTS",
    "PROJECT_CANDIDATES",
    "SIMULATOR_DEFAULTS",
    "discover_config_files",
    "global_config_path",
    "load_config",
    "normalize_config",
    "pass_through_config",
    "redact_config",
    "simulator_settings",
]
