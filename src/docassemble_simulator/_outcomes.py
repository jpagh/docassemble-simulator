"""Private typed outcome and failure vocabulary shared by simulator modules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Generic, TypeVar

from docassemble_simulator._diagnostics import Diagnostic


class ErrorKind(StrEnum):
    INPUT = "input"
    STATE = "state"
    ANSWER_INPUT = "answer-input"
    VALIDATION = "validation"
    EXECUTION = "execution"
    SEEK = "seek"
    RENDER = "render"
    COMPILE = "compile"
    RESOLUTION_CONFLICT = "resolution-conflict"
    DUPLICATE_ID = "duplicate-id"
    RUNTIME_COMPATIBILITY = "runtime-compatibility"
    FAULT = "fault"
    WORKSPACE = "workspace"
    CONFIGURATION = "configuration"
    UNRESOLVED_VARIABLE = "unresolved-variable"
    TRACE_MISMATCH = "trace-mismatch"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class Failure:
    kind: ErrorKind
    message: str
    details: dict[str, Any] | None = None

    def __post_init__(self):
        object.__setattr__(self, "kind", ErrorKind(self.kind))
        if self.details is None:
            object.__setattr__(self, "details", {})


class ExecutionFailure(Exception):
    """A typed failure raised while an operation is in flight.

    Execution and the shared screen-submission implementation both raise it,
    so it lives with the failure vocabulary they share (ADR-0002).
    :class:`Failure` is the value an outcome carries; this is the exception
    that becomes one.
    """

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.kind = ErrorKind(kind)
        self.details = details


def first_line(error: BaseException) -> str:
    """The one-line summary of an error message used in typed failures."""
    text = str(error).strip()
    return text.splitlines()[0] if text else type(error).__name__


@dataclass(frozen=True)
class PublishedAttachment:
    filename: str
    extension: str
    mimetype: str
    path: Path
    uri: str
    diagnostics: tuple[Diagnostic, ...] = ()
    structure: str | None = None


T = TypeVar("T")


@dataclass(frozen=True)
class Outcome(Generic[T]):
    ok: bool
    result: T | None = None
    error: Failure | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    attachments: tuple[PublishedAttachment, ...] = ()
