"""The command adapter's ``render`` request for the persistent worker.

``worker.py`` stays an operation-only server: the ``render`` request is mapped
to the same ``InterviewRenderer``/``RenderRequest`` path as ``sim render`` by
``cli``'s command adapter and served through the worker's generic handler
seam. These tests pin the field mapping, the error shapes, and the protocol
envelope without a real runtime.
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from docassemble_simulator import cli
from docassemble_simulator import render as render_module
from docassemble_simulator._diagnostics import Diagnostic
from docassemble_simulator._outcomes import ErrorKind, Failure
from docassemble_simulator.execution import ExecutionScope
from docassemble_simulator.render import (
    FixtureSource,
    FreshSource,
    RenderOutcome,
    RenderRequest,
    RenderResult,
    SavedSessionSource,
    SnapshotSource,
)
from docassemble_simulator.worker import Worker, WorkerProtocolError, serve


class _FakeExecution:
    """A worker execution that must never serve a render request."""

    _identity = "docassemble.example:data/questions/main.yml"

    def __init__(self, tmp_path):
        self.scope = ExecutionScope(
            tmp_path,
            self._identity,
            "",
            tmp_path / "sessions",
            tmp_path / "sessions" / "main.pkl",
        )
        self.operations = []

    def run(self, operation):
        self.operations.append(operation)
        raise AssertionError("render must not run an execution operation")

    def reset(self):
        pass


def _args(config=None):
    return SimpleNamespace(_simulator_config=config or {})


def _worker(tmp_path, handlers):
    return Worker(
        tmp_path, execution=_FakeExecution(tmp_path), timeout=1, handlers=handlers
    )


def _render_handlers(args):
    return cli._worker_request_handlers(args)


def _capture_render(monkeypatch, outcome):
    """Return the list that receives every mapped ``RenderRequest``."""
    captured = []
    monkeypatch.setattr(
        render_module.InterviewRenderer,
        "render",
        lambda self, request: captured.append(request) or outcome,
    )
    return captured


def test_worker_render_maps_every_cli_source(tmp_path, monkeypatch):
    worker = SimpleNamespace(root=tmp_path, execution=object())
    captured = _capture_render(
        monkeypatch, RenderOutcome(True, RenderResult("form.docx", 4))
    )

    cli._render_worker_request(_args(), worker, {"template": "form.docx"})
    assert captured[-1] == RenderRequest("form.docx", SavedSessionSource(), True)

    cli._render_worker_request(
        _args(),
        worker,
        {"template": "form.docx", "source": "saved", "no_assemble": True},
    )
    assert captured[-1] == RenderRequest("form.docx", SavedSessionSource(), False)

    cli._render_worker_request(
        _args(), worker, {"template": "form.docx", "source": "fresh"}
    )
    assert captured[-1] == RenderRequest("form.docx", FreshSource(), True)

    fixture = tmp_path / "fixture.py"
    fixture.write_text("value = 1\n")
    cli._render_worker_request(
        _args(),
        worker,
        {"template": "form.docx", "source": "fixture", "path": str(fixture)},
    )
    # A fixture source never assembles, exactly as ``sim render --fixture``.
    assert captured[-1] == RenderRequest(
        "form.docx", FixtureSource(fixture.resolve()), False
    )

    snapshot = tmp_path / "state.snapshot"
    cli._render_worker_request(
        _args(),
        worker,
        {"template": "form.docx", "source": "snapshot", "path": str(snapshot)},
    )
    assert captured[-1] == RenderRequest(
        "form.docx", SnapshotSource(snapshot.resolve()), True
    )


def test_worker_render_maps_paths_bindings_and_config_defaults(tmp_path, monkeypatch):
    captured = _capture_render(
        monkeypatch, RenderOutcome(True, RenderResult("form.docx", 4))
    )
    config = {
        "simulator": {"render_bindings": {"form.docx": {"name": "'Alice'"}}},
    }
    worker = SimpleNamespace(root=tmp_path, execution=object())

    cli._render_worker_request(
        _args(config),
        worker,
        {
            "template": "form.docx",
            "output": str(tmp_path / "artifact.docx"),
            "save_snapshot": str(tmp_path / "state.snapshot"),
            "expect_missing": "M.x",
            "bindings": {"extra": "'Bob'", "count": "2"},
            "timeout": 5,
        },
    )
    request = captured[-1]

    assert request.output == (tmp_path / "artifact.docx").resolve()
    assert request.save_snapshot == (tmp_path / "state.snapshot").resolve()
    assert request.expect_missing == "M.x"
    assert request.bindings == (
        ("name", "'Alice'"),
        ("extra", "'Bob'"),
        ("count", "2"),
    )


def test_worker_render_accepts_hyphenated_no_assemble(tmp_path, monkeypatch):
    captured = _capture_render(
        monkeypatch, RenderOutcome(True, RenderResult("form.docx", 4))
    )
    worker = SimpleNamespace(root=tmp_path, execution=object())

    cli._render_worker_request(
        _args(), worker, {"template": "form.docx", "no-assemble": True}
    )

    assert captured[-1].assemble is False


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"operation": "render"}, "template"),
        ({"template": "  "}, "template"),
        ({"template": "form.docx", "source": 7}, "source"),
        ({"template": "form.docx", "source": "bogus"}, "unknown render source"),
        ({"template": "form.docx", "source": "fixture"}, "fixture requires a path"),
        ({"template": "form.docx", "source": "snapshot"}, "snapshot requires a path"),
        ({"template": "form.docx", "output": 7}, "output must be a path"),
        (
            {"template": "form.docx", "save_snapshot": 7},
            "save_snapshot must be a path",
        ),
        ({"template": "form.docx", "expect_missing": 7}, "must be a string"),
        ({"template": "form.docx", "no_assemble": "yes"}, "must be a boolean"),
        ({"template": "form.docx", "bindings": 7}, "bindings must be an object"),
        (
            {"template": "form.docx", "bindings": {"1bad": "1"}},
            "binding name",
        ),
        ({"template": "form.docx", "bindings": {"x": ""}}, "empty expression"),
    ],
)
def test_worker_render_rejects_malformed_requests(tmp_path, payload, message):
    worker = SimpleNamespace(root=tmp_path, execution=object())

    with pytest.raises(WorkerProtocolError, match=message):
        cli._render_worker_request(_args(), worker, payload)


def test_render_request_flows_through_the_worker_protocol(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact.docx"
    _capture_render(
        monkeypatch, RenderOutcome(True, RenderResult("form.docx", 4, artifact))
    )
    worker = _worker(tmp_path, _render_handlers(_args()))
    incoming = io.StringIO(
        '{"id":"one","operation":"render","template":"form.docx"}\n'
        '{"id":"two","operation":"shutdown"}\n'
    )
    outgoing = io.StringIO()

    assert serve(worker, incoming, outgoing) == 0
    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]

    assert responses[0]["ok"] is True
    assert responses[0]["result"] == {
        "template": "form.docx",
        "paragraphs": 4,
        "artifact": str(artifact),
    }
    assert responses[1]["result"]["kind"] == "shutdown"
    assert worker.execution.operations == []


def test_render_result_omits_an_absent_artifact_like_the_cli(tmp_path, monkeypatch):
    _capture_render(monkeypatch, RenderOutcome(True, RenderResult("form.docx", 4)))
    worker = _worker(tmp_path, _render_handlers(_args()))

    response = worker.handle({"operation": "render", "template": "form.docx"})

    assert response["ok"] is True
    assert response["result"] == {"template": "form.docx", "paragraphs": 4}


def test_malformed_render_request_is_a_typed_input_error(tmp_path):
    worker = _worker(tmp_path, _render_handlers(_args()))

    response = worker.handle({"operation": "render"})

    assert response["ok"] is False
    assert response["error"]["kind"] == "input"
    assert "template" in response["error"]["message"]


def test_render_failure_reaches_the_envelope_with_diagnostics(tmp_path, monkeypatch):
    failure = RenderOutcome(
        False,
        error=Failure(ErrorKind.RENDER, "bad template", {"paragraph": 7}),
        diagnostics=(Diagnostic("pdf-skip", "skipped", {}),),
    )
    _capture_render(monkeypatch, failure)
    worker = _worker(tmp_path, _render_handlers(_args()))

    response = worker.handle({"operation": "render", "template": "form.docx"})

    assert response["ok"] is False
    assert response["error"]["kind"] == "render"
    assert response["error"]["details"] == {"paragraph": 7}
    assert response["diagnostics"] == [
        {"kind": "pdf-skip", "message": "skipped", "details": {}}
    ]


def test_render_attachments_forward_through_the_envelope(tmp_path, monkeypatch):
    from docassemble_simulator._outcomes import PublishedAttachment

    artifact = tmp_path / "artifact.docx"
    local_file = tmp_path / "local_document.docx"
    attachment = PublishedAttachment(
        filename="local_document.docx",
        extension="docx",
        mimetype=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        path=local_file,
        uri=local_file.resolve().as_uri(),
    )
    _capture_render(
        monkeypatch,
        RenderOutcome(
            True,
            RenderResult("form.docx", 4, artifact),
            attachments=(attachment,),
        ),
    )
    worker = _worker(tmp_path, _render_handlers(_args()))

    response = worker.handle({"operation": "render", "template": "form.docx"})

    assert response["ok"] is True
    assert response["attachments"] == [
        {
            "filename": "local_document.docx",
            "extension": "docx",
            "mimetype": (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            "path": str(local_file),
            "uri": local_file.resolve().as_uri(),
            "diagnostics": [],
            "structure": None,
        }
    ]


def test_worker_command_registers_the_render_handler(tmp_path, monkeypatch):
    import docassemble_simulator.worker as worker_module

    seen = {}

    class FakeWorker:
        closed = False

        def __init__(self, *args, **kwargs):
            seen.update(kwargs)

    def fake_serve(worker, stream, output, observer=None):
        return 0

    monkeypatch.setattr(worker_module, "Worker", FakeWorker)
    monkeypatch.setattr(worker_module, "serve", fake_serve)
    args = SimpleNamespace(
        session_root=str(tmp_path / "sessions"),
        interview=None,
        timeout=1.0,
        timings=False,
        project_root=None,
        journey=None,
        _simulator_config={},
    )

    assert cli.cmd_worker(args, tmp_path) == 0
    assert {"render"} <= set(seen["handlers"])
    assert callable(seen["handlers"]["render"])
