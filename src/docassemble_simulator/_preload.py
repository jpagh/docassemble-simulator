"""Preload installed docassemble package modules before any interview compiles.

A docassemble webapp server walks every installed ``docassemble.<package>``
module at startup and imports the ones that define classes (or opt in with a
``# pre-load`` marker).  Those imports are what register custom datatypes via
``docassemble.base.util.CustomDataType``; without them a standard field such
as AssemblyLine's ``BirthDate`` with ``alMonthLabel`` / ``alDayLabel`` /
``alYearLabel`` mako parameters is rejected by the parser as an overwritten
label.

The simulator has no long-lived server process, so this module reproduces the
webapp's startup import pass whenever the runtime is bootstrapped, before any
interview is parsed.  It is
deliberately narrow: it reads the installed package tree and the effective
``preloaded modules`` / ``module whitelist`` / ``module blacklist``
configuration, it never installs, edits, or rewrites anything, and a failing
package import is logged and skipped exactly as the webapp does.
"""

from __future__ import annotations

import fnmatch
import importlib
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Core packages the webapp never preloads through this pass.
_CORE_PACKAGES = frozenset({"base", "demo", "webapp"})

#: Marker a package author can place at the top of a module to opt out.
_SKIP_MARKER = "# do not pre-load"

#: Markers the webapp uses to decide a module has import-time side effects.
_PRELOAD_MARKERS = ("# pre-load", "docassemble.base.util.update")

_PRELOADED = False
_FAILURES: tuple[tuple[str, BaseException], ...] = ()


def _package_directories() -> list[Path]:
    """Installed ``docassemble`` namespace portions, if any."""
    try:
        import docassemble
    except ImportError:
        return []
    return [Path(entry) for entry in getattr(docassemble, "__path__", ()) or ()]


def _effective_config() -> dict[str, Any]:
    try:
        from docassemble.base import config
    except ImportError:
        return {}
    value = getattr(config, "daconfig", None)
    return value if isinstance(value, dict) else {}


def _module_name(root: Path, path: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(["docassemble", *parts])


def _needs_preload(path: Path) -> bool:
    """Whether a module defines classes or opts in to preloading.

    Mirrors the webapp's line scan: a ``# do not pre-load`` marker anywhere
    before the first qualifying line excludes the module.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith(_SKIP_MARKER):
                    return False
                if line.startswith("class ") or any(
                    marker in line for marker in _PRELOAD_MARKERS
                ):
                    return True
    except OSError:
        return False
    return False


def _as_patterns(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _candidate_modules(config: dict[str, Any], roots: list[Path]) -> list[str]:
    """Module names the webapp would import at startup, in import order."""
    whitelist = _as_patterns(config.get("module whitelist"))
    blacklist = _as_patterns(config.get("module blacklist"))
    candidates: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        if name in seen:
            return
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in blacklist):
            return
        seen.add(name)
        candidates.append(name)

    for name in _as_patterns(config.get("preloaded modules")):
        add(name)
    for root in roots:
        for current, directories, files in os.walk(root):
            relative = Path(current).relative_to(root).parts
            if relative and relative[0] in _CORE_PACKAGES:
                directories[:] = []
                continue
            directories[:] = [name for name in directories if name != "__pycache__"]
            for filename in files:
                if not filename.endswith(".py"):
                    continue
                path = Path(current) / filename
                name = _module_name(root, path)
                if whitelist:
                    if any(fnmatch.fnmatchcase(name, pattern) for pattern in whitelist):
                        add(name)
                    continue
                if _needs_preload(path):
                    add(name)
    return candidates


def _runtime_available() -> bool:
    try:
        importlib.import_module("docassemble.base")
    except ImportError:
        return False
    return True


def preload_installed_modules() -> tuple[tuple[str, BaseException], ...]:
    """Import installed package modules so class registrations happen first.

    Returns the modules that failed to import so callers can surface them as
    diagnostics.  Failures are not fatal: a broken optional package must not
    prevent the rest of the environment from loading, matching the webapp.
    """
    global _PRELOADED, _FAILURES
    if _PRELOADED:
        return _FAILURES
    _PRELOADED = True

    config = _effective_config()
    roots = _package_directories()
    failures: list[tuple[str, BaseException]] = []
    candidates = _candidate_modules(config, roots)
    if roots and _runtime_available():
        # The webapp always imports docassemble.base.legal; it is not part of
        # the walk because docassemble.base is a core package.
        candidates.insert(0, "docassemble.base.legal")
    for name in candidates:
        try:
            importlib.import_module(name)
        except BaseException as error:  # noqa: BLE001 - webapp logs and continues
            failures.append((name, error))
            logger.debug("could not preload %s: %s", name, error)
    _FAILURES = tuple(failures)
    return _FAILURES


def reset_preload_state() -> None:
    """Forget the process-level preload pass (tests and embedders only)."""
    global _PRELOADED, _FAILURES
    _PRELOADED = False
    _FAILURES = ()


__all__ = ["preload_installed_modules", "reset_preload_state"]
