"""Discover and merge simulator TOML configuration."""

from __future__ import annotations

import copy
import hashlib
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def deep_merge(base: dict, override: dict) -> None:
    """Merge one configuration layer into another in place."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value


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
    "seek_diagnostics": "capture",
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


def config_fingerprint(values: dict[str, Any] | None) -> str:
    """Return a stable digest of resolved user configuration values.

    Built-in defaults are not part of a resolved user configuration, so an
    unconfigured workspace has an empty fingerprint and keeps its
    interview-only session path.  Identical content from different sources
    (discovered files, ``--config``, command overrides) yields the same digest.
    """
    if not values:
        return ""
    canonical = yaml.safe_dump(values, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


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
            "seek-diagnostics": "seek_diagnostics",
            "seek_diagnostics": "seek_diagnostics",
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
    seek = str(settings.get("seek_diagnostics")).lower()
    if seek in {"capture", "on", "true", "yes", "enabled"}:
        settings["seek_diagnostics"] = "capture"
    elif seek in {"off", "disabled", "none", "false", "no"}:
        settings["seek_diagnostics"] = "off"
    else:
        raise ValueError("simulator.seek_diagnostics must be 'capture' or 'off'")
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


@dataclass(frozen=True)
class ResolvedConfiguration:
    """One resolved simulator/docassemble configuration handoff."""

    root: Path
    values: dict[str, Any]
    files: tuple[Path, ...]
    override_path: Path | None = None

    @property
    def effective_path(self) -> Path:
        return self.root / ".simulator" / "config-effective.yml"

    @property
    def fingerprint(self) -> str:
        """The digest of the resolved user configuration (see the function)."""
        return config_fingerprint(self.values)

    @property
    def simulator(self) -> dict[str, Any]:
        return simulator_settings(self.values)

    @property
    def pass_through(self) -> dict[str, Any]:
        return pass_through_config(self.values)

    def report(self) -> dict[str, Any]:
        report = {
            "files": [str(path) for path in self.files],
            "effective_config": str(self.effective_path),
            "config_fingerprint": self.fingerprint,
            "simulator": redact_config(self.simulator),
            "pass_through_keys": sorted(self.pass_through),
            "defaults": {
                "docassemble": DOCASSEMBLE_DEFAULTS,
                "simulator": SIMULATOR_DEFAULTS,
            },
        }
        if self.override_path is not None:
            report["config_override"] = str(self.override_path)
        return report


def _read_override(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"config {path} does not exist")
    if path.suffix.lower() == ".toml":
        loaded = tomllib.loads(path.read_text(encoding="utf-8"))
    else:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise TypeError(f"config {path} must be a mapping")
    return normalize_config(loaded)


def resolve_configuration(
    package_root: str | Path,
    *,
    override_path: str | Path | None = None,
    base: dict[str, Any] | None = None,
    command_overrides: dict[str, Any] | None = None,
) -> ResolvedConfiguration:
    """Resolve every source and command policy through one configuration owner."""
    root = Path(package_root).resolve()
    values = copy.deepcopy(load_config(root) if base is None else base)
    selected_override = None
    if override_path is not None:
        selected_override = Path(override_path).expanduser().resolve()
        deep_merge(values, _read_override(selected_override))
    if command_overrides:
        simulator = values.setdefault("simulator", {})
        if not isinstance(simulator, dict):
            raise TypeError("simulator config must be a table")
        deep_merge(simulator, command_overrides)
    global_file = global_config_path().expanduser()
    files = [
        *reversed(discover_config_files(root, global_path=global_file)),
    ]
    if global_file.is_file():
        files.insert(0, global_file.resolve())
    if selected_override is not None:
        files.append(selected_override)
    return ResolvedConfiguration(
        root, normalize_config(values), tuple(files), selected_override
    )


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
    "ResolvedConfiguration",
    "config_fingerprint",
    "deep_merge",
    "discover_config_files",
    "global_config_path",
    "load_config",
    "normalize_config",
    "pass_through_config",
    "redact_config",
    "resolve_configuration",
    "simulator_settings",
]
