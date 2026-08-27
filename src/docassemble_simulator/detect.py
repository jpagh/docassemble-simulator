"""Locate a docassemble package in a directory and its interviews."""

from __future__ import annotations

import importlib
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

QUESTIONS_DIRS = ("data/questions", "data/sources")


def find_package_root(root: str | Path | None = None) -> Path:
    """Find the docassemble package root (a dir containing a `docassemble/` subpackage dir).

    Starts at `root` (default: cwd) and walks upward.
    """
    start = Path(root or Path.cwd()).resolve()
    if not start.is_dir():
        raise SystemExit(f"error: root is not a directory: {start}")
    for candidate in (start, *start.parents):
        da_dir = candidate / "docassemble"
        if da_dir.is_dir() and any(
            p.is_dir() and (p / "__init__.py").exists() for p in da_dir.iterdir()
        ):
            return candidate
    raise SystemExit(
        "error: no docassemble package found here or in parent directories "
        f"(looked for a 'docassemble/' package dir starting at {start})"
    )


def list_packages(root: str | Path) -> list[str]:
    """Return the short names of the packages inside the root's docassemble/ dir."""
    da_dir = Path(root).resolve() / "docassemble"
    names = []
    for p in sorted(da_dir.iterdir()):
        if p.is_dir() and (p / "__init__.py").exists():
            names.append(p.name)
    return names


def _import_error(module_name: str):
    """Return an import error, or None when a runtime module imports cleanly."""
    try:
        importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        # A missing dependency such as zbar is an installed-runtime failure,
        # not evidence that docassemble-base itself is absent.
        if error.name in {module_name, module_name.split(".")[0]}:
            return "missing"
        return error
    except ImportError as error:
        return error
    return None


def _locked_runtime_specs(root: Path, missing: list[str]) -> list[str]:
    """Find exact package pins from the active pyproject/uv lock when present."""
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
                # An explicit target pin is authoritative even when an old lock
                # file still records a different resolved version.
                if name in wanted and name not in specs and package.get("version"):
                    specs[name] = f"{name}=={package['version']}"
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            pass
    return [specs.get(name, name) for name in sorted(wanted)]


def _install_missing_runtime(root: Path, missing: list[str]) -> str:
    specs = _locked_runtime_specs(root, missing)
    uv = shutil.which("uv")
    if uv:
        command = [uv, "pip", "install", "--python", sys.executable, *specs]
    else:
        command = [sys.executable, "-m", "pip", "install", *specs]
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
    """Make `docassemble.<pkg>` importable from the package root.

    Docassemble resolves interview paths via importlib.resources on the
    package module, so it must be importable. Editable-installed packages
    already are; plain source trees get their root prepended to sys.path
    (the site-packages `docassemble` portion has no __init__.py, so both
    portions merge as one namespace package).
    """
    root_path = str(Path(root).resolve())
    # Always add plain source trees before checking docassemble.base. The
    # runtime may already provide docassemble.base while the selected package
    # itself is not installed in that interpreter.
    if root_path not in sys.path:
        sys.path.insert(0, root_path)
    root_path_obj = Path(root).resolve()
    # webapp imports its Redis connection at import time. Install the local
    # module shim before probing it, just as bootstrap does before registration.
    try:
        from docassemble_simulator.bootstrap import install_fake_redis

        install_fake_redis()
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
    if missing:
        if offline or not install_missing:
            raise SystemExit(
                "error: missing docassemble runtime package(s): "
                + ", ".join(missing)
                + ". Acquisition is disabled (offline mode); install them in "
                "the current interpreter and retry."
            )
        _install_missing_runtime(root_path_obj, missing)
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


def list_interviews(root: str | Path, package: str | None = None) -> list[str]:
    """Return interview paths ('docassemble.<pkg>:<file>.yml') found under root.

    Scans data/questions (and data/sources for .yml there) of every package.
    """
    da_dir = Path(root).resolve() / "docassemble"
    results: list[str] = []
    pkgs = [package] if package else [p for p in list_packages(root)]
    for pkg in pkgs:
        base = da_dir / pkg / "data" / "questions"
        if not base.is_dir():
            continue
        for yml in sorted(base.rglob("*.yml")) + sorted(base.rglob("*.yaml")):
            rel = yml.relative_to(da_dir / pkg)
            # skip scratch/backup files
            if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
                continue
            results.append(f"docassemble.{pkg}:{rel.as_posix()}")
    return results


def guess_main_interview(interviews: list[str]) -> str | None:
    """Prefer main.yml at the top of data/questions; then any main.yml; then first."""
    mains = [
        i
        for i in interviews
        if re.match(r"^docassemble\.[^:]+:data/questions/main\.ya?ml$", i)
    ]
    if mains:
        return mains[0]
    others = [i for i in interviews if i.endswith(("main.yml", "main.yaml"))]
    if others:
        return others[0]
    return interviews[0] if interviews else None


def resolve_interview(
    root: str | Path, requested: str | None = None
) -> tuple[str, list[str]]:
    """Resolve an interview reference to a full 'docassemble.pkg:path' string.

    Accepts: full path ('docassemble.pkg:main.yml'), bare filename
    ('main.yml' -> searched across packages), or None (auto-detect).
    Returns (interview_path, all_interviews).
    """
    interviews = list_interviews(root)
    if requested is None:
        chosen = guess_main_interview(interviews)
        if chosen is None:
            raise SystemExit(
                "error: no interview YAML files found under data/questions; "
                "pass one explicitly with --interview"
            )
        return chosen, interviews
    if ":" in requested and not requested.startswith("/"):
        return requested, interviews
    matches = [
        i
        for i in interviews
        if i.rsplit(":", 1)[1] == requested
        or i.rsplit(":", 1)[1].endswith("/" + requested)
    ]
    if len(matches) == 1:
        return matches[0], interviews
    if len(matches) > 1:
        raise SystemExit(
            "error: '" + requested + "' is ambiguous:\n  " + "\n  ".join(matches)
        )
    raise SystemExit(
        f"error: interview '{requested}' not found. Available:\n  "
        + "\n  ".join(interviews[:50])
    )
