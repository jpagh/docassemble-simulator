"""The command adapter's read-only catalog/project requests for the worker.

``worker.py`` stays an operation-only server; each read-only catalog/project
request is mapped by ``cli``'s command adapter onto the same payload the
one-shot ``sim <command> --json`` builds, and served through the worker's
generic handler seam. These tests pin dispatch, payload parity (including the
``--json`` None-dropping cleaning), and the error shapes without a real
runtime.
"""

from __future__ import annotations

import io
import json
import os
from types import SimpleNamespace

import pytest
from test_catalog import _install_interview_cache, _reload_workspace

from docassemble_simulator import catalog as catalog_module
from docassemble_simulator import cli
from docassemble_simulator._outcomes import Outcome
from docassemble_simulator.catalog import CatalogOutcome, InterviewCatalog
from docassemble_simulator.execution import (
    ExecutionScope,
    ExplainResolution,
    InterviewExecution,
)
from docassemble_simulator.worker import Worker, serve

_INTERVIEW = "docassemble.example:data/questions/main.yml"


class _FakeExecution:
    """A worker execution that answers one fixed outcome read-only."""

    _identity = _INTERVIEW

    def __init__(self, tmp_path, outcome=None, catalog=None):
        self.scope = ExecutionScope(
            tmp_path,
            self._identity,
            "",
            tmp_path / "sessions",
            tmp_path / "sessions" / "main.pkl",
        )
        self.outcome = outcome
        self.catalog = catalog
        self.operations = []

    def run(self, operation):
        self.operations.append(operation)
        return self.outcome

    def reset(self):
        pass


def _args(config=None):
    return SimpleNamespace(_simulator_config=config or {})


def _handlers(args=None):
    return cli._worker_request_handlers(args or _args())


def _patch_catalog(monkeypatch, outcome, constructed):
    class FakeCatalog:
        def __init__(self, root=None, selector=None, **kwargs):
            constructed.append((root, selector))

        def check(self):
            return outcome

        def questions(self, contains=None):
            return outcome

        def index(self, contains=None):
            return outcome

    monkeypatch.setattr(catalog_module, "InterviewCatalog", FakeCatalog)
    return FakeCatalog()


def _cli_catalog(monkeypatch, capsys, argv, root, expected_code):
    args = cli.build_parser().parse_args(argv)
    assert cli.cmd_catalog(args, root) == expected_code
    return json.loads(capsys.readouterr().out)


def _workspace(tmp_path):
    package = tmp_path / "docassemble" / "example"
    (package / "data" / "questions").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    return package


def test_worker_catalog_requests_match_the_json_envelope(tmp_path, monkeypatch, capsys):
    """Listing/check payloads equal ``sim <command> --json`` for the same catalog."""
    result = {"blocks": [{"index": 0, "name": "Q", "variables": None}]}
    outcome = CatalogOutcome(_INTERVIEW, result)
    constructed = []
    catalog = _patch_catalog(monkeypatch, outcome, constructed)

    expected = _cli_catalog(monkeypatch, capsys, ["questions", "--json"], tmp_path, 0)
    execution = _FakeExecution(tmp_path, catalog=catalog)
    worker = Worker(tmp_path, execution=execution, handlers=_handlers())

    response = worker.handle({"operation": "questions"})

    assert response["ok"] is True
    assert response["result"] == expected["result"]
    # The CLI drops the None-valued ``variables`` key; the worker must too.
    assert "variables" not in response["result"]["blocks"][0]
    # The worker compiles through its execution's own catalog, not a new one.
    assert worker.execution.catalog is catalog


def test_worker_check_error_shapes_match_the_json_envelope(
    tmp_path, monkeypatch, capsys
):
    compile_result = {
        "checked": 1,
        "failures": 1,
        "duplicate_ids": [],
        "totals": {"checked": 1, "ok": 0, "failed": 1, "duplicate_ids": 0},
        "results": [{"interview": _INTERVIEW, "ok": False, "message": "broken"}],
    }
    catalog = _patch_catalog(monkeypatch, CatalogOutcome(None, compile_result), [])
    expected = _cli_catalog(monkeypatch, capsys, ["check", "--json"], tmp_path, 2)
    worker = Worker(
        tmp_path,
        execution=_FakeExecution(tmp_path, catalog=catalog),
        handlers=_handlers(),
    )

    response = worker.handle({"operation": "check"})

    assert response["ok"] is False
    assert response["error"]["kind"] == expected["error"]["kind"] == "compile"
    assert response["error"]["message"] == expected["error"]["message"]
    assert response["error"]["details"] == expected["error"]["details"]

    duplicate_result = {
        "checked": 1,
        "failures": 0,
        "duplicate_ids": [{"id": "shared", "kind": "duplicate-id"}],
        "totals": {"checked": 1, "ok": 1, "failed": 0, "duplicate_ids": 1},
        "results": [],
    }
    duplicate_catalog = _patch_catalog(
        monkeypatch, CatalogOutcome(None, duplicate_result), []
    )
    expected = _cli_catalog(
        monkeypatch, capsys, ["check", "--strict", "--json"], tmp_path, 2
    )
    strict_worker = Worker(
        tmp_path,
        execution=_FakeExecution(tmp_path, catalog=duplicate_catalog),
        handlers=_handlers(),
    )

    strict = strict_worker.handle({"operation": "check", "strict": True})

    assert strict["ok"] is False
    assert strict["error"]["kind"] == expected["error"]["kind"] == "duplicate-id"


def test_worker_why_matches_the_json_envelope_and_reuses_execution(
    tmp_path, monkeypatch, capsys
):
    report = {
        "variable": "M.value",
        "order": "last-loaded-first",
        "session_state": "not saved",
        "candidates": [],
        "winner": None,
        "conflict": False,
        "conflict_details": None,
    }
    execution = _FakeExecution(tmp_path, Outcome(True, report))
    monkeypatch.setattr(cli, "_execution", lambda args, root: execution)
    expected = _cli_catalog(
        monkeypatch, capsys, ["why", "M.value", "--json"], tmp_path, 0
    )
    worker = Worker(tmp_path, execution=execution, handlers=_handlers())

    response = worker.handle({"operation": "why", "variable": "M.value"})

    assert response["ok"] is True
    assert response["result"] == expected["result"]
    # The CLI drops the None-valued ``winner``; the worker must too.
    assert "winner" not in response["result"]
    assert isinstance(execution.operations[-1], ExplainResolution)
    assert execution.operations[-1].variable == "M.value"


def test_worker_why_without_a_session_stays_read_only(tmp_path, monkeypatch, capsys):
    package = _workspace(tmp_path)
    (package / "data" / "questions" / "main.yml").write_text(
        "---\nquestion: Hello\nfields:\n  - Value: value\n"
    )
    session_root = tmp_path / "sessions"
    execution = InterviewExecution(tmp_path, session_directory=session_root)
    report = {
        "variable": "value",
        "order": "last-loaded-first",
        "session_state": "not saved",
        "candidates": [],
        "winner": None,
        "conflict": False,
        "conflict_details": None,
    }
    monkeypatch.setattr(
        execution._catalog,
        "why",
        lambda variable, namespace=None, **kwargs: CatalogOutcome(
            execution._catalog.identity, report
        ),
    )
    monkeypatch.setattr(cli, "_execution", lambda args, root: execution)
    expected = _cli_catalog(
        monkeypatch, capsys, ["why", "value", "--json"], tmp_path, 0
    )
    worker = Worker(tmp_path, execution=execution, handlers=_handlers())

    response = worker.handle({"operation": "why", "variable": "value"})

    assert response["ok"] is True
    assert response["result"] == expected["result"]
    assert response["result"]["session_state"] == "not saved"
    assert not list(session_root.glob("*.pkl"))


def test_worker_info_and_config_match_the_json_envelope(tmp_path, capsys):
    _workspace(tmp_path)
    for command, cmd in (("info", cli.cmd_info), ("config", cli.cmd_config)):
        args = cli.build_parser().parse_args([command, "--json"])
        assert cmd(args, tmp_path) == 0
        expected = json.loads(capsys.readouterr().out)

        worker = Worker(
            tmp_path, execution=_FakeExecution(tmp_path), handlers=_handlers()
        )
        response = worker.handle({"operation": command})

        assert response["ok"] is True
        assert response["result"] == expected["result"], command


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"operation": "why"}, "variable"),
        ({"operation": "why", "variable": 7}, "variable"),
        ({"operation": "questions", "var": 7}, "var must be a string"),
        ({"operation": "index", "var": 7}, "var must be a string"),
        ({"operation": "check", "strict": "yes"}, "strict must be a boolean"),
        ({"operation": "why", "variable": "M.value", "strict": "yes"}, "boolean"),
    ],
)
def test_malformed_catalog_requests_are_typed_input_errors(tmp_path, payload, message):
    worker = Worker(tmp_path, execution=_FakeExecution(tmp_path), handlers=_handlers())

    response = worker.handle(payload)

    assert response["ok"] is False
    assert response["error"]["kind"] == "input"
    assert message in response["error"]["message"]


def _question_count_interview(source):
    """A compiled interview whose block count tracks the authored source."""

    def factory():
        text = source.read_text(encoding="utf-8")
        return SimpleNamespace(
            questions_list=[SimpleNamespace() for _ in range(text.count("question:"))],
            questions={},
            default_language="en",
        )

    return factory


def test_worker_catalog_source_is_the_executions_compile_authority(tmp_path):
    """Inspection compiles through the execution's own catalog, not a copy."""
    catalog = object()
    execution = _FakeExecution(tmp_path, catalog=catalog)
    worker = Worker(tmp_path, execution=execution, handlers=_handlers())

    assert cli._worker_catalog_source(worker) is catalog


def test_worker_questions_observes_an_edit_through_the_shared_catalog(
    tmp_path, monkeypatch, da_stubs
):
    """A request after an edit inspects the current program with no refresh step.

    The worker compiles through one cached catalog whose ``_compile``
    revalidates every identity it is asked for, so the adapter needs no
    ensure-current ordering to see changed YAML.
    """
    source = _reload_workspace(tmp_path)
    calls = _install_interview_cache(
        monkeypatch, source, _question_count_interview(source)
    )
    catalog = InterviewCatalog(tmp_path, _INTERVIEW, cache_compiled=True)
    execution = InterviewExecution(tmp_path, _INTERVIEW, catalog=catalog)
    worker = Worker(tmp_path, execution=execution, handlers=_handlers())

    first = worker.handle({"operation": "questions"})
    assert first["ok"] is True
    assert len(first["result"]["blocks"]) == 1
    assert len(calls) == 1

    source.write_text(
        "---\nquestion: First\nfields:\n  - Name: user_name\n"
        "---\nquestion: Second\nfields:\n  - Name: user_name\n"
    )
    os.utime(source, (0, 0))

    second = worker.handle({"operation": "questions"})
    assert second["ok"] is True
    assert len(second["result"]["blocks"]) == 2
    assert len(calls) == 2


def test_worker_warm_requests_do_not_recompile(tmp_path, monkeypatch, da_stubs):
    """An unchanged request stays warm: one compile for repeated inspection."""
    source = _reload_workspace(tmp_path)
    calls = _install_interview_cache(
        monkeypatch, source, _question_count_interview(source)
    )
    catalog = InterviewCatalog(tmp_path, _INTERVIEW, cache_compiled=True)
    execution = InterviewExecution(tmp_path, _INTERVIEW, catalog=catalog)
    worker = Worker(tmp_path, execution=execution, handlers=_handlers())

    for _ in range(3):
        assert worker.handle({"operation": "questions"})["ok"] is True

    assert len(calls) == 1


def test_worker_failing_requests_compile_once_and_keep_the_typed_error(
    tmp_path, monkeypatch, da_stubs
):
    """A failing request pays one compile and reports its own typed error."""
    source = _reload_workspace(tmp_path)

    def broken():
        raise ValueError("broken interview")

    calls = _install_interview_cache(monkeypatch, source, broken)
    catalog = InterviewCatalog(tmp_path, _INTERVIEW, cache_compiled=True)
    execution = InterviewExecution(tmp_path, _INTERVIEW, catalog=catalog)
    worker = Worker(tmp_path, execution=execution, handlers=_handlers())

    check = worker.handle({"operation": "check"})
    assert check["ok"] is False
    assert check["error"]["kind"] == "compile"
    assert check["error"]["details"]["results"][0]["error_type"] == "ValueError"
    assert len(calls) == 1

    questions = worker.handle({"operation": "questions"})
    assert questions["ok"] is False
    assert questions["error"]["kind"] == "compile"
    assert len(calls) == 2


def test_a_failing_compile_leaves_a_saved_session_untouched(
    tmp_path, monkeypatch, da_stubs
):
    """A reload compile failure never clears or mutates the Saved session."""
    source = _reload_workspace(tmp_path)

    def broken():
        raise ValueError("broken interview")

    _install_interview_cache(monkeypatch, source, broken)
    catalog = InterviewCatalog(tmp_path, _INTERVIEW, cache_compiled=True)
    execution = InterviewExecution(
        tmp_path, _INTERVIEW, catalog=catalog, session_directory=tmp_path / "sessions"
    )
    execution._store.save({"answer": 1}, {"kind": "screen"})
    saved = execution._store.path.read_bytes()

    with (
        pytest.raises(ValueError, match="broken interview"),
        execution._prepared_operation({"answer": 1}),
    ):
        pass

    assert execution._store.path.read_bytes() == saved


def test_catalog_requests_flow_through_the_worker_protocol(tmp_path, monkeypatch):
    outcome = CatalogOutcome(_INTERVIEW, {"blocks": []})
    catalog = _patch_catalog(monkeypatch, outcome, [])
    worker = Worker(
        tmp_path,
        execution=_FakeExecution(tmp_path, catalog=catalog),
        handlers=_handlers(),
    )
    incoming = io.StringIO(
        '{"id":"one","operation":"questions"}\n'
        '{"id":"two","operation":"index"}\n'
        '{"id":"three","operation":"shutdown"}\n'
    )
    outgoing = io.StringIO()

    assert serve(worker, incoming, outgoing) == 0
    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]

    assert responses[0]["ok"] is True
    assert responses[0]["result"]["blocks"] == []
    assert responses[1]["ok"] is True
    assert responses[2]["result"]["kind"] == "shutdown"


def test_worker_catalog_handlers_are_registered():
    """The catalog family is exactly the declared verbs and the project pair."""
    args = _args()
    handlers = cli._catalog_worker_handlers(args)

    assert set(handlers) == {*cli._CATALOG_VERBS, "info", "config"}
    assert all(callable(handler) for handler in handlers.values())
    assert set(cli._worker_request_handlers(args)) >= set(handlers)
