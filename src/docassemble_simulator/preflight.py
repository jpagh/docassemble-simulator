"""Import probing and acquisition for the docassemble runtime."""

from __future__ import annotations

import importlib
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


def _import_error(module_name: str):
    """Return an import error, or None when a runtime module imports cleanly."""
    try:
        importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name in {module_name, module_name.split(".")[0]}:
            return "missing"
        return error
    except ImportError as error:
        return error
    return None


def _locked_runtime_specs(root: Path, missing: list[str]) -> list[str]:
    """Find exact package pins from the active project or uv lock."""
    names = {
        "docassemble.base": "docassemble-base",
        "docassemble.webapp": "docassemble-webapp",
    }
    wanted = {names[item] for item in missing}
    specs: dict[str, str] = {}
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            dependencies = data.get("project", {}).get("dependencies", [])
            for item in dependencies:
                if isinstance(item, str):
                    package = re.split(r"[<>=!~; ]", item, maxsplit=1)[0].lower()
                    if package in wanted and "==" in item:
                        specs[package] = item
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            pass
    lock = root / "uv.lock"
    if lock.is_file():
        try:
            data = tomllib.loads(lock.read_text(encoding="utf-8"))
            for package in data.get("package", []):
                name = str(package.get("name", "")).lower()
                if name in wanted and name not in specs and package.get("version"):
                    specs[name] = f"{name}=={package['version']}"
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            pass
    return [specs.get(name, name) for name in sorted(wanted)]


def _install_missing_runtime(root: Path, missing: list[str]) -> str:
    specs = _locked_runtime_specs(root, missing)
    uv = shutil.which("uv")
    command = (
        [uv, "pip", "install", "--python", sys.executable, *specs]
        if uv
        else [sys.executable, "-m", "pip", "install", *specs]
    )
    try:
        completed = subprocess.run(command, check=False, text=True, capture_output=True)
    except OSError as error:
        raise SystemExit(
            "error: could not acquire missing docassemble runtime packages. "
            f"Command was: {' '.join(command)} ({error})"
        ) from error
    if completed.returncode:
        detail = (
            completed.stderr or completed.stdout or "installer returned a failure"
        ).strip()
        raise SystemExit(
            "error: runtime package acquisition failed. Re-run or install the "
            f"packages manually with: {' '.join(command)}\n{detail}"
        )
    return " ".join(command)


def ensure_importable(
    root: str | Path,
    *,
    install_missing: bool = True,
    offline: bool = False,
) -> None:
    """Activate the source root and acquire a missing core runtime if allowed."""
    root_path = str(Path(root).resolve())
    if root_path not in sys.path:
        sys.path.insert(0, root_path)
    root_object = Path(root).resolve()
    try:
        from docassemble_simulator._runtime import SimulatorRuntime

        SimulatorRuntime().install_fake_redis()
    except ImportError:
        pass
    required = ["docassemble.base", "docassemble.webapp"]
    missing = [name for name in required if _import_error(name) == "missing"]
    broken = [(name, _import_error(name)) for name in required]
    broken = [(name, error) for name, error in broken if error not in (None, "missing")]
    if broken:
        name, error = broken[0]
        raise SystemExit(
            f"error: {name} is installed but failed to import; this is not a "
            "missing-package problem. Check native libraries and the target "
            f"environment. (underlying error: {error})"
        )
    if not missing:
        return
    if offline or not install_missing:
        raise SystemExit(
            "error: missing docassemble runtime package(s): "
            + ", ".join(missing)
            + ". Acquisition is disabled (offline mode); install them in "
            "the current interpreter and retry."
        )
    _install_missing_runtime(root_object, missing)
    importlib.invalidate_caches()
    still_missing = [name for name in required if _import_error(name) == "missing"]
    failures = [(name, _import_error(name)) for name in required]
    failures = [
        (name, error) for name, error in failures if error not in (None, "missing")
    ]
    if still_missing or failures:
        detail = ", ".join(
            still_missing or [f"{name}: {error}" for name, error in failures]
        )
        raise SystemExit(
            "error: runtime package acquisition completed but imports still "
            f"fail ({detail}). Verify the target interpreter."
        )


__all__ = ["ensure_importable"]
