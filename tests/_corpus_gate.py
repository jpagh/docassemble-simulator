"""False-green prevention for the external corpus gate (issue #12).

``mise run test:corpus`` provisions the modern Runtime compatibility lane,
validates the external fixture checkout, and selects the ``corpus``-marked
contract tests. Provisioning and a pytest selection alone cannot promise that
the suite ran: a fixture checkout whose tests all skip still produces a green
pytest session, and that would make the gate depend on local state rather than
on passing coverage.

This pytest plugin turns "the corpus suite passed" into a checked property of
the session. It activates only when ``DASIMULATOR_CORPUS_GATE`` is set (by
``scripts/test-corpus``), counts the tests carrying the ``corpus`` marker, and
at the end of a would-be-green session fails when no corpus test passed.
Individually skipped corpus tests stay legitimate skips once at least one
corpus test passed.

A collection-only run is not the gate: it does not enforce coverage.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

GATE_ENV = "DASIMULATOR_CORPUS_GATE"
CORPUS_MARKER = "corpus"


def gate_requested(environ: Mapping[str, str]) -> bool:
    """True when the session was started as the corpus gate."""
    return environ.get(GATE_ENV, "").strip() not in ("", "0")


@dataclass
class CorpusGate:
    """Accumulates corpus coverage for one gate session."""

    applied: set[str] = field(default_factory=set)
    passed: set[str] = field(default_factory=set)

    def record_setup(self, nodeid: str) -> None:
        self.applied.add(nodeid)

    def record_pass(self, nodeid: str) -> None:
        self.passed.add(nodeid)

    def failures(self) -> list[str]:
        if not self.applied:
            return [
                (
                    "no corpus-marked test ran: check the corpus marker and any "
                    "pytest arguments"
                )
            ]
        if not self.passed:
            return [
                (
                    "every corpus-marked test was skipped: the corpus gate has no "
                    "passing corpus coverage"
                ),
                (
                    "acquire the external fixtures and set "
                    "DASIMULATOR_DEMO_FIXTURES, or run mise run test:corpus "
                    "after provisioning them"
                ),
            ]
        return []


_GATE: CorpusGate | None = None


def pytest_configure(config) -> None:
    global _GATE
    if gate_requested(os.environ) and not config.getoption("collectonly"):
        _GATE = CorpusGate()


def pytest_runtest_logreport(report) -> None:
    if _GATE is None:
        return
    if CORPUS_MARKER not in getattr(report, "keywords", {}):
        return
    if report.when == "setup":
        _GATE.record_setup(report.nodeid)
    elif report.when == "call" and report.passed:
        _GATE.record_pass(report.nodeid)


def pytest_sessionfinish(session, exitstatus) -> None:
    global _GATE
    if _GATE is None:
        return
    gate = _GATE
    _GATE = None
    if session.exitstatus != pytest.ExitCode.OK:
        return
    problems = gate.failures()
    if not problems:
        return
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
    terminal = session.config.pluginmanager.get_plugin("terminalreporter")
    header = "corpus gate not satisfied:"
    if terminal is not None:
        terminal.write_line("")
        terminal.write_line(header, red=True)
        for problem in problems:
            terminal.write_line(f"  - {problem}", red=True)
    else:  # pragma: no cover - the terminal reporter is present in practice
        print(header, file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
