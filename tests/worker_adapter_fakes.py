"""Shared doubles for the command adapter's worker-request tests."""

from __future__ import annotations

from docassemble_simulator.execution import ExecutionScope

IDENTITY = "docassemble.example:data/questions/main.yml"


class AdapterFakeExecution:
    """A worker execution whose operation path must never serve a request.

    The command adapter maps each request onto its own seam (the renderer, the
    catalog payload, or a trace seam), so a test that sends one through the
    worker's generic handler registry proves the adapter never fell through to
    ``Worker.execution.run``.
    """

    _identity = IDENTITY

    def __init__(self, tmp_path):
        self.scope = ExecutionScope(
            tmp_path,
            self._identity,
            "",
            tmp_path / "sessions",
            tmp_path / "sessions" / "main.pkl",
        )
        # A shared compile authority for the catalog and trace adapters.
        self.catalog = None
        self.operations = []

    def run(self, operation):
        self.operations.append(operation)
        raise AssertionError("an adapter request must not run an execution operation")

    def reset(self):
        pass
