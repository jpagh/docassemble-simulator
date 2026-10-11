from __future__ import annotations

import datetime
import sys
import types
from types import SimpleNamespace

from docassemble_simulator.execution import ExplainResolution, InterviewExecution, Start


def _install_runtime(monkeypatch):
    """Install only the controlled runtime surface the public execution uses."""
    parse = sys.modules["docassemble.base.parse"]
    parse.get_initial_dict = lambda: {
        "_internal": {"tracker": 0, "answers": {}, "objselections": {}},
        "nav": SimpleNamespace(instanceName="nav", sections=None),
        "url_args": {},
    }
    util = types.ModuleType("docassemble.base.util")
    util.DAObject = SimpleNamespace
    util.DADateTime = datetime.datetime
    monkeypatch.setitem(sys.modules, "docassemble.base.util", util)
    monkeypatch.setattr(
        sys.modules["docassemble.base.functions"],
        "get_language",
        lambda: "en",
        raising=False,
    )


def _question(source, *, identifier, guard):
    return SimpleNamespace(
        id=identifier,
        name=identifier,
        question_type="fields",
        line_number=1,
        from_source=SimpleNamespace(
            path="docassemble.pkg:data/questions/main.yml",
            filepath=str(source),
        ),
        source_code=(
            "---\n"
            f"id: {identifier}\n"
            f"if: {guard}\n"
            "question: Example\n"
            "fields:\n  - Value: shared\n"
        ),
        data_for_debug={"if": guard},
        fields=[SimpleNamespace(saveas="shared")],
    )


def _execution(tmp_path, monkeypatch, da_stubs, *, guards):
    _install_runtime(monkeypatch)
    questions_path = tmp_path / "docassemble" / "pkg" / "data" / "questions"
    questions_path.mkdir(parents=True)
    (questions_path / "main.yml").write_text("---\nquestion: controlled\n")
    questions = [
        _question(
            questions_path / "main.yml",
            identifier=f"candidate-{index}",
            guard=guard,
        )
        for index, guard in enumerate(guards, start=1)
    ]

    class DAErrorNoEndpoint(Exception):
        pass

    sys.modules["docassemble.base.error"].DAErrorNoEndpoint = DAErrorNoEndpoint

    class ControlledInterview:
        source = SimpleNamespace(path="main.yml", package="docassemble.pkg")
        default_language = "en"

        def populate_non_pickleable(self, namespace):
            namespace["user"] = SimpleNamespace(kind="a")

        def assemble(self, namespace, interview_status):
            raise DAErrorNoEndpoint("finished")

    ControlledInterview.questions_list = questions
    ControlledInterview.questions = {"shared": {"*": questions}}

    cache = types.ModuleType("docassemble.base.interview_cache")
    cache.get_interview = lambda identity: ControlledInterview()
    monkeypatch.setitem(sys.modules, "docassemble.base.interview_cache", cache)
    return InterviewExecution(
        tmp_path,
        "docassemble.pkg:data/questions/main.yml",
    )


def _session_bytes(execution):
    return execution.scope.session.read_bytes()


def test_resolution_explanation_without_saved_state_stays_static(
    tmp_path, monkeypatch, da_stubs
):
    execution = _execution(
        tmp_path,
        monkeypatch,
        da_stubs,
        guards=['user.kind == "a"', 'user.kind == "b"'],
    )

    outcome = execution.run(ExplainResolution("shared"))

    assert outcome.ok
    assert outcome.result["session_state"] == "not saved"
    assert [candidate["live"] for candidate in outcome.result["candidates"]] == [
        None,
        None,
    ]
    assert "state" not in outcome.result["candidates"][0]
    assert not execution.scope.session.exists()


def test_resolution_explanation_prepares_saved_state_without_committing(
    tmp_path, monkeypatch, da_stubs
):
    execution = _execution(
        tmp_path,
        monkeypatch,
        da_stubs,
        guards=['missing_guard == "yes"', 'user.kind == "a"'],
    )
    assert execution.run(Start()).ok
    before = _session_bytes(execution)

    outcome = execution.run(ExplainResolution("shared"))

    assert outcome.ok
    assert outcome.result["session_state"] == "saved"
    candidates = outcome.result["candidates"]
    assert candidates[0]["state"] == "live"
    assert candidates[0]["live"] is True
    assert candidates[1]["state"] == "unknown"
    assert candidates[1]["live"] is None
    assert "NameError" in candidates[1]["guard_error"]
    assert outcome.result["conflict"] is True
    assert outcome.result["winner"] is None
    assert _session_bytes(execution) == before
