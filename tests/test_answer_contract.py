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


class NestedEditInterview(ScreenInterview):
    def assemble(self, namespace, interview_status):
        from test_row_actions import action_link

        if interview_status.current_info.get("action"):
            namespace["pressed_arguments"] = interview_status.current_info["arguments"]
            namespace["pressed"] = interview_status.current_info["action"]
            if namespace.get("fail_action"):
                raise ValueError("authored edit failed")
        super().assemble(namespace, interview_status)
        interview_status.subquestion_text = action_link(
            "_da_list_edit",
            items=[
                {"follow up": ["children[0].grandchildren[0].name"]},
                {
                    "action": "_da_list_ensure_complete",
                    "arguments": {"group": "children[0].grandchildren"},
                },
            ],
        )


def test_nested_edit_replays_rendered_control_transactionally(answer_execution):
    from docassemble_simulator.execution import Execute, RowAction, Status

    execution, selected = answer_execution
    selected["interview"] = NestedEditInterview
    started = execution.run(Start())
    assert started.result["row_actions"] == [
        {"kind": "edit", "target": "children[0].grandchildren", "row": 0}
    ]
    assert execution.run(RowAction("edit", "children[0].grandchildren", 0)).ok
    assert execution.run(Evaluate("pressed")).result["value"] == "'_da_list_edit'"
    assert execution.run(Evaluate("pressed_arguments['items']")).result["value"] == (
        "[{'follow up': ['children[0].grandchildren[0].name']}, "
        "{'action': '_da_list_ensure_complete', 'arguments': {'group': 'children[0].grandchildren'}}]"
    )
    assert execution.run(Execute("fail_action = True")).ok
    saved = execution.run(Status()).result
    before = session_bytes(execution)
    assert not execution.run(RowAction("edit", "children[0].grandchildren", 0)).ok
    assert session_bytes(execution) == before
    assert execution.run(Status()).result == saved


class ForcedEditInterview(ScreenInterview):
    questions_by_name: ClassVar = {}
    fields = (SimpleNamespace(saveas="name", datatype="text", required=True),)

    def assemble(self, namespace, interview_status):
        from test_row_actions import action_link

        if interview_status.current_info.get("action"):
            namespace["_internal"]["event_stack"] = {
                "dasimulator": [{"action": "name", "arguments": {}}]
            }
        super().assemble(namespace, interview_status)
        pending = namespace["_internal"].get("event_stack", {}).get("dasimulator", [])
        interview_status.orig_sought = "name" if pending else None
        interview_status.question_text = "Edit row" if pending else "Review rows"
        interview_status.subquestion_text = action_link(
            "_da_list_edit", items=[{"follow up": ["people[0].name"]}]
        )


def test_forced_edit_advances_only_after_successful_answer(
    answer_execution, monkeypatch
):
    from docassemble.base.functions import this_thread

    from docassemble_simulator.execution import RowAction, Status

    def native_pop(variable):
        namespace = this_thread.current_dict
        stack = namespace["_internal"]["event_stack"]["dasimulator"]
        if stack and stack[0]["action"] == variable:
            stack.pop(0)

    monkeypatch.setattr(
        sys.modules["docassemble.base.functions"],
        "pop_event_stack",
        native_pop,
        raising=False,
    )
    execution, selected = answer_execution
    selected["interview"] = ForcedEditInterview
    assert execution.run(Start()).ok
    assert execution.run(RowAction("edit", "people", 0)).ok
    before = session_bytes(execution)
    assert not execution.run(Answer((("name", ""),))).ok
    assert session_bytes(execution) == before
    assert (
        execution.run(
            Evaluate("_internal['event_stack']['dasimulator'][0]['action']")
        ).result["value"]
        == "'name'"
    )
    assert execution.run(Status()).result["question_text"] == "Edit row"
    answered = execution.run(Answer((("name", "Ada"),)))
    assert answered.ok
    assert answered.result["question_text"] == "Review rows"
    assert (
        execution.run(Evaluate("_internal['event_stack']['dasimulator']")).result[
            "value"
        ]
        == "[]"
    )


class RowActionInterview(ScreenInterview):
    def assemble(self, namespace, interview_status):
        from test_row_actions import action_link

        action = interview_status.current_info.get("action")
        if action:
            namespace["pressed"] = action
            if namespace.get("fail_action"):
                raise ValueError("authored action failed")
        super().assemble(namespace, interview_status)
        interview_status.subquestion_text = action_link("_da_list_add", list="people")


def test_row_action_commits_only_successful_post_action_screen(answer_execution):
    from docassemble_simulator.execution import Execute, RowAction, Status

    execution, selected = answer_execution
    selected["interview"] = RowActionInterview
    assert execution.run(Start()).ok
    rejected = execution.run(RowAction("delete", "people", 0))
    assert rejected.error.kind == "answer-input"
    added = execution.run(RowAction("add", "people"))
    assert added.ok
    assert execution.run(Evaluate("pressed")).result["value"] == "'_da_list_add'"
    assert execution.run(Execute("fail_action = True")).ok
    before = session_bytes(execution)
    saved = execution.run(Status()).result
    failed = execution.run(RowAction("add", "people"))
    assert not failed.ok
    assert failed.error.kind == "execution"
    assert "authored action failed" in failed.error.message
    assert session_bytes(execution) == before
    assert execution.run(Status()).result == saved


def test_screen_action_dispatches_only_offered_controls_transactionally(
    answer_execution,
):
    from docassemble_simulator.execution import Execute, ScreenAction, Status

    execution, selected = answer_execution
    selected["interview"] = RowActionInterview
    started = execution.run(Start())
    action = started.result["actions"][0]
    before = session_bytes(execution)
    assert not execution.run(ScreenAction("unavailable")).ok
    assert session_bytes(execution) == before
    assert execution.run(ScreenAction(action["id"])).ok
    assert execution.run(Evaluate("pressed")).result["value"] == "'_da_list_add'"
    assert execution.run(Execute("fail_action = True")).ok
    saved = execution.run(Status()).result
    before = session_bytes(execution)
    failed = execution.run(ScreenAction(saved["actions"][0]["id"]))
    assert not failed.ok
    assert "authored action failed" in failed.error.message
    assert session_bytes(execution) == before
    assert execution.run(Status()).result == saved


def test_screen_action_rejects_offered_javascript_without_saving(answer_execution):
    from docassemble_simulator.execution import ScreenAction

    class JavascriptInterview(ScreenInterview):
        def assemble(self, namespace, interview_status):
            super().assemble(namespace, interview_status)
            interview_status.subquestion_text = '<a href="javascript:skip()">Skip</a>'

    execution, selected = answer_execution
    selected["interview"] = JavascriptInterview
    started = execution.run(Start())
    before = session_bytes(execution)
    rejected = execution.run(ScreenAction(started.result["actions"][0]["id"]))
    assert not rejected.ok
    assert rejected.error.kind == "answer-input"
    assert "JavaScript" in rejected.error.message
    assert session_bytes(execution) == before


class ShowIfChainInterview(ScreenInterview):
    fields = (
        SimpleNamespace(saveas="a", datatype="boolean", required=True),
        SimpleNamespace(saveas="b", datatype="boolean", required=True, showif_code="a"),
        SimpleNamespace(
            saveas="c", datatype="text", required=False, showif_code="a and b"
        ),
    )
    questions_by_name: ClassVar = {"answer-contract": SimpleNamespace(fields=fields)}


def test_answer_hides_show_if_chain_without_defining_dependents(answer_execution):
    execution, selected = answer_execution
    selected["interview"] = ShowIfChainInterview
    assert execution.run(Start()).ok

    answered = execution.run(Answer((("a", "False"),)))

    assert answered.ok, answered.error
    assert (
        execution.run(Evaluate("'b' in globals() or 'c' in globals()")).result["value"]
        == "False"
    )


def test_answer_still_requires_visible_show_if_dependent(answer_execution):
    execution, selected = answer_execution
    selected["interview"] = ShowIfChainInterview
    assert execution.run(Start()).ok
    before = session_bytes(execution)

    rejected = execution.run(Answer((("a", "True"),)))

    assert not rejected.ok
    assert rejected.error.kind == "validation"
    assert rejected.error.details["missing_required"] == ["b"]
    assert session_bytes(execution) == before


def test_early_rejection_excludes_newly_hidden_required_fields(answer_execution):
    execution, selected = answer_execution
    selected["interview"] = ShowIfChainInterview
    assert execution.run(Start()).ok
    before = session_bytes(execution)

    rejected = execution.run(Answer((("a", "False"), ("typo", "1"))))

    assert not rejected.ok
    assert rejected.error.kind == "answer-input"
    assert rejected.error.details["missing_required"] == []
    assert session_bytes(execution) == before


class DefaultShowIfChainInterview(ShowIfChainInterview):
    fields = (
        SimpleNamespace(
            saveas="a", datatype="boolean", required=False, default="False"
        ),
        *ShowIfChainInterview.fields[1:],
    )
    questions_by_name: ClassVar = {"answer-contract": SimpleNamespace(fields=fields)}


def test_answer_default_hides_show_if_chain_before_blanking(answer_execution):
    execution, selected = answer_execution
    selected["interview"] = DefaultShowIfChainInterview
    assert execution.run(Start()).ok

    answered = execution.run(Answer((), defaults=True))

    assert answered.ok, answered.error
    assert execution.run(Evaluate("a")).result["value"] == "False"
    assert (
        execution.run(Evaluate("'b' in globals() or 'c' in globals()")).result["value"]
        == "False"
    )


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


class FieldsCodeInterview(ScreenInterview):
    questions_by_name: ClassVar = {}

    fields = (
        SimpleNamespace(
            number=0, datatype="fields_code", extras={"fields_code": "address_fields()"}
        ),
    )

    def assemble(self, namespace, interview_status):
        super().assemble(namespace, interview_status)
        interview_status.extras = {
            "sub_fields": {
                0: [
                    SimpleNamespace(
                        number="0_0", saveas="address", datatype="text", required=True
                    ),
                    SimpleNamespace(
                        number="0_1", saveas="unit", datatype="text", required=True
                    ),
                ]
            },
            "ok": {"0_0": True, "0_1": False},
            "required": {"0_0": False, "0_1": True},
        }


def test_fields_code_screen_describes_and_accepts_ask_time_sub_fields(answer_execution):
    execution, selected = answer_execution
    selected["interview"] = FieldsCodeInterview
    started = execution.run(Start())
    assert started.ok
    fields = started.result["fields"]
    assert [(f["variable"], f["visible"], f["required"]) for f in fields] == [
        ("address", True, False),
        ("unit", False, False),
    ]
    answered = execution.run(Answer((("address", "123 Main St"),)))
    assert answered.ok
    assert execution.run(Evaluate("address")).result["value"] == "'123 Main St'"


def unavailable_address_fields(field, namespace):
    raise RuntimeError("authored address expansion failed")


class UnavailableFieldsCodeInterview(ScreenInterview):
    fields = FieldsCodeInterview.fields

    def assemble(self, namespace, interview_status):
        super().assemble(namespace, interview_status)
        interview_status.question.get_question_for_field_with_sub_fields = (
            unavailable_address_fields
        )


def test_fields_code_screen_retains_boundary_and_authored_expansion_error(
    answer_execution,
):
    execution, selected = answer_execution
    selected["interview"] = UnavailableFieldsCodeInterview
    started = execution.run(Start())
    assert started.ok
    assert started.result["dynamic_fields"] is True
    assert started.result["field_source"] == "code"
    (placeholder,) = started.result["fields"]
    assert placeholder["type"] == "fields_code"
    assert "variable" not in placeholder
    assert placeholder["error"] == (
        "could not expand fields code: RuntimeError: authored address expansion failed"
    )


class ConstrainedInterview(ScreenInterview):
    questions_by_name: ClassVar = {}
    fields = (
        SimpleNamespace(saveas="amount", datatype="number", number=0),
        SimpleNamespace(saveas="name", datatype="text", number=1),
    )

    def assemble(self, namespace, interview_status):
        super().assemble(namespace, interview_status)
        interview_status.extras = {
            "min": {0: 2},
            "max": {0: 10},
            "step": {0: 2},
            "minlength": {1: 2},
            "maxlength": {1: 4},
        }


def test_answer_reports_rendered_constraint_facts(answer_execution):
    execution, selected = answer_execution
    selected["interview"] = ConstrainedInterview
    started = execution.run(Start())
    assert started.ok
    assert started.result["fields"][0]["constraints"] == {
        "min": 2,
        "max": 10,
        "step": 2,
    }


@pytest.mark.parametrize(
    "amount,name,constraint",
    [
        ("1", "Ada", "min"),
        ("11", "Ada", "max"),
        ("2", "A", "minlength"),
        ("2", "Alice", "maxlength"),
        ("3", "Ada", "step"),
        ("2", "😀😀😀", "maxlength"),
    ],
)
@pytest.mark.parametrize("validate", [True, False])
def test_answer_rejects_browser_constraints_transactionally(
    answer_execution, amount, name, constraint, validate
):
    execution, selected = answer_execution
    selected["interview"] = ConstrainedInterview
    assert execution.run(Start()).ok
    before = session_bytes(execution)
    rejected = execution.run(
        Answer((("amount", amount), ("name", name)), validate=validate)
    )
    assert not rejected.ok
    assert rejected.error.kind == "validation"
    variable = "name" if "length" in constraint else "amount"
    assert any(
        variable in error and constraint in error
        for error in rejected.error.details["errors"]
    )
    assert session_bytes(execution) == before


@pytest.mark.parametrize(
    "amount,name,partial",
    [("2", "AB", False), ("10", "ABCD", False), ("11", "Alice", True)],
)
def test_answer_constraint_boundaries_and_partial_escape(
    answer_execution, amount, name, partial
):
    execution, selected = answer_execution
    selected["interview"] = ConstrainedInterview
    assert execution.run(Start()).ok
    accepted = execution.run(
        Answer((("amount", amount), ("name", name)), partial=partial)
    )
    assert accepted.ok, accepted.error


def test_answer_does_not_enforce_hidden_rendered_constraints(answer_execution):
    class HiddenConstraintInterview(ConstrainedInterview):
        fields = (
            SimpleNamespace(
                saveas="amount", datatype="number", number=0, showif_code="False"
            ),
            ConstrainedInterview.fields[1],
        )

    execution, selected = answer_execution
    selected["interview"] = HiddenConstraintInterview
    assert execution.run(Start()).ok
    assert execution.run(Answer((("amount", "100"), ("name", "AB")))).ok


@pytest.mark.parametrize("validate", [True, False])
@pytest.mark.parametrize("defaults", [True, False])
def test_constraints_use_post_submission_visibility(
    answer_execution, validate, defaults
):
    class SubmissionConstraintInterview(ScreenInterview):
        fields = (
            SimpleNamespace(
                saveas="show_amount",
                datatype="boolean",
                number=0,
                required=True,
                default="False",
            ),
            SimpleNamespace(
                saveas="amount",
                datatype="number",
                number=1,
                required=True,
                showif_code="show_amount",
            ),
        )
        questions_by_name: ClassVar = {
            "answer-contract": SimpleNamespace(fields=fields)
        }

        def assemble(self, namespace, interview_status):
            super().assemble(namespace, interview_status)
            interview_status.extras = {"min": {1: 2}}

    execution, selected = answer_execution
    selected["interview"] = SubmissionConstraintInterview
    assert execution.run(Start()).ok
    # An already assigned invalid value must not constrain a newly hidden field.
    assignments = (
        (("amount", "1"),) if defaults else (("show_amount", "False"), ("amount", "1"))
    )
    accepted = execution.run(Answer(assignments, defaults=defaults, validate=validate))
    assert accepted.ok, accepted.error


def test_constraints_still_apply_to_newly_visible_fields(answer_execution):
    class SubmissionConstraintInterview(ConstrainedInterview):
        fields = (
            SimpleNamespace(
                saveas="amount",
                datatype="number",
                number=0,
                showif_code="name == 'AB'",
            ),
            ConstrainedInterview.fields[1],
        )
        questions_by_name: ClassVar = {
            "answer-contract": SimpleNamespace(fields=fields)
        }

    execution, selected = answer_execution
    selected["interview"] = SubmissionConstraintInterview
    assert execution.run(Start()).ok
    before = session_bytes(execution)
    rejected = execution.run(Answer((("amount", "1"), ("name", "AB")), validate=False))
    assert not rejected.ok
    assert any("min" in error for error in rejected.error.details["errors"])
    assert session_bytes(execution) == before


class GeneratedVisibilityInterview(FieldsCodeInterview):
    def assemble(self, namespace, interview_status):
        super().assemble(namespace, interview_status)
        interview_status.extras = {
            "sub_fields": {
                0: [
                    SimpleNamespace(
                        number="0_0", saveas="show", datatype="boolean", required=True
                    ),
                    SimpleNamespace(
                        number="0_1",
                        saveas="dependent",
                        datatype="text",
                        required=True,
                        showif_code=compile("show", "<show if>", "eval"),
                    ),
                    SimpleNamespace(
                        number="0_2",
                        saveas="amount",
                        datatype="integer",
                        required=False,
                        showif_code=compile("show", "<show if>", "eval"),
                    ),
                ]
            },
            "min": {"0_2": 10},
        }


@pytest.mark.parametrize("marker", ["different-interpreter", None])
def test_answer_rejects_incompatible_captured_visibility(answer_execution, marker):
    from docassemble_simulator.execution import StateStore

    execution, selected = answer_execution
    selected["interview"] = GeneratedVisibilityInterview
    assert execution.run(Start()).ok
    store = StateStore(execution.scope.root, INTERVIEW)
    payload = store.load()
    visibility = payload["outcome"]["fields"][1]["visibility"]
    if marker is None:
        visibility.pop("cache_tag", None)
    else:
        visibility["cache_tag"] = marker
    # Invalid bytes prove the guard runs before unmarshalling.
    visibility["code"] = "bm90IG1hcnNoYWxsZWQgY29kZQ=="
    store.save(payload["namespace"], payload["outcome"])
    before = session_bytes(execution)

    answered = execution.run(Answer((("show", "False"),)))

    assert not answered.ok
    assert answered.error.kind == "state"
    assert "captured visibility" in answered.error.message
    assert "interpreter" in answered.error.message
    assert "refresh" in answered.error.message
    assert "start" in answered.error.message
    assert session_bytes(execution) == before


def test_generated_fields_follow_submission_visibility(answer_execution):
    execution, selected = answer_execution
    selected["interview"] = GeneratedVisibilityInterview
    assert execution.run(Start()).ok
    answered = execution.run(Answer((("show", "False"), ("amount", "1"))))
    assert answered.ok, answered.error
    assert (
        execution.run(Evaluate("'dependent' in globals()")).result["value"] == "False"
    )


@pytest.mark.parametrize(
    "initial, submitted, explicit, expected",
    [
        (True, False, False, "False"),
        (False, True, False, "'prefilled'"),
        (True, False, True, "'explicit'"),
    ],
)
def test_rendered_defaults_use_submitted_visibility(
    answer_execution, initial, submitted, explicit, expected
):
    class DefaultDependentInterview(ScreenInterview):
        fields = (
            SimpleNamespace(saveas="show", datatype="boolean", required=True),
            SimpleNamespace(
                saveas="dependent",
                datatype="text",
                required=True,
                default="prefilled",
                showif_code="show",
            ),
        )
        questions_by_name: ClassVar = {
            "answer-contract": SimpleNamespace(fields=fields)
        }

        def assemble(self, namespace, interview_status):
            namespace.setdefault("show", initial)
            super().assemble(namespace, interview_status)

    execution, selected = answer_execution
    selected["interview"] = DefaultDependentInterview
    assert execution.run(Start()).ok
    assignments = [("show", str(submitted))]
    if explicit:
        assignments.append(("dependent", "explicit"))
    answered = execution.run(Answer(tuple(assignments)))
    assert answered.ok, answered.error
    expression = "dependent" if submitted or explicit else "'dependent' in globals()"
    assert execution.run(Evaluate(expression)).result["value"] == expected


def test_default_reveals_further_rendered_default(answer_execution):
    class DefaultChainInterview(ScreenInterview):
        fields = (
            SimpleNamespace(
                saveas="dependent",
                datatype="text",
                required=True,
                default="prefilled",
                showif_code="show",
            ),
            SimpleNamespace(
                saveas="show", datatype="boolean", required=False, default="True"
            ),
        )
        questions_by_name: ClassVar = {
            "answer-contract": SimpleNamespace(fields=fields)
        }

    execution, selected = answer_execution
    selected["interview"] = DefaultChainInterview
    assert execution.run(Start()).ok
    answered = execution.run(Answer((), defaults=True))
    assert answered.ok, answered.error
    assert execution.run(Evaluate("dependent")).result["value"] == "'prefilled'"


def test_custom_datatype_receives_rendered_constraints_and_parameters(
    answer_execution, monkeypatch
):
    class CustomParametersInterview(CustomDateInterview):
        fields = (
            SimpleNamespace(
                number=0,
                saveas="custom_date",
                datatype="RegisteredDate",
                required=False,
                extras={"custom_parameters": {"prefix": "configured"}},
            ),
        )

        def assemble(self, namespace, interview_status):
            super().assemble(namespace, interview_status)
            interview_status.extras = {
                "min": {0: 7},
                "custom_parameters_code": {0: {"computed": 8}},
                "custom_parameters_mako": {0: {"rendered": "nine"}},
            }

    class Hook:
        @staticmethod
        def call_validate(raw, variable, data):
            return int(raw) >= data["min"]

        @staticmethod
        def call_transform(raw, variable, data):
            return f"{data['prefix']}:{data['computed']}:{data['rendered']}:{raw}"

    monkeypatch.setattr(
        sys.modules["docassemble.base.functions"],
        "custom_types",
        {"RegisteredDate": {"class": Hook}},
        raising=False,
    )
    execution, selected = answer_execution
    selected["interview"] = CustomParametersInterview
    assert execution.run(Start()).ok
    answered = execution.run(Answer((("custom_date", "7"),)))
    assert answered.ok, answered.error
    assert (
        execution.run(Evaluate("custom_date")).result["value"]
        == "'configured:8:nine:7'"
    )
    before = session_bytes(execution)
    assert not execution.run(Answer((("custom_date", "6"),))).ok
    assert session_bytes(execution) == before


def test_explicit_custom_datatype_blank_uses_transform_not_empty(
    answer_execution, monkeypatch
):
    class Hook:
        @staticmethod
        def empty():
            pytest.fail("explicit text input is not a webapp _empties entry")

        @staticmethod
        def call_validate(raw, variable, data):
            assert raw == ""
            return True

        @staticmethod
        def call_transform(raw, variable, data):
            assert raw == ""
            return "transformed blank"

    monkeypatch.setattr(
        sys.modules["docassemble.base.functions"],
        "custom_types",
        {"RegisteredDate": {"class": Hook, "skip_if_empty": True, "is_object": False}},
        raising=False,
    )
    execution, selected = answer_execution
    selected["interview"] = CustomDateInterview
    assert execution.run(Start()).ok
    assert execution.run(Answer((("custom_date", ""),))).ok
    assert (
        execution.run(Evaluate("custom_date")).result["value"] == "'transformed blank'"
    )


@pytest.mark.parametrize(
    "datatype, default, expected",
    [
        ("integer", 7, "7"),
        ("float", 7.5, "7.5"),
        ("boolean", True, "True"),
        ("text", "prefilled", "'prefilled'"),
    ],
)
def test_generated_required_scalar_default_applies(
    answer_execution, datatype, default, expected
):
    class GeneratedDefaultInterview(FieldsCodeInterview):
        def assemble(self, namespace, interview_status):
            super().assemble(namespace, interview_status)
            interview_status.extras = {
                "sub_fields": {
                    0: [
                        SimpleNamespace(
                            number="0_0",
                            saveas="value",
                            datatype=datatype,
                            required=True,
                        )
                    ]
                }
            }
            interview_status.defaults = {"0_0": default}

    execution, selected = answer_execution
    selected["interview"] = GeneratedDefaultInterview
    assert execution.run(Start()).ok
    answered = execution.run(Answer((), defaults=True))
    assert answered.ok, answered.error
    assert execution.run(Evaluate("value")).result["value"] == expected


@pytest.mark.parametrize(
    "datatype, value, minimum, maximum",
    [
        ("time", "9:00 am", "08:00", "11:00"),
        ("datetime", "January 2, 2026 9:00 am", "2026-01-01T08:00", "2026-01-03T11:00"),
    ],
)
def test_text_time_controls_do_not_compare_bounds_lexically(
    answer_execution, datatype, value, minimum, maximum
):
    class TimeInterview(ScreenInterview):
        questions_by_name: ClassVar = {}
        fields = (
            SimpleNamespace(number=0, saveas="value", datatype=datatype, required=True),
        )

        def assemble(self, namespace, interview_status):
            super().assemble(namespace, interview_status)
            interview_status.extras = {"min": {0: minimum}, "max": {0: maximum}}

    execution, selected = answer_execution
    selected["interview"] = TimeInterview
    assert execution.run(Start()).ok
    answered = execution.run(Answer((("value", value),)))
    assert answered.ok, answered.error


def test_failed_post_edit_assembly_preserves_saved_stack(answer_execution, monkeypatch):
    from docassemble.base.functions import this_thread

    from docassemble_simulator.execution import RowAction, Status

    class FailingEditInterview(ForcedEditInterview):
        def assemble(self, namespace, interview_status):
            if namespace.get("name") == "Ada":
                raise ValueError("post-edit assembly failed")
            super().assemble(namespace, interview_status)

    def native_pop(variable):
        stack = this_thread.current_dict["_internal"]["event_stack"]["dasimulator"]
        if stack and stack[0]["action"] == variable:
            stack.pop(0)

    monkeypatch.setattr(
        sys.modules["docassemble.base.functions"],
        "pop_event_stack",
        native_pop,
        raising=False,
    )
    execution, selected = answer_execution
    selected["interview"] = FailingEditInterview
    assert execution.run(Start()).ok
    assert execution.run(RowAction("edit", "people", 0)).ok
    before = session_bytes(execution)
    saved = execution.run(Status()).result
    failed = execution.run(Answer((("name", "Ada"),)))
    assert not failed.ok
    assert "post-edit assembly failed" in failed.error.message
    assert session_bytes(execution) == before
    assert execution.run(Status()).result == saved
    assert (
        execution.run(Evaluate("_internal['event_stack']['dasimulator']")).result[
            "value"
        ]
        == "[{'action': 'name', 'arguments': {}}]"
    )


def test_undefined_controller_cascade_is_hidden_at_submission(answer_execution):
    class RequiredChainInterview(ShowIfChainInterview):
        fields = (
            ShowIfChainInterview.fields[0],
            ShowIfChainInterview.fields[1],
            SimpleNamespace(
                saveas="c", datatype="text", required=True, showif_code="b"
            ),
        )
        questions_by_name: ClassVar = {
            "answer-contract": SimpleNamespace(fields=fields)
        }

    execution, selected = answer_execution
    selected["interview"] = RequiredChainInterview
    started = execution.run(Start())
    assert started.ok
    assert started.result["fields"][2]["visible"] is None
    rejected = execution.run(Answer((("a", "True"),)))
    assert not rejected.ok
    assert rejected.error.details["missing_required"] == ["b"]


def test_rendered_numeric_constraints_have_one_shape(answer_execution):
    class StringConstraintsInterview(ScreenInterview):
        questions_by_name: ClassVar = {}
        fields = (
            SimpleNamespace(
                number=0, saveas="amount", datatype="number", required=True
            ),
        )

        def assemble(self, namespace, interview_status):
            super().assemble(namespace, interview_status)
            interview_status.extras = {
                key: {0: value}
                for key, value in {
                    "min": "2",
                    "max": "10.5",
                    "step": "0.5",
                    "minlength": "2",
                    "maxlength": "4",
                    "scale": "2",
                    "currency symbol": "$",
                    "field metadata": {"key": "value"},
                }.items()
            }

    execution, selected = answer_execution
    selected["interview"] = StringConstraintsInterview
    started = execution.run(Start())
    assert started.ok
    assert started.result["fields"][0]["constraints"] == {
        "min": 2,
        "max": 10.5,
        "step": 0.5,
        "minlength": 2,
        "maxlength": 4,
        "scale": "2",
        "currency symbol": "$",
        "field metadata": {"key": "value"},
    }


def test_default_that_hides_an_earlier_default_leaves_it_undefined(answer_execution):
    class HidingDefaultInterview(ScreenInterview):
        fields = (
            SimpleNamespace(
                saveas="dependent",
                datatype="text",
                required=True,
                default="prefilled",
                showif_code="globals().get('show', True)",
            ),
            SimpleNamespace(
                saveas="show", datatype="boolean", required=False, default="False"
            ),
        )
        questions_by_name: ClassVar = {
            "answer-contract": SimpleNamespace(fields=fields)
        }

    execution, selected = answer_execution
    selected["interview"] = HidingDefaultInterview
    assert execution.run(Start()).ok
    assert execution.run(Answer((), defaults=True)).ok
    assert (
        execution.run(Evaluate("'dependent' in globals()")).result["value"] == "False"
    )


def test_default_visibility_cycle_rejects_without_saving(answer_execution):
    class CyclicDefaultInterview(ScreenInterview):
        fields = (
            SimpleNamespace(
                saveas="value",
                datatype="boolean",
                required=False,
                default="False",
                showif_code="globals().get('value', True)",
            ),
        )
        questions_by_name: ClassVar = {
            "answer-contract": SimpleNamespace(fields=fields)
        }

    execution, selected = answer_execution
    selected["interview"] = CyclicDefaultInterview
    assert execution.run(Start()).ok
    before = session_bytes(execution)
    rejected = execution.run(Answer((), defaults=True))
    assert not rejected.ok
    assert rejected.error.kind == "validation"
    assert "unstable visibility" in rejected.error.message
    assert session_bytes(execution) == before
