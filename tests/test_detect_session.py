from __future__ import annotations

import datetime
import sys
import types
from types import SimpleNamespace

import pytest

from docassemble_simulator.detect import guess_main_interview, resolve_interview
from docassemble_simulator.execution import (
    Answer,
    Evaluate,
    InterviewExecution,
    Start,
    Status,
)


@pytest.fixture
def pkg_root(tmp_path):
    for pkg in ("pkg1", "pkg2"):
        qdir = tmp_path / "docassemble" / pkg / "data" / "questions"
        qdir.mkdir(parents=True)
        (qdir.parent.parent / "__init__.py").write_text("")
        (qdir / "main.yml").write_text("---\nquestion: x\n")
        (qdir / "shared.yml").write_text("---\nquestion: y\n")
    return tmp_path


def test_guess_main_interview_prefers_main():
    assert guess_main_interview(
        [
            "docassemble.pkg:data/questions/x.yml",
            "docassemble.pkg:data/questions/main.yml",
        ]
    ).endswith("main.yml")


def test_resolve_interview_rejects_ambiguous_filename(pkg_root):
    with pytest.raises(SystemExit, match="ambiguous"):
        resolve_interview(pkg_root, "shared.yml")


def _runtime(monkeypatch):
    class DAObject(SimpleNamespace):
        def __init__(self, instanceName=None, **values):
            super().__init__(instanceName=instanceName, **values)

    class DADict:
        def __init__(self, *, elements):
            self.elements = elements

    DADateTime = datetime.datetime

    def as_datetime(value):
        parsed = datetime.date.fromisoformat(value)
        return DADateTime(
            parsed.year, parsed.month, parsed.day, tzinfo=datetime.timezone.utc
        )

    parse = sys.modules["docassemble.base.parse"]
    parse.get_initial_dict = lambda: {
        "_internal": {"tracker": 0, "answers": {}, "objselections": {}},
        "nav": DAObject("nav", sections=None),
        "url_args": {},
    }
    util = types.ModuleType("docassemble.base.util")
    util.DAObject = DAObject
    util.DADict = DADict
    util.DADateTime = DADateTime
    util.as_datetime = as_datetime
    monkeypatch.setitem(sys.modules, "docassemble.base.util", util)
    error = sys.modules["docassemble.base.error"]
    error.DAErrorNoEndpoint = type("DAErrorNoEndpoint", (Exception,), {})
    return SimpleNamespace(
        DAErrorNoEndpoint=error.DAErrorNoEndpoint, DADateTime=DADateTime
    )


class FakeInterview:
    source = SimpleNamespace(path="main.yml", package="docassemble.pkg")
    questions_by_name = {}

    def populate_non_pickleable(self, namespace):
        namespace["helper"] = lambda value: f"${value}"

    def assemble(self, namespace, interview_status):
        raise sys.modules["docassemble.base.error"].DAErrorNoEndpoint("finished")


def _execution(tmp_path, monkeypatch, da_stubs):
    qdir = tmp_path / "docassemble" / "pkg" / "data" / "questions"
    qdir.mkdir(parents=True)
    (qdir / "main.yml").write_text("---\nquestion: x\n")
    runtime = _runtime(monkeypatch)
    execution = InterviewExecution(tmp_path, "docassemble.pkg:data/questions/main.yml")
    monkeypatch.setattr(
        execution.catalog, "compile", lambda identity=None: FakeInterview()
    )
    return execution, runtime


def test_start_commits_versioned_per_interview_state_and_status_is_pure(
    tmp_path, monkeypatch, da_stubs
):
    execution, _ = _execution(tmp_path, monkeypatch, da_stubs)

    started = execution.run(Start())
    before = execution.store.path.read_bytes()
    status = execution.run(Status())

    assert started.ok and started.result["kind"] == "finished"
    assert status.result == started.result
    assert execution.store.path.read_bytes() == before
    assert execution.store.path.parent.name == "sessions"
    assert "main.yml" in execution.store.path.name


def test_read_only_evaluation_rehydrates_callables_without_changing_session(
    tmp_path, monkeypatch, da_stubs
):
    execution, _ = _execution(tmp_path, monkeypatch, da_stubs)
    assert execution.run(Start()).ok
    before = execution.store.path.read_bytes()

    result = execution.run(Evaluate("helper(12)"))

    assert result.ok and result.result["value"] == "'$12'"
    assert execution.store.path.read_bytes() == before


def test_date_answer_is_field_aware_and_invalid_multi_answer_rolls_back(
    tmp_path, monkeypatch, da_stubs
):
    execution, runtime = _execution(tmp_path, monkeypatch, da_stubs)
    screen = {
        "kind": "question",
        "question_name": None,
        "fields": [
            {"variable": "filing_date", "type": "date", "required": False},
            {"variable": "caption", "type": "text", "required": False},
        ],
    }
    execution.store.save(
        {
            "_internal": {"tracker": 0, "objselections": {}},
            "nav": SimpleNamespace(sections=None),
        },
        screen,
    )

    accepted = execution.run(
        Answer((("filing_date", "2026-08-26"), ("caption", "2026-08-26")))
    )
    state = execution.store.load()["namespace"]
    assert accepted.ok
    assert isinstance(state["filing_date"], runtime.DADateTime)
    assert state["caption"] == "2026-08-26"

    execution.store.save(state, screen)
    before = execution.store.path.read_bytes()
    rejected = execution.run(
        Answer((("filing_date", "2026-02-30"), ("caption", "changed")))
    )
    assert not rejected.ok and rejected.error.kind == "answer-input"
    assert execution.store.path.read_bytes() == before
