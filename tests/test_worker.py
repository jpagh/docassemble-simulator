from __future__ import annotations

import io
import json
import time

from docassemble_simulator._outcomes import ErrorKind, Failure, Outcome
from docassemble_simulator.execution import Answer, ExecutionScope, Start
from docassemble_simulator.worker import Worker, operation_from_request, serve


class FakeExecution:
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
        self.reset_count = 0

    def run(self, operation):
        self.operations.append(operation)
        if isinstance(operation, Answer) and operation.assignments == (("bad", "x"),):
            return Outcome(False, error=Failure(ErrorKind.INPUT, "bad answer"))
        return Outcome(True, {"operation": type(operation).__name__})

    def reset(self):
        self.reset_count += 1


def test_worker_protocol_keeps_scope_and_returns_typed_errors(tmp_path):
    execution = FakeExecution(tmp_path)
    worker = Worker(tmp_path, execution=execution, timeout=1, timings=True)

    started = worker.handle({"id": "first", "operation": "start"})
    failed = worker.handle(
        {"id": "second", "operation": "answer", "assignments": {"bad": "x"}}
    )

    assert started["id"] == "first"
    assert started["ok"] is True
    assert started["result"] == {"operation": "Start"}
    assert started["scope"]["interview"] == execution._identity
    assert "operation_ms" in started["timing"]
    assert failed["ok"] is False
    assert failed["error"]["kind"] == "input"
    assert failed["scope"] == started["scope"]


def test_worker_accepts_structured_assignments_and_reset(tmp_path):
    execution = FakeExecution(tmp_path)
    worker = Worker(tmp_path, execution=execution)

    operation = operation_from_request(
        {"operation": "answer", "assignments": {"count": 2, "enabled": True}}
    )
    assert operation.assignments == (("count", "2"), ("enabled", "true"))
    assert isinstance(operation_from_request({"operation": "start"}), Start)
    response = worker.handle({"id": 4, "operation": "reset"})

    assert response["ok"] is True
    assert response["result"]["kind"] == "reset"
    assert execution.reset_count == 1


def test_serve_continues_after_malformed_and_operation_requests(tmp_path):
    worker = Worker(tmp_path, execution=FakeExecution(tmp_path))
    incoming = io.StringIO(
        "not-json\n"
        '{"id":"ok","operation":"start"}\n'
        '{"id":"stop","operation":"shutdown"}\n'
    )
    outgoing = io.StringIO()

    assert serve(worker, incoming, outgoing) == 2
    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert responses[0]["error"]["kind"] == "input"
    assert responses[1]["result"]["operation"] == "Start"
    assert responses[2]["result"]["kind"] == "shutdown"


def test_serve_keeps_authored_stdout_out_of_protocol(tmp_path, capsys):
    class PrintingExecution(FakeExecution):
        def run(self, operation):
            print("authored output")
            return super().run(operation)

    worker = Worker(tmp_path, execution=PrintingExecution(tmp_path))
    incoming = io.StringIO(
        '{"id":"one","operation":"start"}\n'
        '{"id":"two","operation":"status"}\n'
        '{"id":"stop","operation":"shutdown"}\n'
    )
    outgoing = io.StringIO()

    assert serve(worker, incoming, outgoing) == 0
    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert [response["id"] for response in responses] == ["one", "two", "stop"]
    assert "authored output" not in capsys.readouterr().out


def test_worker_timeout_is_typed_and_stream_remains_usable(tmp_path):
    class SlowExecution(FakeExecution):
        def run(self, operation):
            time.sleep(0.05)
            return super().run(operation)

    worker = Worker(tmp_path, execution=SlowExecution(tmp_path), timeout=0.01)
    response = worker.handle({"id": "slow", "operation": "start"})

    assert response["ok"] is False
    assert response["error"]["kind"] == "timeout"


def test_execution_scope_is_immutable_and_configuration_specific(tmp_path):
    from dataclasses import FrozenInstanceError

    import pytest

    from docassemble_simulator.execution import InterviewExecution

    questions = tmp_path / "docassemble" / "example" / "data" / "questions"
    questions.mkdir(parents=True)
    (questions.parents[1] / "__init__.py").write_text("")
    (questions / "main.yml").write_text("---\nquestion: Hello\n")
    execution = InterviewExecution(tmp_path, config_fingerprint="config-a")
    scope = execution.scope
    assert scope.interview == "docassemble.example:data/questions/main.yml"
    assert scope.root == tmp_path
    assert scope.config_fingerprint == "config-a"
    assert scope.session_root == tmp_path / ".simulator" / "sessions"
    assert scope.session_id == scope.session.stem
    with pytest.raises(FrozenInstanceError):
        scope.interview = "changed"
    other = InterviewExecution(tmp_path, config_fingerprint="config-b")
    assert other.scope.session != scope.session
    assert Worker(tmp_path, execution=execution).scope["interview"] == scope.interview
