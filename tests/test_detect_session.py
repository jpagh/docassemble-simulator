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
    Execute,
    InterviewExecution,
    PrepareRender,
    RenderSource,
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
        execution._catalog, "_compile", lambda identity=None: FakeInterview()
    )
    return execution, runtime


def test_start_commits_versioned_per_interview_state_and_status_is_pure(
    tmp_path, monkeypatch, da_stubs
):
    execution, _ = _execution(tmp_path, monkeypatch, da_stubs)

    started = execution.run(Start())
    before = execution._store.path.read_bytes()
    status = execution.run(Status())

    assert started.ok and started.result["kind"] == "finished"
    assert status.result == started.result
    assert execution._store.path.read_bytes() == before
    assert execution._store.path.parent.name == "sessions"
    assert "main.yml" in execution._store.path.name


def test_flow_error_is_committed_but_reported_as_failed_operation(
    tmp_path, monkeypatch, da_stubs
):
    execution, _ = _execution(tmp_path, monkeypatch, da_stubs)

    class BrokenInterview(FakeInterview):
        def assemble(self, namespace, interview_status):
            namespace["debug_value"] = 42
            raise RuntimeError("authored flow broke")

    monkeypatch.setattr(
        execution._catalog, "_compile", lambda identity=None: BrokenInterview()
    )

    outcome = execution.run(Start())

    assert not outcome.ok and outcome.error.kind == "execution"
    assert execution.run(Status()).result["kind"] == "error"
    assert execution._store.load()["namespace"]["debug_value"] == 42


def test_read_only_evaluation_rehydrates_callables_without_changing_session(
    tmp_path, monkeypatch, da_stubs
):
    execution, _ = _execution(tmp_path, monkeypatch, da_stubs)
    assert execution.run(Start()).ok
    before = execution._store.path.read_bytes()

    result = execution.run(Evaluate("helper(12)"))

    assert result.ok and result.result["value"] == "'$12'"
    assert execution._store.path.read_bytes() == before


@pytest.mark.parametrize("source_kind", ["saved", "fresh", "snapshot", "fixture"])
def test_every_render_source_rehydrates_before_the_context_action(
    source_kind, tmp_path, monkeypatch, da_stubs
):
    execution, _ = _execution(tmp_path, monkeypatch, da_stubs)
    namespace = {
        "_internal": {"tracker": 0, "objselections": {}},
        "nav": SimpleNamespace(sections=None),
    }
    screen = {"kind": "finished"}
    execution._store.save(namespace, screen)
    before = execution._store.path.read_bytes()
    source_path = None
    if source_kind == "snapshot":
        source_path = tmp_path / "snapshot.pkl"
        execution._store.save_snapshot(source_path, namespace)
    elif source_kind == "fixture":
        source_path = tmp_path / "fixture.py"
        source_path.write_text("fixture_ran = True\n")

    outcome = execution.run(
        PrepareRender(
            RenderSource(source_kind, source_path),
            assemble=False,
            save_snapshot=None,
            action=lambda prepared: (
                prepared["helper"](7),
                prepared.get("fixture_ran", False),
            ),
        )
    )

    assert outcome.ok
    assert outcome.result[0] == "$7"
    assert outcome.result[1] is (source_kind == "fixture")
    assert execution._store.path.read_bytes() == before


def test_failed_atomic_replacement_preserves_the_previous_session(
    tmp_path, monkeypatch, da_stubs
):
    import docassemble_simulator.execution as execution_module

    execution, _ = _execution(tmp_path, monkeypatch, da_stubs)
    namespace = {
        "_internal": {"tracker": 0, "objselections": {}},
        "nav": SimpleNamespace(sections=None),
        "counter": 1,
    }
    execution._store.save(namespace, {"kind": "executed"})
    before = execution._store.path.read_bytes()
    monkeypatch.setattr(
        execution_module.os,
        "replace",
        lambda source, target: (_ for _ in ()).throw(OSError("disk failure")),
    )

    outcome = execution.run(Execute("counter += 1", assemble=False))

    assert not outcome.ok and outcome.error.kind == "fault"
    assert execution._store.path.read_bytes() == before
    assert not list(execution._store.path.parent.glob("*.tmp"))


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
    execution._store.save(
        {
            "_internal": {"tracker": 0, "objselections": {}},
            "nav": SimpleNamespace(sections=None),
        },
        screen,
    )

    accepted = execution.run(
        Answer((("filing_date", "2026-08-26"), ("caption", "2026-08-26")))
    )
    state = execution._store.load()["namespace"]
    assert accepted.ok
    assert isinstance(state["filing_date"], runtime.DADateTime)
    assert state["caption"] == "2026-08-26"

    execution._store.save(state, screen)
    empty = execution.run(Answer((("filing_date", ""), ("caption", "kept"))))
    assert empty.ok
    assert execution._store.load()["namespace"]["filing_date"] == ""

    execution._store.save(state, screen)
    before = execution._store.path.read_bytes()
    rejected = execution.run(
        Answer((("filing_date", "2026-02-30"), ("caption", "changed")))
    )
    assert not rejected.ok and rejected.error.kind == "answer-input"
    assert execution._store.path.read_bytes() == before
