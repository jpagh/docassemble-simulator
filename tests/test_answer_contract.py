"""Screen-submission dispositions and guidance through public execution."""

from __future__ import annotations

import datetime
import sys
import types
from types import SimpleNamespace
from typing import ClassVar

import pytest

from docassemble_simulator.execution import Answer, Evaluate, InterviewExecution, Start

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


class GuidanceInterview(ScreenInterview):
    fields = (
        SimpleNamespace(saveas="name", datatype="text", required=True),
        SimpleNamespace(
            saveas="filing_date",
            datatype="date",
            required=True,
            default="2026-01-15",
        ),
        SimpleNamespace(saveas="optional_date", datatype="date", required=False),
        SimpleNamespace(
            saveas="hidden_value",
            datatype="text",
            required=True,
            showif_code="False",
        ),
    )


class ValidatingGuidanceInterview(GuidanceInterview):
    questions_by_name: ClassVar = {
        "answer-contract": SimpleNamespace(validation_code="validation_marker = True")
    }


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


def test_early_rejection_reports_missing_required_fields_without_partial_state(
    answer_execution,
):
    execution, selected = answer_execution
    selected["interview"] = ValidatingGuidanceInterview

    assert execution.run(Start()).ok
    before = session_bytes(execution)
    rejected = execution.run(
        Answer(
            (
                ("name", "Ada"),
                ("nmae", "typo"),
                ("optional_date", "not-a-date"),
            )
        )
    )

    assert not rejected.ok
    assert rejected.error.kind == "answer-input"
    details = rejected.error.details
    assert [item["disposition"] for item in details["assignments"]] == [
        "applied",
        "unknown field",
        "parse error",
    ]
    assert details["missing_required"] == []
    assert details["active_fields"] == [
        "name",
        "filing_date",
        "optional_date",
        "hidden_value",
    ]
    assert details["closest_matches"] == {"nmae": "name"}
    assert session_bytes(execution) == before
    assert not execution.run(Evaluate("name")).ok
    assert (
        execution.run(Evaluate("'validation_marker' in globals()")).result["value"]
        == "False"
    )


def test_explicitly_empty_required_value_is_missing_on_unknown_rejection(
    answer_execution,
):
    execution, selected = answer_execution
    selected["interview"] = GuidanceInterview

    assert execution.run(Start()).ok
    rejected = execution.run(Answer((("name", ""), ("nmae", "typo"))))

    assert not rejected.ok
    assert rejected.error.kind == "answer-input"
    assert rejected.error.details["missing_required"] == ["name"]
    assert rejected.error.details["assignments"][0]["disposition"] == "applied"
    assert rejected.error.details["assignments"][1]["disposition"] == "unknown field"


def test_parse_rejection_reports_missing_required_but_not_defaults_or_hidden_fields(
    answer_execution,
):
    execution, selected = answer_execution
    selected["interview"] = GuidanceInterview

    assert execution.run(Start()).ok
    rejected = execution.run(Answer((("optional_date", "not-a-date"),)))

    assert not rejected.ok
    assert rejected.error.kind == "answer-input"
    details = rejected.error.details
    assert details["missing_required"] == ["name"]
    assert "filing_date" not in details["missing_required"]
    assert "hidden_value" not in details["missing_required"]
    assert details["active_fields"] == [
        "name",
        "filing_date",
        "optional_date",
        "hidden_value",
    ]


class CustomDateInterview(ScreenInterview):
    questions_by_name: ClassVar = {}
    fields = (
        SimpleNamespace(
            saveas="custom_date", datatype="RegisteredDate", required=False
        ),
    )


def test_answer_transforms_registered_custom_datatype(answer_execution, monkeypatch):
    execution, selected = answer_execution
    selected["interview"] = CustomDateInterview

    class RegisteredDate:
        @classmethod
        def call_validate(cls, raw, variable, data):
            assert variable == "custom_date"
            assert data == {}
            return True

        @classmethod
        def call_transform(cls, raw, variable, data):
            return datetime.datetime.fromisoformat(raw)

    monkeypatch.setattr(
        sys.modules["docassemble.base.functions"],
        "custom_types",
        {"RegisteredDate": {"class": RegisteredDate, "is_object": True}},
        raising=False,
    )
    assert execution.run(Start()).ok
    assert execution.run(Answer((("custom_date", "2020-01-02"),))).ok
    observed = execution.run(Evaluate("custom_date.isoformat()"))
    assert observed.ok
    assert "2020-01-02T00:00:00" in str(observed.result)


@pytest.mark.parametrize("failure", ["false", "validation", "transform"])
def test_answer_rejects_invalid_custom_datatype_atomically(
    answer_execution, monkeypatch, failure
):
    execution, selected = answer_execution
    selected["interview"] = CustomDateInterview

    class RegisteredDate:
        @classmethod
        def call_validate(cls, raw, variable, data):
            if failure == "validation":
                raise sys.modules["docassemble.base.error"].DAValidationError(
                    "bad date"
                )
            return failure != "false"

        @classmethod
        def call_transform(cls, raw, variable, data):
            raise TypeError("bad date transform")

    monkeypatch.setattr(
        sys.modules["docassemble.base.functions"],
        "custom_types",
        {"RegisteredDate": {"class": RegisteredDate, "is_object": True}},
        raising=False,
    )
    assert execution.run(Start()).ok
    before = session_bytes(execution)
    rejected = execution.run(Answer((("custom_date", "invalid"),), validate=False))
    assert not rejected.ok
    assert rejected.error.kind == "validation"
    assert "custom_date" in rejected.error.details["errors"][0]
    assert session_bytes(execution) == before


@pytest.mark.parametrize("registry", [{}, {"RegisteredDate": {"class": object}}])
def test_answer_unregistered_custom_datatype_keeps_plain_value(
    answer_execution, monkeypatch, registry
):
    execution, selected = answer_execution
    selected["interview"] = CustomDateInterview
    monkeypatch.setattr(
        sys.modules["docassemble.base.functions"],
        "custom_types",
        registry,
        raising=False,
    )
    assert execution.run(Start()).ok
    assert execution.run(Answer((), defaults=True)).ok
    assert (
        execution.run(Evaluate("'custom_date' in locals()")).result["value"] == "False"
    )
    assert execution.run(Answer((("custom_date", "2020-01-02"),))).ok
    assert "2020-01-02" in str(execution.run(Evaluate("custom_date")).result)
