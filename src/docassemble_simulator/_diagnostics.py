"""Operation-local diagnostic capture for docassemble runtime traces."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class Diagnostic:
    """A non-fatal fact observed while an operation runs."""

    kind: str
    message: str
    details: dict[str, Any]


_ACTIVE: ContextVar[list[Diagnostic] | None] = ContextVar(
    "docassemble_simulator_diagnostics", default=None
)
_PENDING_CULPRIT: ContextVar[tuple[str, dict[str, Any]] | None] = ContextVar(
    "docassemble_simulator_pending_culprit", default=None
)
_ENABLED = True


def set_capture_enabled(enabled: bool) -> None:
    """Turn seek-trace capture on or off for this process."""
    global _ENABLED
    _ENABLED = bool(enabled)


def is_capture_enabled() -> bool:
    return _ENABLED


@contextmanager
def capture_diagnostics():
    """Collect diagnostics for one operation without sharing concurrent state."""
    collected: list[Diagnostic] = []
    token = _ACTIVE.set(collected)
    pending = _PENDING_CULPRIT.set(None)
    try:
        yield collected
    finally:
        _ACTIVE.reset(token)
        _PENDING_CULPRIT.reset(pending)


def is_collecting() -> bool:
    return _ACTIVE.get() is not None


def record_diagnostic(
    kind: str, message: str, details: dict[str, Any] | None = None
) -> None:
    """Record one non-fatal fact for the active operation, if any."""
    collected = _ACTIVE.get()
    if collected is None or not _ENABLED:
        return
    collected.append(Diagnostic(kind, message, details or {}))


def record_seeking(stages: list[Any] | None) -> None:
    """Convert docassemble's native seeking trace into typed diagnostics."""
    collected = _ACTIVE.get()
    if collected is None or not _ENABLED:
        return
    for stage in stages or ():
        if not isinstance(stage, dict) or stage.get("done"):
            continue
        details: dict[str, Any] = {}
        variable = stage.get("variable")
        if variable is not None:
            details["variable"] = str(variable)
        question = stage.get("question")
        question_name = getattr(question, "name", None)
        if question_name:
            details["question"] = str(question_name)
        reason = stage.get("reason")
        if reason:
            details["reason"] = str(reason)
        if not details:
            continue
        if "variable" in details:
            message = f"seeking {details['variable']}"
        elif "reason" in details:
            message = f"{details['reason']} question {details['question']}"
        else:
            message = f"considering question {details['question']}"
        collected.append(Diagnostic("variable-seek", message, details))
    pending = _PENDING_CULPRIT.get()
    if pending is not None:
        _PENDING_CULPRIT.set(None)
        record_culprit(*pending)


def record_culprit(variable: str, culprit: dict[str, Any]) -> None:
    """Attach the terminal seek culprit without rewriting tolerated events."""
    collected = _ACTIVE.get()
    if collected is None or not _ENABLED:
        return
    for index in range(len(collected) - 1, -1, -1):
        event = collected[index]
        if event.kind != "variable-seek":
            continue
        if event.details.get("variable") != str(variable):
            continue
        details = dict(event.details)
        details["culprit"] = culprit
        collected[index] = replace(event, details=details)
        return
    _PENDING_CULPRIT.set((str(variable), culprit))


def is_lazy_seek_log(message: Any) -> bool:
    """Whether a docassemble log line duplicates its structured seek trace."""
    text = str(message)
    return text.startswith(
        (
            "NameError exception during document assembly:",
            "UndefinedError exception during document assembly:",
        )
    )


__all__ = [
    "Diagnostic",
    "capture_diagnostics",
    "is_capture_enabled",
    "is_collecting",
    "is_lazy_seek_log",
    "record_culprit",
    "record_diagnostic",
    "record_seeking",
    "set_capture_enabled",
]


_SEEN_COMPILE_WARNINGS: set[tuple[Any, ...]] = set()
_PENDING_COMPILE_WARNINGS: dict[str, dict[tuple[Any, ...], Diagnostic]] = {}


@contextmanager
def capture_compile_warnings(interview: str):
    """Attribute Python compiler warnings only from authored docassemble code.

    The compiler uses synthetic filenames, so provenance comes from the active
    parser question rather than treating that filename as an interview path.
    All other warnings retain the caller's warning handler and filters.
    """
    import sys
    import warnings

    collected = _ACTIVE.get()
    if collected is not None:
        for key, event in _PENDING_COMPILE_WARNINGS.pop(interview, {}).items():
            if key not in _SEEN_COMPILE_WARNINGS:
                collected.append(event)
                _SEEN_COMPILE_WARNINGS.add(key)

    with warnings.catch_warnings():
        previous = warnings.showwarning

        def showwarning(message, category, filename, lineno, file=None, line=None):
            question = None
            if filename.startswith("<") and (
                issubclass(category, SyntaxWarning)
                or (
                    issubclass(category, DeprecationWarning)
                    and "invalid escape sequence" in str(message)
                )
            ):
                frame = sys._getframe(1)
                try:
                    while frame is not None:
                        if frame.f_globals.get("__name__") == "docassemble.base.parse":
                            candidate = frame.f_locals.get(
                                "question"
                            ) or frame.f_locals.get("self")
                            if getattr(candidate, "from_source", None) is not None:
                                question = candidate
                                break
                        frame = frame.f_back
                finally:
                    del frame
            if question is None:
                previous(message, category, filename, lineno, file=file, line=line)
                return
            source = question.from_source
            details = {
                "interview": interview,
                "source_file": str(getattr(source, "path", None) or interview),
                "line": getattr(question, "line_number", None),
                "question": str(
                    getattr(question, "name", "")
                    or (f"ID {question.id}" if getattr(question, "id", None) else "")
                ),
                "category": category.__name__,
                "compile_filename": filename,
                "compile_line": lineno,
            }
            text = str(message)[:1000]
            # Recompiles and AST scans can use another filepath or <unknown>;
            # warning identity belongs to the authored block, not that wrapper.
            key = (
                interview,
                details["source_file"],
                details["line"],
                details["question"],
                details["category"],
                details["compile_line"],
                text,
            )
            collected = _ACTIVE.get()
            if collected is None and key not in _SEEN_COMPILE_WARNINGS:
                pending = _PENDING_COMPILE_WARNINGS.setdefault(interview, {})
                if len(pending) < 20:
                    pending[key] = Diagnostic("compile-warning", text, details)
            if (
                collected is not None
                and key not in _SEEN_COMPILE_WARNINGS
                and sum(event.kind == "compile-warning" for event in collected) < 20
            ):
                collected.append(Diagnostic("compile-warning", text, details))
                _SEEN_COMPILE_WARNINGS.add(key)

        warnings.showwarning = showwarning
        warnings.filterwarnings("always", category=SyntaxWarning, module=r"^<")
        warnings.filterwarnings(
            "always",
            message=".*invalid escape sequence.*",
            category=DeprecationWarning,
            module=r"^<",
        )
        yield
