"""Operation-local diagnostic capture for docassemble runtime traces."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
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


@contextmanager
def capture_diagnostics():
    """Collect diagnostics for one operation without sharing concurrent state."""
    collected: list[Diagnostic] = []
    token = _ACTIVE.set(collected)
    try:
        yield collected
    finally:
        _ACTIVE.reset(token)


def is_collecting() -> bool:
    return _ACTIVE.get() is not None


def record_seeking(stages: list[Any] | None) -> None:
    """Convert docassemble's native seeking trace into typed diagnostics."""
    collected = _ACTIVE.get()
    if collected is None:
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
    "is_collecting",
    "is_lazy_seek_log",
    "record_seeking",
]
