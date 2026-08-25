import sys
import types
from types import SimpleNamespace

from docassemble_simulator.describe import (
    describe_choices,
    describe_question_result,
    field_visible,
)
from docassemble_simulator.session import parse_value


def safeid(name: str) -> str:
    import base64

    return base64.b64encode(name.encode()).decode()


class TestParseValue:
    def test_json_booleans_null_numbers(self):
        assert parse_value("true") is True
        assert parse_value("false") is False
        assert parse_value("null") is None
        assert parse_value("42") == 42
        assert parse_value("-1.5") == -1.5

    def test_json_collections(self):
        assert parse_value("[1, 2]") == [1, 2]
        assert parse_value('{"a": 1}') == {"a": 1}

    def test_python_literal_fallback(self):
        # single quotes are invalid JSON but valid Python literals
        assert parse_value("'hello world'") == "hello world"
        assert parse_value("(1, 2)") == (1, 2)

    def test_plain_string(self):
        assert parse_value("Alice") == "Alice"

    def test_non_literal_ast_result_returns_raw(self):
        # Ellipsis parses as an AST literal but is not an allowed type
        assert parse_value("...") == "..."


def _field(*, extras=None, **kw):
    return SimpleNamespace(extras=extras or {}, **kw)


class TestDescribeChoices:
    class TextObject:
        def __init__(self, value):
            self.value = value

        def text(self, user_dict):
            return self.value

    def test_multiple_choice_button_dict_uses_key_as_value(self):
        field = _field(
            choices=[
                {
                    "label": self.TextObject("Continue"),
                    "key": self.TextObject("there_is_another"),
                }
            ]
        )

        assert describe_choices(field, {}) == [
            {"value": "there_is_another", "label": "Continue"}
        ]

    def test_object_choice_uses_instance_name(self):
        class Choice:
            instanceName = "M.options[0]"

            def __str__(self):
                return "First option"

        field = _field(choices=[Choice()])

        assert describe_choices(field, {}) == [
            {"value": "M.options[0]", "label": "First option"}
        ]

    def test_reference_list_falls_back_to_reference_to(self):
        class Choices(list):
            instanceName = "visitation_time_options"

        field = _field(choices=Choices([object()]))

        assert describe_choices(field, {}) == [
            {"reference_to": "visitation_time_options"}
        ]

    def test_dynamic_selection_reports_source_reference(self):
        field = _field(
            choices=[{"compute": compile("visitation_time_options", "<test>", "eval")}],
            selections={
                "sourcecode": "docassemble_base_util_selections(visitation_time_options)"
            },
        )

        assert describe_choices(field, {}) == [
            {"reference_to": "visitation_time_options"}
        ]

    def test_dynamic_button_uses_sought_variable(self, monkeypatch):
        thread = SimpleNamespace(current_info={}, current_variable=[])
        functions = types.ModuleType("docassemble.base.functions")
        functions.this_thread = thread
        monkeypatch.setitem(sys.modules, "docassemble.base.functions", functions)

        class DynamicText:
            def __init__(self, value):
                self.value = value

            def text(self, _user_dict):
                if self.value == "variable-tail":
                    return thread.current_info["variable"].split(".")[-1]
                return self.value

        field = _field(
            number=1,
            saveas=safeid("x.button"),
            choices=[
                {
                    "key": DynamicText("variable-tail"),
                    "label": DynamicText("Continue"),
                }
            ],
        )
        question = SimpleNamespace(
            question_type="question",
            name="gather",
            fields=[field],
            validation_code=None,
        )

        result = describe_question_result(
            {
                "question": question,
                "question_text": "Gather",
                "subquestion_text": None,
                "continue_label": None,
                "sought": "M.attorneys.there_is_another",
                "orig_sought": "M.attorneys.there_is_another",
            },
            {},
        )

        assert result["fields"][0]["choices"] == [
            {"value": "there_is_another", "label": "Continue"}
        ]
        assert thread.current_info == {}
        assert thread.current_variable == []

    def test_object_field_uses_live_selection_keys(self):
        field = _field(
            number=3,
            saveas=safeid("M.attorneys"),
            datatype="object_checkboxes",
            choices=[],
        )
        question = SimpleNamespace(
            question_type="question",
            name="attorneys",
            fields=[field],
            validation_code=None,
        )

        result = describe_question_result(
            {
                "question": question,
                "question_text": "Pick attorneys",
                "subquestion_text": None,
                "continue_label": None,
                "sought": "M.attorneys",
                "orig_sought": "M.attorneys",
                "selectcompute": {
                    3: [{"key": safeid("firmdata.attorneys[0]"), "label": "Alice"}]
                },
            },
            {},
        )

        assert result["fields"][0]["choices"] == [
            {"value": safeid("firmdata.attorneys[0]"), "label": "Alice"}
        ]


class TestFieldVisibleCodeForm:
    def test_show_if_code_true(self):
        f = _field(showif_code="x > 1")
        visible, note = field_visible(f, {"x": 2})
        assert visible is True and note is None

    def test_show_if_code_false(self):
        f = _field(showif_code="x > 1")
        visible, note = field_visible(f, {"x": 0})
        assert visible is False and note is None

    def test_hide_if_code_sign_zero_inverts(self):
        f = _field(showif_code="x > 1", extras={"show_if_sign_code": 0})
        visible, _ = field_visible(f, {"x": 2})
        assert visible is False

    def test_unevaluable_code_returns_none_with_note(self):
        f = _field(showif_code="missing_var > 1")
        visible, note = field_visible(f, {})
        assert visible is None
        assert "not evaluable" in note


class TestFieldVisibleVarValForm:
    def test_equal_target_visible(self):
        f = _field(extras={"show_if_var": safeid("custody_type"), "show_if_val": "sole"})
        visible, note = field_visible(f, {"custody_type": "sole"})
        assert (visible, note) == (True, None)

    def test_different_target_hidden(self):
        f = _field(extras={"show_if_var": safeid("custody_type"), "show_if_val": "sole"})
        visible, _ = field_visible(f, {"custody_type": "joint"})
        assert visible is False

    def test_hide_if_sign_inverts(self):
        f = _field(
            extras={
                "show_if_var": safeid("custody_type"),
                "show_if_val": "sole",
                "show_if_sign": 0,
            }
        )
        visible, _ = field_visible(f, {"custody_type": "sole"})
        assert visible is False


class TestFieldVisibleBareVariableForm:
    def test_truthy_variable_visible(self):
        f = _field(extras={"show_if_var": safeid("wants_guardian")})
        visible, note = field_visible(f, {"wants_guardian": True})
        assert (visible, note) == (True, None)

    def test_falsy_variable_hidden(self):
        f = _field(extras={"show_if_var": safeid("wants_guardian")})
        visible, note = field_visible(f, {"wants_guardian": False})
        assert (visible, note) == (False, None)

    def test_bare_variable_hide_if_sign(self):
        f = _field(extras={"show_if_var": safeid("wants_guardian"), "show_if_sign": 0})
        visible, _ = field_visible(f, {"wants_guardian": False})
        assert visible is True

    def test_bare_variable_undefined_returns_none_with_note(self):
        f = _field(extras={"show_if_var": safeid("wants_guardian")})
        visible, note = field_visible(f, {})
        assert visible is None
        assert "not evaluable" in note

    def test_no_condition_defaults_visible(self):
        visible, note = field_visible(_field(), {})
        assert (visible, note) == (True, None)
