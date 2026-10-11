"""Read-only interview discovery, compilation, and metadata inspection."""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docassemble_simulator.compatibility import (
    AssemblyLineCompatibilityError,
    require_assemblyline_compatibility,
)

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


def _normalize_guards(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _guards_from_source(source_code: str | None) -> list[str]:
    """Recover authored ``if:`` values when the compiler dropped them."""
    if not source_code:
        return []
    try:
        import yaml

        document = yaml.safe_load(source_code)
    except Exception:  # noqa: BLE001 - malformed authored source has a text fallback
        document = None
    if isinstance(document, dict) and "if" in document:
        return _normalize_guards(document["if"])

    match = re.search(r"(?m)^\s*if\s*:\s*(.*)$", source_code)
    if match and match.group(1).strip():
        return [match.group(1).strip()]
    if match:
        values = []
        for line in source_code.splitlines()[
            source_code[: match.start()].count("\n") + 1 :
        ]:
            item = re.match(r"^\s*-\s+(.+?)\s*$", line)
            if not item:
                break
            values.append(item.group(1))
        return values
    return []


def _source_block(source_text: str, line_number: int | None) -> str:
    if not source_text or not line_number:
        return source_text
    lines = source_text.splitlines()
    start = max(0, min(len(lines), line_number - 1))
    if start < len(lines) and lines[start].strip() != "---":
        marker = start
        while marker > 0 and lines[marker].strip() != "---":
            marker -= 1
        if lines[marker].strip() == "---":
            start = marker
    end = start + 1
    while end < len(lines) and lines[end].strip() != "---":
        end += 1
    return "\n".join(lines[start:end])


def _source_details(question) -> tuple[str, int | None]:
    source = getattr(question, "from_source", None)
    source_file = getattr(source, "path", None) or getattr(source, "filepath", None)
    line_number = getattr(question, "line_number", None)
    try:
        line = int(line_number) if line_number is not None else None
    except (TypeError, ValueError):
        line = None
    return str(source_file or "unknown"), line


def _provenance(question) -> dict[str, Any]:
    source_file, line = _source_details(question)
    debug = getattr(question, "data_for_debug", None)
    authored_guards = (
        debug.get("if") if isinstance(debug, dict) and "if" in debug else None
    )
    guards = _normalize_guards(authored_guards)
    if not guards:
        guards = _guards_from_source(getattr(question, "source_code", None))
    if not guards:
        source = getattr(question, "from_source", None)
        filepath = getattr(source, "filepath", None)
        if filepath:
            try:
                source_text = Path(filepath).read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                source_text = None
            guards = _guards_from_source(_source_block(source_text, line))

    guard = "none" if not guards else (guards[0] if len(guards) == 1 else guards)
    explicit_id = getattr(question, "id", None)
    if explicit_id is None and isinstance(debug, dict):
        explicit_id = debug.get("id")
    result = {
        "source_file": source_file,
        "line": line,
        "location": f"{source_file}:{line or '?'}",
        "block_type": getattr(question, "question_type", "?"),
        "guards": guards,
        "guard": guard,
        # Keep the existing condition key, but make it authored text rather
        # than the repr of compiled code objects.
        "condition": guard,
    }
    if explicit_id is not None:
        result["id"] = str(explicit_id)
    return result


class InterviewCatalog:
    """Compile and inspect interview definitions without creating session state."""

    def __init__(self, root: str | Path, selector: str | None = None):
        self.root = Path(root).resolve()
        self.selector = selector

    @property
    def identity(self) -> str:
        return resolve_interview(self.root, self.selector)[0]

    def _compile(self, identity: str | None = None):
        """Internal definition loader shared with execution.

        The AssemblyLine compatibility probe runs before the target Interview:
        a runtime pair that cannot compile the standard birthdate metadata
        would otherwise surface as an authored-looking label error. Authored
        parse errors after a passing probe keep their compile-error behavior.
        """
        require_assemblyline_compatibility()
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
            except AssemblyLineCompatibilityError:
                # A runtime-pair incapability is not a per-Interview compile
                # failure; surface it as one typed environment failure.
                raise
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
        failures = sum(not row["ok"] for row in rows)
        return CatalogOutcome(
            None,
            {
                "checked": len(rows),
                "failures": failures,
                "totals": {
                    "checked": len(rows),
                    "ok": len(rows) - failures,
                    "failed": failures,
                },
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
            provenance = _provenance(question)
            rows.append(
                {
                    "index": index,
                    "mandatory": bool(getattr(question, "is_mandatory", False)),
                    "name": name,
                    "first_variable": variables[0] if variables else "",
                    "variables": variables or None,
                    **provenance,
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
                            **_provenance(question),
                        }
                    )
            mapping[variable] = screens or None
        return CatalogOutcome(self.identity, {"index": mapping})

    def _compile_for_inspection(self):
        try:
            return self._compile()
        except AssemblyLineCompatibilityError:
            raise
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
