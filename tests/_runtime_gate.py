"""False-green prevention for the two-family real-runtime gate (issue #11).

``mise run test:runtime`` provisions the ``da110`` and ``da19`` Runtime
compatibility lanes and points the real-runtime fixtures at their interpreters.
Provisioning and environment variables alone cannot promise that a family ran:
a lane whose applicable tests all skip still produces a green pytest session.

This pytest plugin turns "both families were exercised" into a checked property
of the session. It activates only when ``DASIMULATOR_RUNTIME_GATE`` is set (by
``scripts/test-runtime``), remembers which family each collected test exercises
(from the ``family_python`` fixture parameter or the ``real_python`` /
``real_python_19`` fixtures), and at the end of the session fails a green run
when a required family contributed no passing test or when a lane interpreter
variable is missing. Individually skipped capability tests stay legitimate
skips; only a family with no passing coverage at all fails the gate.

A collection-only run is not the gate: it does not enforce coverage.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import pytest

MODERN = "modern"
LEGACY = "legacy"
REQUIRED_FAMILIES = (MODERN, LEGACY)

GATE_ENV = "DASIMULATOR_RUNTIME_GATE"
INTERPRETER_ENV = {
    MODERN: "DASIMULATOR_REAL_PYTHON",
    LEGACY: "DASIMULATOR_REAL_PYTHON_19",
}
FAMILY_FIXTURE = "family_python"
MODERN_FIXTURES = frozenset({"real_python"})
LEGACY_FIXTURES = frozenset({"real_python_19"})


def gate_requested(environ: Mapping[str, str]) -> bool:
    """True when the session was started as the runtime gate."""
    return environ.get(GATE_ENV, "").strip() not in ("", "0")


def families_for_item(item) -> frozenset[str]:
    """The runtime families a collected test exercises, if attributable.

    A test that parameterizes ``family_python`` covers exactly the family of
    its current parameter. Otherwise the directly requested interpreter
    fixtures decide; a test requesting neither is not family coverage.
    """
    callspec = getattr(item, "callspec", None)
    parameters = getattr(callspec, "params", None) or {}
    family = parameters.get(FAMILY_FIXTURE)
    if family is not None:
        name = getattr(family, "value", family)
        return frozenset({name}) if name in REQUIRED_FAMILIES else frozenset()

    requested = set(getattr(item, "fixturenames", ()))
    families = set()
    if requested & MODERN_FIXTURES:
        families.add(MODERN)
    if requested & LEGACY_FIXTURES:
        families.add(LEGACY)
    return frozenset(families)


@dataclass
class RuntimeGate:
    """Accumulates per-family coverage for one gate session."""

    required: tuple[str, ...] = REQUIRED_FAMILIES
    applicable: dict[str, frozenset[str]] = field(default_factory=dict)
    passed: set[str] = field(default_factory=set)
    counts: dict[str, Counter[str]] = field(
        default_factory=lambda: {family: Counter() for family in REQUIRED_FAMILIES}
    )
    reported: dict[str, str] = field(default_factory=dict)

    def attribute(self, nodeid: str, families: Iterable[str]) -> None:
        resolved = frozenset(f for f in families if f in self.required)
        if resolved:
            self.applicable[nodeid] = resolved

    def record_report(self, report) -> None:
        """Record one test outcome, including setup-only skips/failures."""
        families = self.applicable.get(report.nodeid)
        if not families:
            families = frozenset(getattr(report, "_runtime_gate_families", ()))
            if families:
                self.applicable[report.nodeid] = families
        if not families:
            return
        if report.when == "call" or (
            report.when == "setup" and report.outcome in {"failed", "skipped"}
        ):
            outcome = report.outcome
        else:
            return
        if report.nodeid in self.reported:
            return
        self.reported[report.nodeid] = outcome
        for family in families:
            self.counts[family][outcome] += 1
        if outcome == "passed":
            self.passed.add(report.nodeid)

    def summary(self) -> list[str]:
        return [
            f"  {family}: "
            f"passed={self.counts[family]['passed']} "
            f"failed={self.counts[family]['failed']} "
            f"skipped={self.counts[family]['skipped']}"
            for family in self.required
        ]

    def failures(self, environ: Mapping[str, str]) -> list[str]:
        problems: list[str] = []
        for family in self.required:
            variable = INTERPRETER_ENV[family]
            if not environ.get(variable):
                problems.append(
                    f"{variable} is not set, so the gate cannot exercise the "
                    f"{family} runtime family"
                )
        for family in self.required:
            applicable = [
                nodeid
                for nodeid, families in self.applicable.items()
                if family in families
            ]
            if not applicable:
                problems.append(
                    f"no collected test exercises the {family} runtime family"
                )
            elif not any(nodeid in self.passed for nodeid in applicable):
                problems.append(
                    f"every applicable {family} test was skipped or deselected: "
                    f"the gate has no passing {family} coverage"
                )
        return problems


_GATE: RuntimeGate | None = None


def pytest_configure(config) -> None:
    global _GATE
    # Workers keep a local accumulator only long enough to attach each test's
    # family to its serialized report. The controller receives those reports
    # and performs the one final aggregate check.
    if gate_requested(os.environ) and not config.getoption("collectonly"):
        _GATE = RuntimeGate()


def pytest_collection_modifyitems(config, items) -> None:
    if _GATE is None:
        return
    for item in items:
        _GATE.attribute(item.nodeid, families_for_item(item))


def pytest_collection_finish(session) -> None:
    """Attribute items on xdist controllers, which skip modifyitems."""
    if _GATE is None:
        return
    for item in session.items:
        _GATE.attribute(item.nodeid, families_for_item(item))


@pytest.hookimpl(hookwrapper=True)
def pytest_report_to_serializable(config, report):
    outcome = yield
    data = outcome.get_result()
    if data is not None and _GATE is not None:
        families = _GATE.applicable.get(report.nodeid)
        if families:
            data["runtime_gate_families"] = sorted(families)
    outcome.force_result(data)


@pytest.hookimpl(hookwrapper=True)
def pytest_report_from_serializable(config, data):
    outcome = yield
    report = outcome.get_result()
    families = data.get("runtime_gate_families")
    if report is not None and families:
        report._runtime_gate_families = frozenset(families)
    outcome.force_result(report)


def pytest_runtest_logreport(report) -> None:
    if _GATE is None:
        return
    _GATE.record_report(report)


def pytest_sessionfinish(session, exitstatus) -> None:
    global _GATE
    if _GATE is None:
        return
    if hasattr(session.config, "workerinput"):
        _GATE = None
        return
    problems = _GATE.failures(os.environ)
    summary = _GATE.summary()
    _GATE = None
    terminal = session.config.pluginmanager.get_plugin("terminalreporter")
    if terminal is not None:
        terminal.write_line("")
        terminal.write_line("runtime gate coverage:")
        for line in summary:
            terminal.write_line(line)
    else:  # pragma: no cover - the terminal reporter is present in practice
        print("runtime gate coverage:", file=sys.stderr)
        for line in summary:
            print(line, file=sys.stderr)
    if not problems:
        return
    if session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    header = "runtime gate not satisfied:"
    if terminal is not None:
        terminal.write_line(header, red=True)
        for problem in problems:
            terminal.write_line(f"  - {problem}", red=True)
    else:
        print(header, file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
