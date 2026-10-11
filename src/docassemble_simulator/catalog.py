"""Read-only interview discovery, compilation, and metadata inspection."""

from __future__ import annotations

import ast
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


def _guards_are_provably_disjoint(left: list[str], right: list[str]) -> bool:
    def signature(expression: str):
        try:
            node = ast.parse(expression, mode="eval").body
        except (SyntaxError, ValueError):
            return None
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            return None
        if not isinstance(node.ops[0], (ast.Eq, ast.Is)):
            return None
        if not isinstance(node.left, (ast.Name, ast.Attribute)):
            return None
        if len(node.comparators) != 1 or not isinstance(
            node.comparators[0], ast.Constant
        ):
            return None
        return ast.unparse(node.left), node.comparators[0].value

    left_signatures = [signature(item) for item in left]
    right_signatures = [signature(item) for item in right]
    if not left_signatures or not right_signatures:
        return False
    return all(
        left_signature is not None
        and right_signature is not None
        and left_signature[0] == right_signature[0]
        and left_signature[1] != right_signature[1]
        for left_signature in left_signatures
        for right_signature in right_signatures
    )


def _explicit_id(question) -> str | None:
    explicit_id = getattr(question, "id", None)
    debug = getattr(question, "data_for_debug", None)
    if explicit_id is None and isinstance(debug, dict):
        explicit_id = debug.get("id")
    if explicit_id is None:
        return None
    value = str(explicit_id).strip()
    return value or None


def _id_relationship(question) -> str | None:
    debug = getattr(question, "data_for_debug", None)
    authored = debug if isinstance(debug, dict) else {}
    try:
        import yaml

        parsed = yaml.safe_load(getattr(question, "source_code", ""))
        source_authored = parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001 - provenance must not break checking
        source_authored = {}
    for name in ("override", "overrides", "supersede", "supersedes"):
        value = getattr(question, name, None)
        if value is None:
            value = authored.get(name, source_authored.get(name))
        if value:
            return str(value) if not isinstance(value, bool) else name
    return None


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
    explicit_id = _explicit_id(question)
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
        result["id"] = explicit_id
    return result


def _duplicate_id_report(interview, interview_identity: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for question in getattr(interview, "questions_list", None) or ():
        explicit_id = _explicit_id(question)
        if explicit_id is None:
            continue
        block = _provenance(question)
        block["relationship"] = _id_relationship(question)
        grouped.setdefault(explicit_id, []).append(block)

    reports = []
    for explicit_id, blocks in grouped.items():
        if len(blocks) < 2:
            continue
        deliberate = any(block["relationship"] for block in blocks)
        reports.append(
            {
                "id": explicit_id,
                "kind": "deliberate-override" if deliberate else "duplicate-id",
                "interview": interview_identity,
                "warning": True,
                "blocks": blocks,
            }
        )
    return reports


class InterviewCatalog:
    """Compile and inspect interview definitions without creating session state."""

    def __init__(
        self,
        root: str | Path,
        selector: str | None = None,
        *,
        cache_compiled: bool = False,
    ):
        self.root = Path(root).resolve()
        self.selector = selector
        # Only persistent workers opt in. One-shot and test callers retain the
        # historical fresh-compile behavior, while a worker's catalog cache is
        # still scoped to one root/selector/process.
        self._cache_compiled = cache_compiled
        self._compiled: dict[str, Any] = {}

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
        cached = self._compiled.get(selected) if self._cache_compiled else None
        if cached is not None:
            return cached
        saved_argv = list(sys.argv)
        sys.argv[:] = ["docassemble-simulator", selected]
        try:
            with runtime_context():
                interview = get_interview(selected)
        finally:
            sys.argv[:] = saved_argv
        if self._cache_compiled:
            self._compiled[selected] = interview
        return interview

    def check(self) -> CatalogOutcome:
        identities = [self.identity] if self.selector else list_interviews(self.root)
        rows = []
        for identity in identities:
            try:
                interview = self._compile(identity)
                duplicate_ids = _duplicate_id_report(interview, identity)
                rows.append(
                    {
                        "interview": identity,
                        "ok": True,
                        "blocks": len(interview.questions_list),
                        "questions": sum(
                            getattr(q, "question_type", "") in {"question", "fields"}
                            for q in interview.questions_list
                        ),
                        "duplicate_ids": duplicate_ids,
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
        duplicate_ids = [
            duplicate for row in rows for duplicate in row.get("duplicate_ids", [])
        ]
        return CatalogOutcome(
            None,
            {
                "checked": len(rows),
                "failures": failures,
                "duplicate_ids": duplicate_ids,
                "totals": {
                    "checked": len(rows),
                    "ok": len(rows) - failures,
                    "failed": failures,
                    "duplicate_ids": len(
                        [
                            item
                            for item in duplicate_ids
                            if item["kind"] == "duplicate-id"
                        ]
                    ),
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

    def why(
        self,
        variable: str,
        namespace: dict[str, Any] | None = None,
        *,
        interview=None,
        language: str | None = None,
    ):
        """Explain resolution using the compiled runtime's candidate order.

        ``interview`` is supplied by a prepared execution when a saved
        session is available.  Keeping compilation and guard evaluation in
        that execution context lets real runtime objects and helpers behave as
        they do during seeking, while the returned report contains only plain
        values.
        """
        interview = interview or self._compile_for_inspection()
        entry = (getattr(interview, "questions", {}) or {}).get(variable) or {}
        if language is None:
            try:
                from docassemble.base.functions import get_language

                language = get_language()
            except (ImportError, AttributeError, RuntimeError):
                language = None
        language = language or getattr(interview, "default_language", None) or "en"
        questions = []
        for candidate_language in (language, "*"):
            values = entry.get(candidate_language) or []
            sorter = getattr(interview, "sort_with_orderings", None)
            ordered = sorter(values) if callable(sorter) else reversed(values)
            questions.extend((question, candidate_language) for question in ordered)

        candidates = []
        for order, (question, language) in enumerate(questions, start=1):
            provenance = _provenance(question)
            guards = provenance["guards"]
            live = None
            evaluation_error = None
            if namespace is not None:
                if not guards:
                    live = True
                else:
                    try:
                        live = all(bool(eval(guard, namespace)) for guard in guards)
                    except Exception as error:  # noqa: BLE001 - guard code varies by runtime
                        live = None
                        evaluation_error = f"{type(error).__name__}: {error}"
            candidate = {
                "order": order,
                "language": language,
                "winner": False,
                "live": live,
                "provenance": provenance,
                **provenance,
            }
            if evaluation_error:
                candidate["guard_error"] = evaluation_error
            candidates.append(candidate)

        live_indexes = [
            index
            for index, candidate in enumerate(candidates)
            if candidate["live"] is True
        ]
        winner_index = (
            live_indexes[0]
            if namespace is not None and live_indexes
            else (0 if namespace is None and candidates else None)
        )
        if winner_index is not None:
            candidates[winner_index]["winner"] = True

        statically_disjoint = all(
            _guards_are_provably_disjoint(
                candidates[left]["guards"], candidates[right]["guards"]
            )
            for left in range(len(candidates))
            for right in range(left + 1, len(candidates))
        )
        unguarded_duplicate = len(candidates) > 1 and any(
            not candidate["guards"] for candidate in candidates
        )
        conflict = False
        if len(candidates) > 1:
            if namespace is None:
                conflict = not statically_disjoint
            else:
                conflict = len(live_indexes) > 1 or unguarded_duplicate
        winner = candidates[winner_index] if winner_index is not None else None
        return CatalogOutcome(
            self.identity,
            {
                "variable": variable,
                "order": "last-loaded-first",
                "session_state": "saved" if namespace is not None else "not saved",
                "candidates": candidates,
                "winner": winner,
                "conflict": conflict,
                "conflict_details": (
                    {
                        "kind": "resolution-conflict",
                        "variable": variable,
                        "candidates": [
                            candidate["provenance"] for candidate in candidates
                        ],
                    }
                    if conflict
                    else None
                ),
            },
        )

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
