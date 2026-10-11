from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from docassemble_simulator._outcomes import ErrorKind
from docassemble_simulator.execution import (
    ExecutionFailure,
    ExplainResolution,
    InterviewExecution,
)


def _execution(tmp_path, store, catalog):
    execution = object.__new__(InterviewExecution)
    execution.root = tmp_path
    execution._identity = "docassemble.pkg:data/questions/main.yml"
    execution._store = store
    execution._catalog = catalog
    return execution


def test_resolution_explanation_without_saved_state_stays_static(tmp_path):
    report = {"variable": "shared", "session_state": "not saved"}

    class Store:
        def load(self):
            raise ExecutionFailure(
                ErrorKind.STATE, "no saved session; run `start` first"
            )

        def save(self, *args, **kwargs):
            raise AssertionError("read-only explanation must not save")

    class Catalog:
        def why(self, variable, **kwargs):
            assert variable == "shared"
            assert kwargs == {}
            return SimpleNamespace(result=report)

    outcome = _execution(tmp_path, Store(), Catalog()).run(ExplainResolution("shared"))

    assert outcome.ok
    assert outcome.result == {
        **report,
        "interview": "docassemble.pkg:data/questions/main.yml",
    }


def test_resolution_explanation_prepares_saved_state_without_committing(tmp_path):
    report = {"variable": "shared", "session_state": "saved"}
    calls = {}

    class Store:
        def load(self):
            calls["loaded"] = True
            return {"namespace": {"helper": object()}}

        def save(self, *args, **kwargs):
            raise AssertionError("read-only explanation must not save")

    class Catalog:
        def why(self, variable, namespace, **kwargs):
            calls["catalog"] = (variable, namespace, kwargs)
            return SimpleNamespace(result=report)

    @contextmanager
    def prepared(namespace):
        calls["prepared"] = namespace
        yield object(), object()

    execution = _execution(tmp_path, Store(), Catalog())
    execution._prepared_operation = prepared

    outcome = execution.run(ExplainResolution("shared"))

    assert outcome.ok
    assert outcome.result == {
        **report,
        "interview": "docassemble.pkg:data/questions/main.yml",
    }
    assert calls["loaded"] is True
    assert calls["prepared"] is calls["catalog"][1]
    assert calls["catalog"][0] == "shared"
    assert calls["catalog"][2]["interview"] is not None
