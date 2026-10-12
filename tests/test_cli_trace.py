"""CLI adapter tests for ``--record`` and ``trace compare``."""

import json
from types import SimpleNamespace

from docassemble_simulator import cli
from docassemble_simulator._outcomes import ErrorKind, Failure
from docassemble_simulator.trace import (
    ScreenIdentity,
    ScreenTrace,
    TraceEntry,
    TraceMetadata,
    load_trace,
    write_trace,
)

INTERVIEW = "docassemble.probe:data/questions/main.yml"


def _metadata():
    return TraceMetadata(
        interview=INTERVIEW,
        config_fingerprint="fingerprint-1",
        docassemble="1.10.10",
        assemblyline="4.8.0",
        simulator="26.9.0",
    )


def _screen(question_name="ID intro", **overrides):
    screen = {
        "kind": "question",
        "question_type": "fields",
        "question_name": question_name,
        "question_text": "Intro",
        "subquestion_text": "",
        "sought": None,
        "orig_sought": None,
        "fields": [
            {"variable": "user_name", "type": "text", "visible": True, "required": True}
        ],
    }
    screen.update(overrides)
    return screen


def _write_trace(path, *keys):
    entries = tuple(
        TraceEntry(
            seq=index + 1,
            operation="answer",
            phase=None,
            submitted={},
            ok=True,
            screen=_screen(),
            identity=ScreenIdentity(key, "explicit-id"),
            content=(),
        )
        for index, key in enumerate(keys)
    )
    write_trace(path, ScreenTrace(metadata=_metadata(), entries=entries))


def _parse(*arguments):
    return cli.build_parser().parse_args(list(arguments))


def test_record_flags_are_accepted_on_execution_commands_only():
    parser = cli.build_parser()
    for command in (
        ["start"],
        ["refresh"],
        ["answer", "x=1"],
        ["seek", "x"],
        ["exec", "x = 1"],
    ):
        parsed = parser.parse_args(
            [*command, "--record", "t.jsonl", "--phase", "intake"]
        )
        assert parsed.record == "t.jsonl"
        assert parsed.phase == "intake"
    for command in (["status"], ["eval", "x"], ["vars"]):
        try:
            parser.parse_args([*command, "--record", "t.jsonl"])
        except (SystemExit, cli.UsageFailure):
            pass
        else:
            raise AssertionError(f"{command[0]} accepted --record")


class _FakeExecution:
    def __init__(self, *outcomes):
        self._outcomes = list(outcomes)
        self.operations = []

    def run(self, operation):
        self.operations.append(operation)
        return self._outcomes.pop(0)


def _outcome(ok, result=None, error=None):
    return SimpleNamespace(
        ok=ok, result=result, error=error, diagnostics=(), attachments=()
    )


def test_execution_records_a_screen_sidecar(tmp_path, monkeypatch):
    execution = _FakeExecution(_outcome(True, _screen()))
    monkeypatch.setattr(cli, "_execution", lambda args, root: execution)
    monkeypatch.setattr(cli, "_trace_metadata", lambda args, root: _metadata())
    record = tmp_path / "run.trace.jsonl"
    args = _parse("start", "--record", str(record), "--phase", "intake", "--json")

    assert cli.cmd_execution(args, tmp_path) == 0

    trace = load_trace(record)
    assert trace.metadata.interview == INTERVIEW
    entry = trace.entries[0]
    assert entry.identity.key == "id:intro"
    assert entry.phase == "intake"
    assert entry.ok is True


def test_cli_records_each_auto_submitted_screen_before_the_final_outcome(
    tmp_path, monkeypatch
):
    steps = [_screen("ID first"), _screen("ID second")]
    final = _screen("ID final")
    execution = _FakeExecution(
        _outcome(
            True,
            {
                **final,
                "auto_trace": [
                    {"screen": steps[0], "submitted": {"first": "one"}},
                    {"screen": steps[1], "submitted": {"second": "two"}},
                ],
            },
        )
    )
    monkeypatch.setattr(cli, "_execution", lambda args, root: execution)
    monkeypatch.setattr(cli, "_trace_metadata", lambda args, root: _metadata())
    record = tmp_path / "run.trace.jsonl"
    args = _parse(
        "seek",
        "target",
        "--auto",
        "--record",
        str(record),
        "--json",
    )

    assert cli.cmd_execution(args, tmp_path) == 0

    trace = load_trace(record)
    assert [entry.identity.key for entry in trace.entries] == [
        "id:first",
        "id:second",
        "id:final",
    ]
    assert trace.entries[0].submitted == {"first": "one"}
    assert trace.entries[1].submitted == {"second": "two"}
    assert [entry.intent for entry in trace.entries] == ["auto", "auto", "seek"]
    assert trace.entries[-1].target == "target"


def test_default_answer_records_browser_submission_intent(tmp_path, monkeypatch):
    execution = _FakeExecution(_outcome(True, _screen()))
    monkeypatch.setattr(cli, "_execution", lambda args, root: execution)
    monkeypatch.setattr(cli, "_trace_metadata", lambda args, root: _metadata())
    record = tmp_path / "defaults.trace.jsonl"
    args = _parse("answer", "--defaults", "--record", str(record), "--json")

    assert cli.cmd_execution(args, tmp_path) == 0

    entry = load_trace(record).entries[0]
    assert entry.intent == "defaults"
    assert entry.submitted == {}


def test_failed_answer_records_the_unchanged_active_screen(tmp_path, monkeypatch):
    failure = _outcome(
        False, error=Failure(ErrorKind.VALIDATION, "screen rejected", {})
    )
    execution = _FakeExecution(failure, _outcome(True, _screen("ID name")))
    monkeypatch.setattr(cli, "_execution", lambda args, root: execution)
    monkeypatch.setattr(cli, "_trace_metadata", lambda args, root: _metadata())
    record = tmp_path / "run.trace.jsonl"
    args = _parse("answer", "user_name=bad", "--record", str(record), "--json")

    assert cli.cmd_execution(args, tmp_path) == 2

    entry = load_trace(record).entries[0]
    assert entry.ok is False
    assert entry.identity.key == "id:name"
    assert entry.error is not None and entry.error["kind"] == "validation"
    assert entry.submitted == {"user_name": "bad"}


def test_failed_start_records_the_error_identity(tmp_path, monkeypatch):
    failure = _outcome(
        False,
        error=Failure(
            ErrorKind.UNRESOLVED_VARIABLE,
            "name is undefined",
            {
                "failure_kind": "unresolved-variable",
                "sought_variable": "clients[0].name",
            },
        ),
    )
    stored = _outcome(
        True,
        {
            "kind": "error",
            "failure_kind": "unresolved-variable",
            "sought_variable": "clients[0].name",
            "message": "name is undefined",
        },
    )
    execution = _FakeExecution(failure, stored)
    monkeypatch.setattr(cli, "_execution", lambda args, root: execution)
    monkeypatch.setattr(cli, "_trace_metadata", lambda args, root: _metadata())
    record = tmp_path / "run.trace.jsonl"
    args = _parse("start", "--record", str(record), "--json")

    assert cli.cmd_execution(args, tmp_path) == 2

    entry = load_trace(record).entries[0]
    assert entry.ok is False
    assert entry.identity == ScreenIdentity("error:unresolved:clients[i].name", "error")
    assert entry.screen is None
    assert entry.error is not None
    assert entry.error["kind"] == "unresolved-variable"
    assert entry.error["details"]["sought_variable"] == "clients[0].name"


def test_recorded_phases_compare_without_repeating_the_declaration(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "_reexec_with_dyld_path", lambda: None)
    golden = tmp_path / "golden.jsonl"
    run = tmp_path / "run.jsonl"

    def record_two_phases(path):
        execution = _FakeExecution(
            _outcome(True, _screen()), _outcome(True, _screen("ID done"))
        )
        monkeypatch.setattr(cli, "_execution", lambda args, root: execution)
        monkeypatch.setattr(cli, "_trace_metadata", lambda args, root: _metadata())
        for phase in ("intake", "download"):
            args = _parse("start", "--record", str(path), "--phase", phase, "--json")
            assert cli.cmd_execution(args, tmp_path) == 0

    record_two_phases(golden)
    record_two_phases(run)
    capsys.readouterr()

    code = cli.main(
        [
            "--json",
            "trace",
            "compare",
            str(golden),
            str(run),
            "--order",
            "phased",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["result"]["matched"] is True
    assert [phase["phase"] for phase in payload["result"]["phases"]] == [
        "intake",
        "download",
    ]


def test_trace_compare_json_reports_a_match(tmp_path, capsys, monkeypatch):
    # Comparison reads only sidecars: no docassemble package or runtime needed.
    monkeypatch.setattr(cli, "_reexec_with_dyld_path", lambda: None)
    expected = tmp_path / "expected.jsonl"
    actual = tmp_path / "actual.jsonl"
    _write_trace(expected, "id:intro", "id:done")
    _write_trace(actual, "id:intro", "id:done")

    code = cli.main(
        [
            "--json",
            "trace",
            "compare",
            str(expected),
            str(actual),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["result"]["matched"] is True
    assert payload["result"]["counts"] == {
        "expected": 2,
        "actual": 2,
        "matched": 2,
        "missing": 0,
        "extra": 0,
        "duplicates": 0,
    }


def test_trace_compare_reports_a_mismatch_with_exit_code_two(tmp_path, capsys):
    expected = tmp_path / "expected.jsonl"
    actual = tmp_path / "actual.jsonl"
    _write_trace(expected, "id:intro", "id:done")
    _write_trace(actual, "id:intro")
    args = _parse(
        "trace",
        "compare",
        str(expected),
        str(actual),
        "--order",
        "unordered",
        "--json",
    )

    assert cli.cmd_trace(args, tmp_path) == 2

    captured = json.loads(capsys.readouterr().out)
    assert captured["ok"] is False
    assert captured["error"]["kind"] == "trace-mismatch"
    assert captured["error"]["details"]["counts"]["missing"] == 1
    assert [diff["kind"] for diff in captured["error"]["details"]["diffs"]] == [
        "missing"
    ]


def test_trace_compare_update_rewrites_the_golden(tmp_path, capsys):
    expected = tmp_path / "expected.jsonl"
    actual = tmp_path / "actual.jsonl"
    _write_trace(expected, "id:intro")
    _write_trace(actual, "id:intro", "id:done")
    args = _parse("trace", "compare", str(expected), str(actual), "--update", "--json")

    assert cli.cmd_trace(args, tmp_path) == 0

    captured = json.loads(capsys.readouterr().out)
    assert captured["result"]["updated"] == str(expected)
    assert load_trace(expected) == load_trace(actual)


def test_trace_compare_rejects_unreadable_traces(tmp_path, capsys):
    args = _parse(
        "trace", "compare", str(tmp_path / "missing.jsonl"), "actual.jsonl", "--json"
    )

    assert cli.cmd_trace(args, tmp_path) == 1

    captured = json.loads(capsys.readouterr().out)
    assert captured["error"]["kind"] == "input"
    assert "could not read" in captured["error"]["message"]


def test_recording_refuses_a_trace_from_another_run(tmp_path, monkeypatch, capsys):
    execution = _FakeExecution(_outcome(True, _screen()), _outcome(True, _screen()))
    monkeypatch.setattr(cli, "_execution", lambda args, root: execution)
    metadata = [_metadata()]
    monkeypatch.setattr(cli, "_trace_metadata", lambda args, root: metadata[0])
    record = tmp_path / "run.trace.jsonl"
    args = _parse("start", "--record", str(record), "--json")
    assert cli.cmd_execution(args, tmp_path) == 0
    capsys.readouterr()  # discard the first invocation's envelope

    metadata[0] = _metadata().from_dict(
        {**_metadata().as_dict(), "config_fingerprint": "fingerprint-2"}
    )
    code = cli.cmd_execution(args, tmp_path)

    captured = json.loads(capsys.readouterr().out)
    assert code == 1
    assert captured["error"]["kind"] == "input"
    assert "fingerprint" in captured["error"]["message"]


def test_trace_compare_human_mismatch_points_at_update(tmp_path, capsys):
    expected = tmp_path / "expected.jsonl"
    actual = tmp_path / "actual.jsonl"
    _write_trace(expected, "id:intro", "id:done")
    _write_trace(actual, "id:intro")
    args = _parse(
        "trace", "compare", str(expected), str(actual), "--order", "unordered"
    )

    assert cli.cmd_trace(args, tmp_path) == 2

    output = capsys.readouterr().out
    assert "trace compare: MISMATCH (unordered)" in output
    assert "missing: id:done" in output
    assert "pass --update to rewrite the golden after review" in output


class _ScopedFakeExecution(_FakeExecution):
    """A fake worker execution whose scope is serializable."""

    def __init__(self, root, *outcomes):
        super().__init__(*outcomes)
        from docassemble_simulator.execution import ExecutionScope

        self.scope = ExecutionScope(
            root,
            INTERVIEW,
            "",
            root / "sessions",
            root / "sessions" / "main.pkl",
        )

    def reset(self):
        pass


def _serve_worker(worker, observer, incoming):
    import io

    from docassemble_simulator.worker import serve

    outgoing = io.StringIO()
    code = serve(worker, io.StringIO(incoming), outgoing, observer=observer)
    return code, [json.loads(line) for line in outgoing.getvalue().splitlines()]


def test_worker_journey_records_execution_responses_and_skips_protocol(
    tmp_path, monkeypatch
):
    from docassemble_simulator.worker import Worker

    monkeypatch.setattr(cli, "_trace_metadata", lambda args, root: _metadata())
    record = tmp_path / "journey.trace.jsonl"
    args = _parse("start", "--record", str(record), "--phase", "intake", "--json")
    execution = _ScopedFakeExecution(
        tmp_path,
        _outcome(True, _screen("ID first")),
        _outcome(True, _screen("ID second")),
    )
    worker = Worker(tmp_path, execution=execution)
    observer = cli._worker_journey_recorder(args, tmp_path, worker)

    code, responses = _serve_worker(
        worker,
        observer,
        "not-json\n"
        '{"id":"ping","operation":"ping"}\n'
        '{"id":"start","operation":"start"}\n'
        '{"id":"answer","operation":"answer","assignments":{"x":"1"},'
        '"phase":"second"}\n',
    )

    assert code == 2  # the malformed frame still fails the stream
    assert [response["id"] for response in responses] == [
        None,
        "ping",
        "start",
        "answer",
    ]
    trace = load_trace(record)
    assert [entry.operation for entry in trace.entries] == ["start", "answer"]
    assert [entry.phase for entry in trace.entries] == ["intake", "second"]


def test_worker_journey_recorder_keeps_pairing_for_non_object_requests(
    tmp_path, monkeypatch
):
    """A JSON null, array, or string must not abort the recorder."""
    from docassemble_simulator.worker import Worker

    monkeypatch.setattr(cli, "_trace_metadata", lambda args, root: _metadata())
    record = tmp_path / "journey.trace.jsonl"
    args = _parse("start", "--record", str(record), "--phase", "intake", "--json")
    execution = _ScopedFakeExecution(tmp_path, _outcome(True, _screen("ID first")))
    worker = Worker(tmp_path, execution=execution)
    observer = cli._worker_journey_recorder(args, tmp_path, worker)

    code, responses = _serve_worker(
        worker,
        observer,
        "null\n"
        "[]\n"
        '"a string request"\n'
        '{"id":"start","operation":"start"}\n'
        '{"id":"stop","operation":"shutdown"}\n',
    )

    assert code == 2
    assert [response["id"] for response in responses] == [
        None,
        None,
        None,
        "start",
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
    trace = load_trace(record)
    assert [entry.operation for entry in trace.entries] == ["start"]


def test_worker_journey_recorder_rejects_a_non_string_record_or_phase(
    tmp_path, monkeypatch
):
    from docassemble_simulator.worker import Worker

    monkeypatch.setattr(cli, "_trace_metadata", lambda args, root: _metadata())
    record = tmp_path / "journey.trace.jsonl"
    args = _parse("start", "--record", str(record), "--json")
    execution = _ScopedFakeExecution(
        tmp_path,
        _outcome(True, _screen("ID first")),
        _outcome(True, _screen("ID second")),
        _outcome(True, _screen("ID third")),
        _outcome(True, _screen("ID fourth")),
    )
    worker = Worker(tmp_path, execution=execution)
    observer = cli._worker_journey_recorder(args, tmp_path, worker)

    code, responses = _serve_worker(
        worker,
        observer,
        '{"id":"record","operation":"start","record":123}\n'
        '{"id":"empty","operation":"start","record":""}\n'
        '{"id":"phase","operation":"start","phase":7}\n'
        '{"id":"start","operation":"start"}\n'
        '{"id":"stop","operation":"shutdown"}\n',
    )

    assert code == 2
    assert [response["id"] for response in responses] == [
        "record",
        "empty",
        "phase",
        "start",
        "stop",
    ]
    for response in responses[:3]:
        assert response["ok"] is False
        assert response["error"]["kind"] == "input"
    assert "record" in responses[0]["error"]["message"]
    assert "record" in responses[1]["error"]["message"]
    assert "phase" in responses[2]["error"]["message"]
    assert responses[3]["ok"] is True
    assert responses[4]["result"]["kind"] == "shutdown"
    trace = load_trace(record)
    assert [entry.operation for entry in trace.entries] == ["start"]


def test_worker_journey_surfaces_a_recording_failure_as_a_typed_response(
    tmp_path, monkeypatch
):
    """A truncated journey trace must not report success (issue #45).

    The failing request is recorded against a destination that cannot be
    created; the worker's own ``--record`` default stays usable, so the next
    execution request still answers and records.
    """
    from docassemble_simulator.worker import Worker

    monkeypatch.setattr(cli, "_trace_metadata", lambda args, root: _metadata())
    record = tmp_path / "journey.trace.jsonl"
    missing = tmp_path / "missing" / "journey.trace.jsonl"
    args = _parse("start", "--record", str(record), "--phase", "intake", "--json")
    execution = _ScopedFakeExecution(
        tmp_path,
        _outcome(True, _screen("ID first")),
        _outcome(True, _screen("ID second")),
    )
    worker = Worker(tmp_path, execution=execution)
    observer = cli._worker_journey_recorder(args, tmp_path, worker)

    code, responses = _serve_worker(
        worker,
        observer,
        json.dumps({"id": "start", "operation": "start", "record": str(missing)}) + "\n"
        '{"id":"status","operation":"status"}\n'
        '{"id":"stop","operation":"shutdown"}\n',
    )

    assert code == 2
    assert responses[0]["ok"] is False
    assert responses[0]["error"]["kind"] == "input"
    assert str(missing) in responses[0]["error"]["message"]
    assert not missing.exists()
    assert responses[1]["ok"] is True
    assert responses[2]["result"]["kind"] == "shutdown"
    # The failed operation is not recorded as a success anywhere.
    assert [entry.operation for entry in load_trace(record).entries] == ["status"]


def test_worker_journey_phase_null_falls_back_to_the_worker_default(
    tmp_path, monkeypatch
):
    from docassemble_simulator.worker import Worker

    monkeypatch.setattr(cli, "_trace_metadata", lambda args, root: _metadata())
    record = tmp_path / "journey.trace.jsonl"
    args = _parse("start", "--record", str(record), "--phase", "intake", "--json")
    execution = _ScopedFakeExecution(
        tmp_path,
        _outcome(True, _screen("ID first")),
        _outcome(True, _screen("ID second")),
        _outcome(True, _screen("ID third")),
    )
    worker = Worker(tmp_path, execution=execution)
    observer = cli._worker_journey_recorder(args, tmp_path, worker)

    code, responses = _serve_worker(
        worker,
        observer,
        '{"id":"absent","operation":"start"}\n'
        '{"id":"null","operation":"start","phase":null}\n'
        '{"id":"explicit","operation":"start","phase":"close"}\n'
        '{"id":"stop","operation":"shutdown"}\n',
    )

    assert code == 0, responses
    assert all(response["ok"] for response in responses)
    trace = load_trace(record)
    assert [entry.phase for entry in trace.entries] == ["intake", "intake", "close"]


def test_worker_journey_records_an_alias_operation_under_the_canonical_name(
    tmp_path, monkeypatch
):
    """``evaluate`` must record what the one-shot ``eval`` spelling records."""
    from docassemble_simulator.execution import Evaluate
    from docassemble_simulator.worker import Worker, operation_name

    assert operation_name({"operation": "evaluate"}) == "eval"
    assert _parse("eval", "x").command == "eval"
    monkeypatch.setattr(cli, "_trace_metadata", lambda args, root: _metadata())

    alias_record = tmp_path / "alias.trace.jsonl"
    canonical_record = tmp_path / "canonical.trace.jsonl"
    args = _parse("start", "--record", str(canonical_record), "--json")

    for path, spelling in ((alias_record, "evaluate"), (canonical_record, "eval")):
        execution = _ScopedFakeExecution(
            tmp_path,
            _outcome(False, error=Failure(ErrorKind.EXECUTION, "no such variable")),
            _outcome(True, _screen("ID main")),
        )
        worker = Worker(tmp_path, execution=execution)
        observer = cli._worker_journey_recorder(args, tmp_path, worker)
        code, responses = _serve_worker(
            worker,
            observer,
            json.dumps(
                {
                    "id": "one",
                    "operation": spelling,
                    "expression": "missing_value",
                    "record": str(path),
                }
            )
            + "\n"
            '{"id":"stop","operation":"shutdown"}\n',
        )
        assert code == 2
        assert responses[0]["ok"] is False

    alias_entry = load_trace(alias_record).entries[0]
    canonical_entry = load_trace(canonical_record).entries[0]
    assert alias_entry.operation == "eval"
    assert alias_entry.intent == "eval"
    assert (
        alias_entry.intent
        == cli._trace_operation_context(Evaluate("x"), "eval")["intent"]
    )
    assert alias_entry.as_dict() == canonical_entry.as_dict()
