"""Screen-submission dispositions and guidance through public execution."""

from __future__ import annotations

import datetime
import sys
import types
from types import SimpleNamespace

import pytest

from docassemble_simulator.execution import Answer, InterviewExecution, Start

INTERVIEW = "docassemble.pkg:data/questions/main.yml"


class ScreenInterview:
    source = SimpleNamespace(path="main.yml", package="docassemble.pkg")
    fields = ()

    def populate_non_pickleable(self, namespace):
        return None

    def assemble(self, namespace, interview_status):
        interview_status.question = SimpleNamespace(
            question_type="fields",
            name="answer-contract",
            validation_code=None,
            fields=list(self.fields),
        )
        interview_status.question_text = "Answer contract"
        interview_status.subquestion_text = None
        interview_status.continue_label = None
        interview_status.sought = None
        interview_status.orig_sought = None
        interview_status.selectcompute = {}


class AssignmentInterview(ScreenInterview):
    fields = (
        SimpleNamespace(saveas="name", datatype="text", required=False),
        SimpleNamespace(saveas="filing_date", datatype="date", required=False),
    )


class CheckboxInterview(ScreenInterview):
    fields = (
        SimpleNamespace(
            saveas="topics",
            datatype="checkboxes",
            required=False,
            choices=(("housing", "Housing"),),
        ),
    )


@pytest.fixture
def answer_execution(tmp_path, monkeypatch, da_stubs):
    """Build a real public execution around a controlled interview substitute."""

    class DAObject(SimpleNamespace):
        def __init__(self, instanceName=None, **values):
            super().__init__(instanceName=instanceName, **values)

    class DADict:
        def __init__(self, *, elements):
            self.elements = elements

    parse = sys.modules["docassemble.base.parse"]
    parse.get_initial_dict = lambda: {
        "_internal": {"tracker": 0, "answers": {}, "objselections": {}},
        "nav": DAObject("nav", sections=None),
        "url_args": {},
    }
    util = types.ModuleType("docassemble.base.util")
    util.DAObject = DAObject
    util.DADict = DADict
    util.DAEmpty = type("DAEmpty", (), {})
    util.DADateTime = datetime.datetime
    util.as_datetime = lambda value: datetime.datetime.fromisoformat(value)
    monkeypatch.setitem(sys.modules, "docassemble.base.util", util)

    error = sys.modules["docassemble.base.error"]
    error.DAErrorNoEndpoint = type("DAErrorNoEndpoint", (Exception,), {})

    selected = {"interview": AssignmentInterview}
    cache = types.ModuleType("docassemble.base.interview_cache")
    cache.get_interview = lambda identity: selected["interview"]()
    monkeypatch.setitem(sys.modules, "docassemble.base.interview_cache", cache)

    qdir = tmp_path / "docassemble" / "pkg" / "data" / "questions"
    qdir.mkdir(parents=True)
    qdir.parent.parent.joinpath("__init__.py").write_text("")
    (qdir / "main.yml").write_text("---\nquestion: answer contract\n")

    execution = InterviewExecution(tmp_path, INTERVIEW)
    return execution, selected


def session_bytes(execution):
    """Observe durable state through the public execution scope."""

    return execution.scope.session.read_bytes()


def test_answer_outcome_reports_applied_unknown_and_parse_error(answer_execution):
    execution, _selected = answer_execution

    assert execution.run(Start()).ok
    before = session_bytes(execution)
    rejected = execution.run(
        Answer(
            (
                ("name", "Ada"),
                ("nmae", "typo"),
                ("filing_date", "not-a-date"),
            )
        )
    )

    assert not rejected.ok
    assert rejected.error.kind == "answer-input"
    assignments = rejected.error.details["assignments"]
    assert [item["disposition"] for item in assignments] == [
        "applied",
        "unknown field",
        "parse error",
    ]
    assert assignments[1]["input"] == "typo"
    assert assignments[2]["input"] == "not-a-date"
    assert any("filing_date" in error for error in rejected.error.details["errors"])
    assert rejected.error.details["active_fields"] == ["name", "filing_date"]
    assert rejected.error.details["closest_matches"]["nmae"] == "name"
    assert session_bytes(execution) == before


def test_answer_error_payload_preserves_active_field_examples(answer_execution):
    execution, selected = answer_execution
    selected["interview"] = CheckboxInterview

    assert execution.run(Start()).ok
    before = session_bytes(execution)
    rejected = execution.run(Answer((("topic", "{}"),)))

    assert not rejected.ok
    assert rejected.error.kind == "answer-input"
    details = rejected.error.details
    assert details["active_fields"] == ["topics"]
    assert details["active_field_details"][0]["example"] == 'topics={"housing":true}'
    assert details["closest_matches"] == {"topic": "topics"}
    assert session_bytes(execution) == before
