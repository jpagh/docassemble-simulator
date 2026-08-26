"""Private typed outcome vocabulary shared by simulator modules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Generic, TypeVar


class ErrorKind(StrEnum):
    INPUT = "input"
    STATE = "state"
    ANSWER_INPUT = "answer-input"
    VALIDATION = "validation"
    EXECUTION = "execution"
    SEEK = "seek"
    RENDER = "render"
    COMPILE = "compile"
    FAULT = "fault"
    WORKSPACE = "workspace"
    CONFIGURATION = "configuration"


@dataclass(frozen=True)
class Failure:
    kind: ErrorKind
    message: str
    details: dict[str, Any] | None = None

    def __post_init__(self):
        object.__setattr__(self, "kind", ErrorKind(self.kind))
        if self.details is None:
            object.__setattr__(self, "details", {})


T = TypeVar("T")


@dataclass(frozen=True)
class Outcome(Generic[T]):
    ok: bool
    result: T | None = None
    error: Failure | None = None
