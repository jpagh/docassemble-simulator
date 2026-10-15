"""The command adapter's ``trace-compare``/``trace-replay`` worker requests.

``worker.py`` stays an operation-only server: trace compare and replay are
mapped onto the same ``_trace_compare_report``/``_trace_replay_report`` seams as
``sim trace compare``/``sim trace replay`` and served through the worker's
generic handler seam. These tests pin the field mapping, the report and typed
error passthrough, and the protocol envelope without a real runtime.
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from worker_adapter_fakes import AdapterFakeExecution

from docassemble_simulator import cli
from docassemble_simulator._outcomes import ErrorKind
from docassemble_simulator.trace import (
    ComparePolicy,
    ScreenIdentity,
    ScreenTrace,
    TraceComparison,
    TraceEntry,
    TraceMetadata,
    write_trace,
)
from docassemble_simulator.worker import Worker, WorkerProtocolError, serve


def _worker(tmp_path, handlers):
    return Worker(
        tmp_path, execution=AdapterFakeExecution(tmp_path), timeout=1, handlers=handlers
    )


def _comparison(matched=True):
    return TraceComparison(
        matched=matched,
        order="ordered",
        expected_count=2,
        actual_count=2,
        matched_count=2,
        missing_count=0,
        extra_count=0,
        duplicate_count=0,
        phases=(),
        diffs=(),
        policy=ComparePolicy(),
    )


def _replay_report(clean):
    return {
        "golden": "golden.jsonl",
        "metadata": {},
        "clean": clean,
        "steps": [],
        "first_divergence": None,
        "counts": {"expected": 0, "replayed": 0, "matched": 0},
    }


# -- registration ----------------------------------------------------------


def test_trace_worker_handlers_register_both_requests():
    handlers = cli._trace_worker_handlers(SimpleNamespace())

    assert set(handlers) == {"trace-compare", "trace-replay"}
    assert all(callable(handler) for handler in handlers.values())


def test_worker_command_registers_the_trace_handlers():
    handlers = cli._worker_request_handlers(SimpleNamespace())

    assert {"trace-compare", "trace-replay"} <= set(handlers)


# -- trace compare ---------------------------------------------------------


def test_trace_compare_worker_returns_the_cli_report(monkeypatch, tmp_path):
    seen = {}

    def fake(expected, actual, policy, exceptions):
        seen.update(
            expected=expected, actual=actual, policy=policy, exceptions=exceptions
        )
        return _comparison(True)

    monkeypatch.setattr(cli, "_trace_compare_report", fake)

    outcome = cli._trace_compare_worker_request({"expected": "golden", "actual": "run"})

    assert outcome.ok is True
    assert outcome.result["matched"] is True
    assert seen["expected"] == "golden"
    assert seen["actual"] == "run"
    assert seen["exceptions"] is None


def test_trace_compare_worker_maps_the_policy_and_exceptions(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(
        cli,
        "_trace_compare_report",
        lambda expected, actual, policy, exceptions: (
            seen.update(policy=policy, exceptions=exceptions) or _comparison(True)
        ),
    )

    cli._trace_compare_worker_request(
        {
            "expected": "golden",
            "actual": "run",
            "order": "phased",
            "missing": "allow",
            "extra": "allow",
            "full_text": True,
            "phases": ["intake", "close"],
            "exceptions": "exceptions.toml",
        },
    )

    policy = seen["policy"]
    assert (
        policy.order,
        policy.missing,
        policy.extra,
        policy.full_text,
        policy.phases,
    ) == ("phased", "allow", "allow", True, ("intake", "close"))
    assert seen["exceptions"] == "exceptions.toml"


def test_trace_compare_worker_accepts_a_hyphenated_full_text_flag(
    monkeypatch, tmp_path
):
    seen = {}
    monkeypatch.setattr(
        cli,
        "_trace_compare_report",
        lambda expected, actual, policy, exceptions: (
            seen.update(policy=policy) or _comparison(True)
        ),
    )

    cli._trace_compare_worker_request(
        {"expected": "golden", "actual": "run", "full-text": True}
    )

    assert seen["policy"].full_text is True


def test_trace_compare_worker_reports_a_mismatch_as_trace_mismatch(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        cli, "_trace_compare_report", lambda *a, **k: _comparison(False)
    )

    outcome = cli._trace_compare_worker_request({"expected": "golden", "actual": "run"})

    assert outcome.ok is False
    assert outcome.error.kind == ErrorKind.TRACE_MISMATCH
    assert outcome.error.message == "screen trace comparison failed"
    assert outcome.error.details["matched"] is False


def test_trace_compare_worker_maps_a_trace_error_to_input(monkeypatch, tmp_path):
    from docassemble_simulator.trace import TraceError

    def fail(*args, **kwargs):
        raise TraceError("could not read trace golden")

    monkeypatch.setattr(cli, "_trace_compare_report", fail)

    outcome = cli._trace_compare_worker_request({"expected": "golden", "actual": "run"})

    assert outcome.ok is False
    assert outcome.error.kind == ErrorKind.INPUT
    assert outcome.error.message == "could not read trace golden"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"actual": "run"}, "expected"),
        ({"expected": "  ", "actual": "run"}, "expected"),
        ({"expected": "golden"}, "actual"),
        ({"expected": "golden", "actual": 7}, "actual"),
        ({"expected": "golden", "actual": "run", "exceptions": 7}, "exceptions"),
        ({"expected": "golden", "actual": "run", "order": 7}, "order"),
        ({"expected": "golden", "actual": "run", "phases": 7}, "phases"),
        ({"expected": "golden", "actual": "run", "full_text": "yes"}, "full_text"),
    ],
)
def test_trace_compare_worker_rejects_malformed_requests(tmp_path, payload, message):

    with pytest.raises(WorkerProtocolError, match=message):
        cli._trace_compare_worker_request(payload)


# -- trace replay ----------------------------------------------------------

INTERVIEW = "docassemble.example:data/questions/main.yml"


def _replay_worker(tmp_path, execution=None):
    """A worker namespace for the replay handler, with a shared catalog."""
    return SimpleNamespace(
        root=tmp_path,
        identity=INTERVIEW,
        execution=execution or SimpleNamespace(catalog=None),
    )


def test_trace_replay_worker_returns_a_clean_report(monkeypatch, tmp_path):
    seen = {}
    report = _replay_report(True)
    monkeypatch.setattr(
        cli,
        "_trace_replay_report",
        lambda root, golden, **kwargs: (
            seen.update(root=root, golden=golden, **kwargs) or report
        ),
    )
    worker = _replay_worker(tmp_path)

    outcome = cli._trace_replay_worker_request(
        SimpleNamespace(),
        worker,
        {"golden": "golden.jsonl", "overrides": ["2.user_name=Bob"]},
    )

    assert outcome.ok is True
    assert outcome.result["clean"] is True
    assert seen["root"] == tmp_path
    assert seen["golden"] == "golden.jsonl"
    assert seen["overrides"] == ("2.user_name=Bob",)
    assert seen["interview"] == INTERVIEW
    assert seen["resolved_configuration"] is None


def test_trace_replay_worker_reports_a_divergence_as_trace_mismatch(
    monkeypatch, tmp_path
):
    report = _replay_report(False)
    monkeypatch.setattr(
        cli, "_trace_replay_report", lambda root, golden, **kwargs: report
    )
    worker = _replay_worker(tmp_path)

    outcome = cli._trace_replay_worker_request(
        SimpleNamespace(), worker, {"golden": "golden.jsonl"}
    )

    assert outcome.ok is False
    assert outcome.error.kind == ErrorKind.TRACE_MISMATCH
    assert outcome.error.message == "screen trace replay diverged"
    assert outcome.error.details["clean"] is False


def test_trace_replay_worker_maps_a_metadata_mismatch_to_input(monkeypatch, tmp_path):
    from docassemble_simulator.trace import TraceError

    def fail(root, golden, **kwargs):
        raise TraceError("trace config fingerprint mismatch: 'a' does not match 'b'")

    monkeypatch.setattr(cli, "_trace_replay_report", fail)
    worker = _replay_worker(tmp_path)

    outcome = cli._trace_replay_worker_request(
        SimpleNamespace(), worker, {"golden": "golden.jsonl"}
    )

    assert outcome.ok is False
    assert outcome.error.kind == ErrorKind.INPUT
    assert "config fingerprint" in outcome.error.message


def test_trace_replay_worker_honours_an_explicit_interview(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(
        cli,
        "_trace_replay_report",
        lambda root, golden, **kwargs: seen.update(kwargs) or _replay_report(True),
    )
    worker = _replay_worker(tmp_path)

    cli._trace_replay_worker_request(
        SimpleNamespace(),
        worker,
        {
            "golden": "golden.jsonl",
            "interview": "docassemble.other:data/questions/main.yml",
        },
    )

    assert seen["interview"] == "docassemble.other:data/questions/main.yml"


def test_trace_replay_worker_accepts_a_single_override_string(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(
        cli,
        "_trace_replay_report",
        lambda root, golden, **kwargs: seen.update(kwargs) or _replay_report(True),
    )
    worker = _replay_worker(tmp_path)

    cli._trace_replay_worker_request(
        SimpleNamespace(),
        worker,
        {"golden": "golden.jsonl", "override": "2.user_name=Bob"},
    )

    assert seen["overrides"] == ("2.user_name=Bob",)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"overrides": ["2.user_name=Bob"]}, "golden"),
        ({"golden": "  "}, "golden"),
        ({"golden": "g", "interview": 7}, "interview"),
        ({"golden": "g", "overrides": 7}, "overrides"),
        ({"golden": "g", "overrides": [7]}, "override"),
    ],
)
def test_trace_replay_worker_rejects_malformed_requests(tmp_path, payload, message):
    worker = _replay_worker(tmp_path)

    with pytest.raises(WorkerProtocolError, match=message):
        cli._trace_replay_worker_request(SimpleNamespace(), worker, payload)


# -- protocol --------------------------------------------------------------


def test_trace_replay_worker_hands_the_shared_catalog_to_the_replay(
    monkeypatch, tmp_path
):
    """The replay compiles through the worker's one catalog, not a fresh one.

    The worker's cached catalog owns the current-source watches and its
    ``_compile`` revalidates every identity it is asked for, so the replay
    observes an edit without the adapter refreshing first.
    """
    seen = {}
    monkeypatch.setattr(
        cli,
        "_trace_replay_report",
        lambda root, golden, **kwargs: seen.update(kwargs) or _replay_report(True),
    )
    catalog = object()
    execution = SimpleNamespace(catalog=catalog)
    worker = SimpleNamespace(root=tmp_path, identity=INTERVIEW, execution=execution)

    outcome = cli._trace_replay_worker_request(
        SimpleNamespace(), worker, {"golden": "golden.jsonl"}
    )

    assert outcome.ok is True
    assert seen["catalog"] is catalog
    assert seen["interview"] == INTERVIEW


def test_trace_replay_worker_names_the_recorded_interview(monkeypatch, tmp_path):
    """A replay for another interview still forwards the requested identity."""
    seen = {}
    monkeypatch.setattr(
        cli,
        "_trace_replay_report",
        lambda root, golden, **kwargs: seen.update(kwargs) or _replay_report(True),
    )
    worker = SimpleNamespace(
        root=tmp_path, identity=INTERVIEW, execution=SimpleNamespace(catalog=object())
    )

    cli._trace_replay_worker_request(
        SimpleNamespace(),
        worker,
        {
            "golden": "golden.jsonl",
            "interview": "docassemble.other:data/questions/main.yml",
        },
    )

    assert seen["interview"] == "docassemble.other:data/questions/main.yml"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"expected": "g", "actual": "r", "phases": "intake,close"},
            "phases is only used with order phased",
        ),
        (
            {
                "expected": "g",
                "actual": "r",
                "order": "ordered",
                "phases": ["intake"],
            },
            "phases is only used with order phased",
        ),
        (
            {"expected": "g", "actual": "r", "update": True},
            "update is not a worker capability",
        ),
    ],
)
def test_trace_compare_worker_rejects_policy_the_cli_refuses(
    tmp_path, payload, message
):
    """The CLI refuses these pairings; the worker must not ignore them."""

    with pytest.raises(WorkerProtocolError, match=message):
        cli._trace_compare_worker_request(payload)


def test_trace_compare_worker_rejects_an_itemless_declared_phase_order():
    """``--phases ","`` is still a declaration the CLI refuses without phased."""
    with pytest.raises(
        WorkerProtocolError, match="phases is only used with order phased"
    ):
        cli._trace_compare_worker_request(
            {
                "expected": "g",
                "actual": "r",
                "order": "unordered",
                "phases": ",",
            }
        )


def test_trace_replay_worker_rejects_the_update_field(tmp_path):
    """``--update`` is the CLI's golden write path; a worker never writes one."""
    worker = _replay_worker(tmp_path)

    with pytest.raises(WorkerProtocolError, match="update is not a worker capability"):
        cli._trace_replay_worker_request(
            SimpleNamespace(), worker, {"golden": "g", "update": True}
        )


def test_trace_requests_flow_through_the_worker_protocol(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_trace_compare_report", lambda *a, **k: _comparison(True))
    monkeypatch.setattr(
        cli, "_trace_replay_report", lambda root, golden, **kwargs: _replay_report(True)
    )
    worker = _worker(
        tmp_path,
        cli._trace_worker_handlers(
            SimpleNamespace(
                interview=None,
                _resolved_configuration=None,
            )
        ),
    )
    incoming = io.StringIO(
        '{"id":"one","operation":"trace-compare","expected":"g","actual":"r"}\n'
        '{"id":"two","operation":"trace-replay","golden":"g"}\n'
        '{"id":"three","operation":"shutdown"}\n'
    )
    outgoing = io.StringIO()

    assert serve(worker, incoming, outgoing) == 0
    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]

    assert responses[0]["ok"] is True
    assert responses[0]["result"]["matched"] is True
    assert responses[1]["ok"] is True
    assert responses[1]["result"]["clean"] is True
    assert responses[2]["result"]["kind"] == "shutdown"
    assert worker.execution.operations == []


def test_trace_mismatch_reaches_the_envelope_as_a_trace_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "_trace_compare_report", lambda *a, **k: _comparison(False)
    )
    worker = _worker(tmp_path, cli._trace_worker_handlers(SimpleNamespace()))

    response = worker.handle(
        {"operation": "trace-compare", "expected": "g", "actual": "r"}
    )

    assert response["ok"] is False
    assert response["error"]["kind"] == "trace-mismatch"
    assert response["error"]["message"] == "screen trace comparison failed"
    assert response["error"]["details"]["matched"] is False


def test_malformed_trace_request_is_a_typed_input_error(tmp_path):
    worker = _worker(tmp_path, cli._trace_worker_handlers(SimpleNamespace()))

    response = worker.handle({"operation": "trace-compare", "actual": "r"})

    assert response["ok"] is False
    assert response["error"]["kind"] == "input"
    assert "expected" in response["error"]["message"]


# -- replay seam -----------------------------------------------------------


def _single_entry_trace():
    screen = {
        "kind": "question",
        "question_type": "fields",
        "question_name": "ID intro",
        "question_text": "Intro",
        "subquestion_text": "",
        "sought": None,
        "orig_sought": None,
        "fields": [
            {"variable": "user_name", "type": "text", "visible": True, "required": True}
        ],
    }
    return ScreenTrace(
        metadata=TraceMetadata(
            interview=INTERVIEW,
            config_fingerprint="fingerprint-1",
            docassemble="1.10.10",
            assemblyline="4.8.0",
            simulator="26.9.0",
        ),
        entries=(
            TraceEntry(
                seq=1,
                operation="start",
                phase=None,
                submitted={},
                ok=True,
                screen=screen,
                identity=ScreenIdentity("id:intro", "explicit-id"),
                content=(),
            ),
        ),
    )


def test_trace_replay_report_runs_in_a_temporary_session_directory(
    tmp_path, monkeypatch
):
    from docassemble_simulator import execution as execution_module

    trace = _single_entry_trace()
    golden = tmp_path / "golden.jsonl"
    write_trace(golden, trace)
    original = golden.read_bytes()
    captured = {}
    catalog = object()

    class FakeExecution:
        def __init__(self, root, interview, **kwargs):
            captured["root"] = root
            captured["interview"] = interview
            captured["session_directory"] = kwargs["session_directory"]
            captured["catalog"] = kwargs["catalog"]

        def run(self, operation):
            return SimpleNamespace(ok=True, result=trace.entries[0].screen, error=None)

    monkeypatch.setattr(execution_module, "InterviewExecution", FakeExecution)
    monkeypatch.setattr(
        cli,
        "_trace_metadata",
        lambda root, interview, resolved_configuration=None: trace.metadata,
    )

    report = cli._trace_replay_report(tmp_path, str(golden), catalog=catalog)

    assert report["clean"] is True
    assert captured["root"] == tmp_path
    assert captured["interview"] == INTERVIEW
    # The replay's isolated execution compiles through the shared catalog.
    assert captured["catalog"] is catalog
    scope = captured["session_directory"]
    assert scope.parent.parent == tmp_path / ".simulator"
    assert scope.parent.name.startswith("trace-replay-")
    # The temporary session directory is removed with the replay, so a replay
    # leaves no durable session behind in the worker's project.
    assert not scope.exists()
    # The golden is only read, never written.
    assert golden.read_bytes() == original


def test_trace_replay_worker_never_runs_the_workers_own_execution(
    monkeypatch, tmp_path
):
    from docassemble_simulator import execution as execution_module

    trace = _single_entry_trace()
    golden = tmp_path / "golden.jsonl"
    write_trace(golden, trace)

    class FakeExecution:
        def __init__(self, root, interview, **kwargs):
            pass

        def run(self, operation):
            return SimpleNamespace(ok=True, result=trace.entries[0].screen, error=None)

    monkeypatch.setattr(execution_module, "InterviewExecution", FakeExecution)
    monkeypatch.setattr(
        cli,
        "_trace_metadata",
        lambda root, interview, resolved_configuration=None: trace.metadata,
    )
    worker = _worker(
        tmp_path,
        cli._trace_worker_handlers(
            SimpleNamespace(interview=None, _resolved_configuration=None)
        ),
    )

    response = worker.handle({"operation": "trace-replay", "golden": str(golden)})

    assert response["ok"] is True, response
    assert response["result"]["clean"] is True
    # The replay uses its own isolated execution; the worker's execution never
    # serves a replay operation.
    assert worker.execution.operations == []


# -- compare parity --------------------------------------------------------


def _compare_entry(seq, key, *, phase=None, text="Text"):
    screen = {
        "kind": "question",
        "question_type": "fields",
        "question_name": f"ID {key}",
        "question_text": text,
        "subquestion_text": "",
        "sought": None,
        "orig_sought": None,
        "fields": [
            {"variable": "user_name", "type": "text", "visible": True, "required": True}
        ],
    }
    return TraceEntry(
        seq=seq,
        operation="answer",
        phase=phase,
        submitted={},
        ok=True,
        screen=screen,
        identity=ScreenIdentity(key, "explicit-id"),
        content=(),
    )


def _write_compare_trace(path, entries):
    write_trace(
        path,
        ScreenTrace(
            metadata=TraceMetadata(
                interview=INTERVIEW,
                config_fingerprint="fingerprint-1",
                docassemble="1.10.10",
                assemblyline="4.8.0",
                simulator="26.9.0",
                phase_order=("intake", "close"),
            ),
            entries=tuple(entries),
        ),
    )


def test_trace_compare_worker_report_equals_the_cli_for_every_policy_case(
    tmp_path, capsys, monkeypatch
):
    """The worker's compare report is exactly the CLI's for every policy case."""
    monkeypatch.setattr(cli, "_reexec_with_dyld_path", lambda: None)
    base_entries = [
        _compare_entry(1, "id:intro", phase="intake"),
        _compare_entry(2, "id:done", phase="close", text="Done"),
    ]
    base = tmp_path / "base.jsonl"
    _write_compare_trace(base, base_entries)
    reordered = tmp_path / "reordered.jsonl"
    _write_compare_trace(
        reordered,
        [
            _compare_entry(1, "id:done", phase="close", text="Done"),
            _compare_entry(2, "id:intro", phase="intake"),
        ],
    )
    changed = tmp_path / "changed.jsonl"
    _write_compare_trace(
        changed,
        [
            _compare_entry(1, "id:intro", phase="intake", text="Changed"),
            _compare_entry(2, "id:done", phase="close", text="Done"),
        ],
    )
    missing = tmp_path / "missing.jsonl"
    _write_compare_trace(missing, [_compare_entry(1, "id:intro", phase="intake")])
    extra = tmp_path / "extra.jsonl"
    _write_compare_trace(
        extra, [*base_entries, _compare_entry(3, "id:extra", phase="close")]
    )
    exceptions = tmp_path / "exceptions.toml"
    exceptions.write_text(
        "[[exceptions]]\n"
        f'interview = "{INTERVIEW}"\n'
        'phase = "*"\n'
        'identity = "id:done"\n'
        'category = "capability-boundary"\n'
        'reason = "known difference"\n',
        encoding="utf-8",
    )

    cases = (
        ("matching", base, base, [], {}),
        ("missing", base, missing, [], {}),
        ("extra", base, extra, [], {}),
        ("ordered", base, reordered, [], {}),
        (
            "unordered",
            base,
            reordered,
            ["--order", "unordered"],
            {"order": "unordered"},
        ),
        (
            "phased",
            base,
            reordered,
            ["--order", "phased", "--phases", "intake,close"],
            {"order": "phased", "phases": "intake,close"},
        ),
        ("full-text-default", base, changed, [], {}),
        ("full-text", base, changed, ["--full-text"], {"full_text": True}),
        (
            "exceptions",
            base,
            missing,
            ["--exceptions", str(exceptions)],
            {"exceptions": str(exceptions)},
        ),
    )

    for name, expected_path, actual_path, cli_args, request in cases:
        args = cli.build_parser().parse_args(
            [
                "--json",
                "trace",
                "compare",
                str(expected_path),
                str(actual_path),
                *cli_args,
            ]
        )
        cli.cmd_trace(args, tmp_path)
        cli_payload = json.loads(capsys.readouterr().out)

        outcome = cli._trace_compare_worker_request(
            {
                "expected": str(expected_path),
                "actual": str(actual_path),
                **request,
            },
        )
        if outcome.ok:
            assert cli_payload["ok"] is True, name
            assert outcome.result == cli_payload["result"], name
        else:
            assert cli_payload["ok"] is False, name
            assert outcome.error.kind == ErrorKind.TRACE_MISMATCH, name
            assert outcome.error.details == cli_payload["error"]["details"], name
