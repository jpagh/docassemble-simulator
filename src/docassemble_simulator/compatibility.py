"""Executable docassemble/AssemblyLine runtime compatibility contract.

The simulator runs the real docassemble compiler inside the target package's
interpreter.  A small set of docassemble and AssemblyLine releases are tested
together; outside that set, a compile failure can belong to the runtime pair
rather than to the authored Interview.

This module makes the contract executable:

* it reports the target interpreter's runtime family and installed package
  versions;
* it compiles a minimal probe containing the standard AssemblyLine birthdate
  field metadata (``datatype: BirthDate`` with ``alMonthLabel`` /
  ``alDayLabel`` / ``alYearLabel``) through the real docassemble compiler;
* when the probe fails and AssemblyLine is installed, it raises a typed
  :class:`AssemblyLineCompatibilityError` naming the runtime family, package
  versions, failing capability, and recovery direction.

The probe never installs, upgrades, downgrades, or edits a package.  It is
compiled in memory, so it also never creates Working state or a Saved session.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any

from docassemble_simulator._runtime import RuntimeCompatibilityError, runtime_context

logger = logging.getLogger(__name__)

#: Capabilities the simulator relies on for standard AssemblyLine interviews.
PROBE_CAPABILITY = (
    "AssemblyLine BirthDate field metadata (alMonthLabel, alDayLabel, alYearLabel)"
)

#: In-memory Interview identity for the probe; never resolved from a package.
PROBE_IDENTITY = "docassemble.simulator_probe:data/questions/compatibility.yml"

PROBE_SOURCE = """\
---
id: simulator-assemblyline-compatibility-probe
mandatory: True
question: |
  AssemblyLine compatibility probe
fields:
  - Birthdate: simulator_probe_birthdate
    datatype: BirthDate
    alMonthLabel: ${ word('Month') }
    alDayLabel: ${ word('Day') }
    alYearLabel: ${ word('Year') }
"""

#: Dependency pairs the real-runtime lane provisions and tests together.
#: A dependency lower bound is not evidence that arbitrary later releases work.
SUPPORTED_MATRIX = (
    (
        "docassemble 1.9.x with docassemble.AssemblyLine 4.8.x "
        "(docassemble.ALToolbox 0.19.x)"
    ),
    (
        "docassemble 1.10.x with docassemble.AssemblyLine 4.8.x "
        "(docassemble.ALToolbox 0.19.x)"
    ),
)

RECOVERY_DIRECTION = (
    "Provision the target package interpreter with a tested compatible pair "
    "from docs/runtime-compatibility.md (for example "
    "docassemble-assemblyline==4.8.* with docassemble-ALToolbox>=0.19,<0.20), "
    "or run the simulator in an interpreter that already has one. Compatibility "
    "checks never install, upgrade, downgrade, or edit installed packages."
)

_DISTRIBUTIONS = (
    "docassemble-base",
    "docassemble-webapp",
    "docassemble-assemblyline",
    "docassemble-altoolbox",
)
_ASSEMBLYLINE_DISTRIBUTIONS = (
    "docassemble-assemblyline",
    "docassemble-altoolbox",
)
_ASSEMBLYLINE_MODULES = ("docassemble.AssemblyLine", "docassemble.ALToolbox")
_FAMILY_LABELS = {
    "legacy": "legacy (docassemble 1.9.x)",
    "modern": "modern (docassemble 1.10.x)",
    "unknown": "unknown (docassemble runtime not importable)",
}


class AssemblyLineCompatibilityError(RuntimeCompatibilityError):
    """The installed docassemble/AssemblyLine pair fails a required capability."""

    def __init__(self, message: str, details: dict[str, Any]):
        super().__init__(message)
        self.details = dict(details)


@dataclass(frozen=True)
class RuntimeEnvironment:
    """The target interpreter's runtime family and installed package versions."""

    family: str
    interpreter: str
    versions: dict[str, str | None] = field(default_factory=dict)

    def as_details(self) -> dict[str, Any]:
        details: dict[str, Any] = {
            "runtime_family": _FAMILY_LABELS.get(self.family, self.family),
            "interpreter": self.interpreter,
        }
        for key in _DISTRIBUTIONS:
            details[key.replace("-", "_")] = self.versions.get(key) or "not installed"
        return details


def _installed_version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except Exception:  # noqa: BLE001 - absent or unreadable metadata is not fatal
        return None


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:  # noqa: BLE001 - stub/incomplete runtimes are not fatal
        return False


def runtime_family() -> str:
    """Classify the installed runtime as legacy, modern, or unknown."""
    if not _module_available("docassemble.base"):
        return "unknown"
    if _module_available("docassemble.base.thread_context"):
        return "modern"
    return "legacy"


def collect_environment() -> RuntimeEnvironment:
    return RuntimeEnvironment(
        family=runtime_family(),
        interpreter=sys.executable,
        versions={key: _installed_version(key) for key in _DISTRIBUTIONS},
    )


def assemblyline_installed() -> bool:
    """Whether an AssemblyLine/ALToolbox distribution is present at all."""
    if any(_installed_version(name) for name in _ASSEMBLYLINE_DISTRIBUTIONS):
        return True
    return any(_module_available(name) for name in _ASSEMBLYLINE_MODULES)


def _compiler_available() -> bool:
    """Whether the real docassemble compiler is importable in this process.

    Stub runtimes in unit tests replace ``docassemble.base.parse``; there is
    nothing for the probe to compile against, so it must not engage and must
    not memoize a stub failure.
    """
    try:
        from docassemble.base.parse import (  # noqa: F401
            Interview,
            InterviewSourceString,
        )
    except Exception:  # noqa: BLE001 - any stub/partial runtime is not the probe target
        return False
    return True


def _compile_birthdate_probe() -> None:
    """Compile the probe with the real compiler; raises on any failure."""
    from docassemble.base.parse import Interview, InterviewSourceString

    with runtime_context():
        source = InterviewSourceString(
            content=PROBE_SOURCE,
            path=PROBE_IDENTITY,
            package="docassemble.simulator_probe",
        )
        Interview(source=source)


def _error_details(
    environment: RuntimeEnvironment, error: BaseException
) -> dict[str, Any]:
    details = environment.as_details()
    details.update(
        {
            "failing_capability": PROBE_CAPABILITY,
            "probe_identity": PROBE_IDENTITY,
            "recovery": RECOVERY_DIRECTION,
            "tested_matrix": list(SUPPORTED_MATRIX),
            "underlying_error": f"{type(error).__name__}: {str(error)[:500]}",
        }
    )
    return details


def _error_message(environment: RuntimeEnvironment, error: BaseException) -> str:
    versions = environment.versions
    return (
        "runtime compatibility failure: installed "
        f"{versions.get('docassemble-base') or 'docassemble-base (unknown version)'} "
        f"with {versions.get('docassemble-assemblyline') or 'AssemblyLine (not installed)'} "
        f"({versions.get('docassemble-altoolbox') or 'ALToolbox (not installed)'}) "
        f"cannot compile the {PROBE_CAPABILITY}. The simulator will not rewrite "
        f"installed AssemblyLine definitions or packages. Underlying error: "
        f"{str(error)[:300]}"
    )


_PROBE_DONE = False
_PROBE_FAILURE: AssemblyLineCompatibilityError | None = None


def require_assemblyline_compatibility() -> None:
    """Raise a typed, actionable failure when the probe cannot compile.

    The probe only applies when AssemblyLine is installed: an environment
    without AssemblyLine has no standard birthdate metadata to supply. When
    the probe fails, the error carries the runtime family, versions, failing
    capability, and recovery direction instead of a raw parser traceback.
    """
    global _PROBE_DONE, _PROBE_FAILURE
    if not assemblyline_installed() or not _compiler_available():
        return
    if _PROBE_DONE:
        if _PROBE_FAILURE is not None:
            raise _PROBE_FAILURE
        return
    _PROBE_DONE = True
    environment = collect_environment()
    try:
        _compile_birthdate_probe()
    except Exception as error:
        logger.debug("AssemblyLine birthdate compatibility probe failed: %s", error)
        failure = AssemblyLineCompatibilityError(
            _error_message(environment, error),
            _error_details(environment, error),
        )
        _PROBE_FAILURE = failure
        raise failure from error


def compatibility_report() -> dict[str, Any]:
    """Non-mutating environment report for ``info`` (does not run the probe)."""
    environment = collect_environment()
    return {
        **environment.as_details(),
        "assemblyline_installed": assemblyline_installed(),
        "tested_matrix": list(SUPPORTED_MATRIX),
        "probe": {
            "capability": PROBE_CAPABILITY,
            "identity": PROBE_IDENTITY,
            "runs_on": "check, questions, index, and execution commands",
        },
        "recovery": RECOVERY_DIRECTION,
    }


def reset_compatibility_cache() -> None:
    """Forget the memoized probe result (tests and embedders only)."""
    global _PROBE_DONE, _PROBE_FAILURE
    _PROBE_DONE = False
    _PROBE_FAILURE = None


__all__ = [
    "PROBE_CAPABILITY",
    "PROBE_IDENTITY",
    "PROBE_SOURCE",
    "RECOVERY_DIRECTION",
    "SUPPORTED_MATRIX",
    "AssemblyLineCompatibilityError",
    "RuntimeEnvironment",
    "assemblyline_installed",
    "collect_environment",
    "compatibility_report",
    "require_assemblyline_compatibility",
    "reset_compatibility_cache",
    "runtime_family",
]
