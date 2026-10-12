from __future__ import annotations

import io
import json
import time

from docassemble_simulator._outcomes import ErrorKind, Failure, Outcome
from docassemble_simulator.execution import (
    Answer,
    Evaluate,
    Execute,
    ExecutionScope,
    SeedState,
    Start,
    Variables,
)
from docassemble_simulator.worker import (
    Worker,
    operation_from_request,
    operation_name,
    serve,
)


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


def test_worker_echoes_caller_supplied_identifier_types(tmp_path):
    worker = Worker(tmp_path, execution=FakeExecution(tmp_path))

    text_response = worker.handle({"id": "caller", "operation": "start"})
    integer_response = worker.handle({"id": 7, "operation": "status"})

    assert text_response["id"] == "caller"
    assert integer_response["id"] == 7


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


def test_operation_names_normalize_to_the_canonical_cli_spelling():
    assert operation_name({"operation": "evaluate"}) == "eval"
    assert operation_name({"op": "execute"}) == "exec"
    assert operation_name({"command": "variables"}) == "vars"
    assert operation_name({"operation": "state_seed"}) == "seed"
    assert operation_name({"operation": "State-Seed"}) == "seed"
    assert operation_name({"operation": "start"}) == "start"
    assert operation_name({"operation": "shutdown"}) == "shutdown"
    assert operation_name({}) == ""
    assert operation_name("evaluate") == ""
    assert operation_name({"operation": 7}) == ""

    spellings = {
        "eval": (Evaluate, {"expression": "1 + 1"}),
        "evaluate": (Evaluate, {"expression": "1 + 1"}),
        "exec": (Execute, {"code": "x = 1"}),
        "execute": (Execute, {"code": "x = 1"}),
        "vars": (Variables, {}),
        "variables": (Variables, {}),
        "seed": (SeedState, {"code": "x = 1"}),
        "state-seed": (SeedState, {"code": "x = 1"}),
    }
    for spelling, (expected, fields) in spellings.items():
        decoded = operation_from_request({"operation": spelling, **fields})
        assert isinstance(decoded, expected), spelling


def test_serve_replaces_a_successful_response_when_the_observer_fails(tmp_path, capsys):
    """Recording is part of the command contract, so an unrecorded success fails."""
    worker = Worker(tmp_path, execution=FakeExecution(tmp_path))

    def observer(request, response):
        if request.get("id") == "one":
            raise RuntimeError("recording is broken")

    incoming = io.StringIO(
        '{"id":"one","operation":"start"}\n'
        '{"id":"two","operation":"status"}\n'
        '{"id":"stop","operation":"shutdown"}\n'
    )
    outgoing = io.StringIO()

    assert serve(worker, incoming, outgoing, observer=observer) == 2
    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert [response["id"] for response in responses] == ["one", "two", "stop"]
    assert responses[0]["ok"] is False
    assert responses[0]["error"]["kind"] == "input"
    assert responses[0]["error"]["message"] == "recording is broken"
    # The original success is replaced, not annotated: no result and no
    # recording diagnostic survives an ok response the observer rejected.
    assert "result" not in responses[0]
    assert "diagnostics" not in responses[0]
    assert responses[1]["ok"] is True
    assert responses[2]["result"]["kind"] == "shutdown"
    assert "recording is broken" in capsys.readouterr().err


def test_serve_isolates_a_failing_observer_and_keeps_answering(tmp_path, capsys):
    worker = Worker(tmp_path, execution=FakeExecution(tmp_path))
    observed = []

    def observer(request, response):
        observed.append(request.get("id"))
        if request.get("id") == "one":
            raise RuntimeError("recording is broken")

    incoming = io.StringIO(
        '{"id":"one","operation":"start"}\n'
        '{"id":"two","operation":"status"}\n'
        '{"id":"stop","operation":"shutdown"}\n'
    )
    outgoing = io.StringIO()

    assert serve(worker, incoming, outgoing, observer=observer) == 2
    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert [response["id"] for response in responses] == ["one", "two", "stop"]
    assert observed == ["one", "two", "stop"]
    assert responses[0]["ok"] is False
    assert responses[0]["error"]["kind"] == "input"
    assert responses[0]["error"]["message"] == "recording is broken"
    assert "result" not in responses[0]
    assert responses[1]["result"]["operation"] == "Status"
    assert responses[2]["result"]["kind"] == "shutdown"
    captured = capsys.readouterr()
    assert "recording is broken" in captured.err
    assert "RuntimeError" in captured.err
    assert captured.out == ""


def test_serve_keeps_an_execution_failure_when_the_observer_also_fails(
    tmp_path, capsys
):
    """A recording failure must not erase the operation's own failure."""
    from docassemble_simulator._diagnostics import Diagnostic

    class StartFailsExecution(FakeExecution):
        def run(self, operation):
            if isinstance(operation, Start):
                return Outcome(
                    False,
                    error=Failure(
                        ErrorKind.UNRESOLVED_VARIABLE,
                        "no question defines x",
                        {"variable": "x"},
                    ),
                    diagnostics=(
                        Diagnostic("variable-seek", "seeking x", {"variable": "x"}),
                    ),
                )
            return super().run(operation)

    worker = Worker(tmp_path, execution=StartFailsExecution(tmp_path), timings=True)

    def observer(request, response):
        if request.get("id") == "failing":
            raise RuntimeError("recording destination is unwritable")

    incoming = io.StringIO(
        '{"id":"failing","operation":"start"}\n'
        '{"id":"later","operation":"status"}\n'
        '{"id":"stop","operation":"shutdown"}\n'
    )
    outgoing = io.StringIO()

    assert serve(worker, incoming, outgoing, observer=observer) == 2
    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert [response["id"] for response in responses] == ["failing", "later", "stop"]
    failed = responses[0]
    assert failed["ok"] is False
    # The operation's own failure survives, with its kind, message, details,
    # diagnostics, and timing intact.
    assert failed["error"]["kind"] == "unresolved-variable"
    assert failed["error"]["message"] == "no question defines x"
    assert failed["error"]["details"] == {"variable": "x"}
    assert "operation_ms" in failed["timing"]
    recording = [
        event for event in failed["diagnostics"] if event["kind"] == "recording-failure"
    ]
    assert len(recording) == 1
    assert "recording destination is unwritable" in recording[0]["message"]
    assert recording[0]["details"] == {"error_kind": "input"}
    # The stream keeps answering and the accumulator still fails the process.
    assert responses[1]["ok"] is True
    assert responses[1]["result"]["operation"] == "Status"
    assert responses[2]["result"]["kind"] == "shutdown"
    captured = capsys.readouterr()
    assert "recording destination is unwritable" in captured.err
    assert captured.out == ""


def test_serve_keeps_response_pairing_for_non_object_requests(tmp_path):
    worker = Worker(tmp_path, execution=FakeExecution(tmp_path))
    observed = []

    def observer(request, response):
        observed.append(request)

    incoming = io.StringIO(
        "null\n"
        "[]\n"
        '"a string request"\n'
        '{"id":"one","operation":"start"}\n'
        '{"id":"stop","operation":"shutdown"}\n'
    )
    outgoing = io.StringIO()

    assert serve(worker, incoming, outgoing, observer=observer) == 2
    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert [response["id"] for response in responses] == [
        None,
        None,
        None,
        "one",
        "stop",
    ]
    assert [response["ok"] for response in responses] == [
        False,
        False,
        False,
        True,
        True,
    ]
    assert all(response["error"]["kind"] == "input" for response in responses[:3])
    assert observed == [
        None,
        [],
        "a string request",
        {"id": "one", "operation": "start"},
        {"id": "stop", "operation": "shutdown"},
    ]


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
