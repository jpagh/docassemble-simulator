"""Locate a docassemble package in a directory and its interviews."""
from __future__ import annotations

import re
import sys
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


def ensure_importable(root: str | Path) -> None:
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
    try:
        import docassemble.base  # noqa: F401
    except ImportError as err:
        raise SystemExit(
            "error: docassemble.base is not importable in this interpreter. "
            "Install docassemble-simulator into the target package's virtualenv, e.g.:\n"
            f"  uv pip install --python {root_path}/.venv/bin/python docassemble-simulator\n"
            f"or run: {root_path}/.venv/bin/docassemble-simulator <command>\n"
            f"(underlying error: {err})"
        ) from err


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
    others = [i for i in interviews if i.endswith("main.yml") or i.endswith("main.yaml")]
    if others:
        return others[0]
    return interviews[0] if interviews else None


def resolve_interview(root: str | Path, requested: str | None = None) -> tuple[str, list[str]]:
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
