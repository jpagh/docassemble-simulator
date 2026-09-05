"""Read-only interview discovery, compilation, and metadata inspection."""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def list_packages(root: str | Path) -> list[str]:
    """Return package names from a docassemble source workspace."""
    directory = Path(root).resolve() / "docassemble"
    return [
        path.name
        for path in sorted(directory.iterdir())
        if path.is_dir() and (path / "__init__.py").exists()
    ]


def list_interviews(root: str | Path, package: str | None = None) -> list[str]:
    """Return canonical interview identities discovered in the catalog."""
    da_dir = Path(root).resolve() / "docassemble"
    results: list[str] = []
    packages = [package] if package else list_packages(root)
    for package_name in packages:
        base = da_dir / package_name / "data" / "questions"
        if not base.is_dir():
            continue
        for yaml_path in sorted(base.rglob("*.yml")) + sorted(base.rglob("*.yaml")):
            relative = yaml_path.relative_to(da_dir / package_name)
            if any(
                part.startswith(".") or part == "__pycache__" for part in relative.parts
            ):
                continue
            results.append(f"docassemble.{package_name}:{relative.as_posix()}")
    return results


def guess_main_interview(interviews: list[str]) -> str | None:
    """Prefer a top-level main interview, then any main, then the first."""
    mains = [
        identity
        for identity in interviews
        if re.match(r"^docassemble\.[^:]+:data/questions/main\.ya?ml$", identity)
    ]
    if mains:
        return mains[0]
    others = [
        identity
        for identity in interviews
        if identity.endswith(("main.yml", "main.yaml"))
    ]
    return others[0] if others else (interviews[0] if interviews else None)


def resolve_interview(
    root: str | Path, requested: str | None = None
) -> tuple[str, list[str]]:
    """Resolve a selector to one canonical Interview definition identity."""
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
        identity
        for identity in interviews
        if identity.rsplit(":", 1)[1] == requested
        or identity.rsplit(":", 1)[1].endswith("/" + requested)
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


class CatalogFailure(Exception):
    """A user-facing interview definition compilation failure."""


@dataclass(frozen=True)
class CatalogOutcome:
    interview: str | None
    result: Any


class InterviewCatalog:
    """Compile and inspect interview definitions without creating session state."""

    def __init__(self, root: str | Path, selector: str | None = None):
        self.root = Path(root).resolve()
        self.selector = selector

    @property
    def identity(self) -> str:
        return resolve_interview(self.root, self.selector)[0]

    def _compile(self, identity: str | None = None):
        """Internal definition loader shared with execution."""
        from docassemble.base.interview_cache import get_interview

        from docassemble_simulator._runtime import runtime_context

        selected = identity or self.identity
        saved_argv = list(sys.argv)
        sys.argv[:] = ["docassemble-simulator", selected]
        try:
            with runtime_context():
                return get_interview(selected)
        finally:
            sys.argv[:] = saved_argv

    def check(self) -> CatalogOutcome:
        identities = [self.identity] if self.selector else list_interviews(self.root)
        rows = []
        for identity in identities:
            try:
                interview = self._compile(identity)
                rows.append(
                    {
                        "interview": identity,
                        "ok": True,
                        "blocks": len(interview.questions_list),
                        "questions": sum(
                            getattr(q, "question_type", "") in {"question", "fields"}
                            for q in interview.questions_list
                        ),
                    }
                )
            except Exception as error:  # noqa: BLE001 - DAError varies by runtime release
                logger.debug("interview %r failed to compile: %s", identity, error)
                rows.append(
                    {
                        "interview": identity,
                        "ok": False,
                        "error_type": type(error).__name__,
                        "message": str(error)[:300],
                    }
                )
        return CatalogOutcome(
            None,
            {
                "checked": len(rows),
                "failures": sum(not row["ok"] for row in rows),
                "results": rows,
            },
        )

    def questions(self, contains: str | None = None) -> CatalogOutcome:
        from docassemble_simulator.describe import field_variable

        interview = self._compile_for_inspection()
        rows = []
        for index, question in enumerate(interview.questions_list):
            variables = []
            for field in getattr(question, "fields", None) or []:
                variables.append(field_variable(field))
            name = getattr(question, "name", None)
            if (
                contains
                and contains.lower() not in " ".join(variables + [name or ""]).lower()
            ):
                continue
            condition = getattr(question, "condition", "") or ""
            rows.append(
                {
                    "index": index,
                    "block_type": getattr(question, "question_type", "?"),
                    "mandatory": bool(getattr(question, "is_mandatory", False)),
                    "name": name,
                    "first_variable": variables[0] if variables else "",
                    "variables": variables or None,
                    "condition": condition[:120] or None,
                }
            )
        return CatalogOutcome(self.identity, {"blocks": rows})

    def index(self, contains: str | None = None) -> CatalogOutcome:
        interview = self._compile_for_inspection()
        mapping = {}
        for variable, entry in interview.questions.items():
            if contains and contains.lower() not in variable.lower():
                continue
            screens = []
            for language in ("en", "*"):
                for question in entry.get(language) or []:
                    screens.append(
                        {
                            "language": language,
                            "question_name": getattr(question, "name", None),
                        }
                    )
            mapping[variable] = screens or None
        return CatalogOutcome(self.identity, {"index": mapping})

    def _compile_for_inspection(self):
        try:
            return self._compile()
        except Exception as error:  # includes DAError/DAErrorMissingVariable
            raise CatalogFailure(f"{type(error).__name__}: {error}") from error


__all__ = [
    "CatalogFailure",
    "CatalogOutcome",
    "InterviewCatalog",
    "guess_main_interview",
    "list_interviews",
    "list_packages",
    "resolve_interview",
]
