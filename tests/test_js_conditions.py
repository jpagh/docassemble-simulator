"""Declared JS conditions through the rendered screen description (#94)."""

import base64
from types import SimpleNamespace

import pytest

from docassemble_simulator.describe import describe_question_result


def screen(
    expression,
    *,
    count=None,
    sign=True,
    triggers=None,
    datatype="integer",
    input_type=None,
    absent=False,
    conditional=False,
    rendered_default=None,
    mode=0,
    disabled=False,
):
    def field(name, number, datatype):
        return SimpleNamespace(
            saveas=base64.b64encode(name.encode()).decode(),
            number=number,
            datatype=datatype,
            required=True,
            extras={},
        )

    controller = field("count", 0, datatype)
    controller.inputtype = input_type
    if absent:
        controller.choicetype = "manual"
    if conditional:
        controller.extras["show_if_js"] = {}
    dependent = field("answer", 1, "yesnoradio")
    dependent.extras["show_if_js"] = {}
    question = SimpleNamespace(question_type="fields", fields=[controller, dependent])
    return describe_question_result(
        {
            "question": question,
            "selectcompute": {0: []} if absent else {},
            "defaults": {0: rendered_default},
            "extras": {
                "disabled": {1: disabled},
                "show_if_js": {
                    1: {
                        "expression": expression,
                        "sign": sign,
                        "mode": mode,
                        "vars": ["count"] if triggers is None else triggers,
                    }
                },
            },
        },
        {} if count is None else {"count": count},
    )


def test_parse_standardformatter_app_js_count_switch_uses_numeric_input_string():
    assert screen('val("count") == 1', count=1)["fields"][1]["visible"] is True
    assert screen('val("count") === 1', count=1)["fields"][1]["visible"] is False
    assert screen('val("count") > 1', count=2)["fields"][1]["visible"] is True
    assert screen('val("count") > 1', count=1)["fields"][1]["visible"] is False


@pytest.mark.parametrize(
    "expression, expected",
    [
        ('val("count") == 0', True),  # unanswered numeric DOM value is ""
        ('val("count") === ""', True),
        ('val("count") === null', False),
        ('val("missing") === null && val("count") == 0', True),
        ('val("missing") === undefined || val("count") != 0', False),
        ('val("missing") == undefined && !val("count")', True),
        ('val("missing") != false && val("count") == false', True),
        ('true || val("count")', True),
        ('false && val("count")', False),
        ('val("count") || "fallback"', True),
        ('val("count") == -0', True),
        ('val("count") == 0 && ("2" < "10" || 2 < 10)', True),
        ('val("count") == 0 && "\U00010000" < "\ue000"', True),
        ('val("count") == 0 && "\U0001f600" === "\\ud83d\\ude00"', True),
        ('val("count") == 0 && "not a number" >= 0', False),
        ('val("count") == 0 && "0x10" == 16', True),
        ('val("count") == 0 && "1_0" == 10', False),
    ],
)
def test_app_js_val_and_ecmascript_primitive_semantics(expression, expected):
    assert screen(expression)["fields"][1]["visible"] is expected


@pytest.mark.parametrize("sign", [True, False])
@pytest.mark.parametrize(
    "expression, triggers",
    [
        ("true", []),
        ("false", []),
        ('val("missing") == null', ["missing"]),
        ('val("count") == 0', []),  # AST recognition is not upstream registration
    ],
)
def test_standardformatter_initial_hidden_wrapper_without_app_js_trigger(
    sign, expression, triggers
):
    field = screen(expression, sign=sign, triggers=triggers)["fields"][1]
    assert field["visible"] is False
    assert field["js_condition_state"] == "no-trigger"


@pytest.mark.parametrize(
    "expression, code",
    [
        ('val("count"); true', "expression"),
        ('val("count") ==', "syntax"),
        ('true || arbitrary(val("count"))', "call"),
        ("false && val(name)", "call"),
        ('val("count") + 1', "syntax-unsupported"),
        ('val("count").length', "syntax-unsupported"),
        ('val("count") && /x/', "literal"),
        ('val("count") && 1n', "literal"),
        ('val("count") && [1]', "syntax-unsupported"),
        ('val("count") && typeof null', "syntax-unsupported"),
        ('val("count")' + " " * 4096, "source-limit"),
        ("!" * 40 + 'val("count")', "ast-limit"),
        ('val("count") && (' + "true && " * 140 + "true)", "ast-limit"),
        ("(" * 600 + 'val("count")' + ")" * 600, "parser-limit"),
    ],
)
def test_unsupported_conditions_are_inspectable_even_in_dead_branches(expression, code):
    field = screen(expression)["fields"][1]
    assert field["visible"] is None
    assert field["js_condition_state"] == "unsupported"
    assert field["js_condition_diagnostic"]["code"] == code
    assert field["visibility_note"]


def test_unsupported_browser_value_is_not_an_absent_control():
    field = screen('val("count") == null', count=object())["fields"][1]
    assert field["visible"] is None
    assert field["js_condition_diagnostic"]["code"] == "value"


@pytest.mark.parametrize(
    "datatype, input_type, value, expected",
    [
        ("boolean", "yesnoradio", None, True),
        ("boolean", "yesnoradio", True, False),
        ("boolean", "yesnoradio", False, False),
        ("boolean", "yesno", None, False),
        ("boolean", "yesno", True, False),
        ("boolean", "noyes", True, False),
    ],
)
def test_app_js_val_unanswered_scalar_control_is_not_undefined(
    datatype, input_type, value, expected
):
    observed = screen(
        'val("count") === null', datatype=datatype, input_type=input_type, count=value
    )
    assert observed["fields"][1]["visible"] is expected


@pytest.mark.parametrize(
    "input_type, value, expected",
    [
        ("yesno", None, False),
        ("yesno", False, False),
        ("yesno", True, True),
        ("noyes", None, False),
        ("noyes", False, True),
        ("noyes", True, False),
        ("yesnoradio", True, True),
        ("yesnoradio", False, False),
    ],
)
def test_app_js_val_checkbox_and_radio_boolean_values(input_type, value, expected):
    observed = screen(
        'val("count") === true', datatype="boolean", input_type=input_type, count=value
    )
    assert observed["fields"][1]["visible"] is expected


def test_empty_choice_control_does_not_bind_even_with_namespace_value():
    observed = screen('val("count") == 1', count=1, absent=True)
    assert observed["fields"][1]["js_condition_state"] == "no-trigger"
    assert observed["fields"][1]["visible"] is False


def test_rendered_controller_binding_is_not_rejected_for_conditional_source_metadata():
    # The ask-time rendered condition, not compiled source metadata alone,
    # determines eligibility. Full chained ask results are covered by #96.
    observed = screen('val("count") == 1', count=1, conditional=True)
    assert observed["fields"][1]["visible"] is True
    assert "js_condition_diagnostic" not in observed["fields"][1]


@pytest.mark.parametrize("listen, expected", [(True, True), (False, False)])
def test_app_js_chain_settles_in_reverse_field_order_without_saved_fact_mutation(
    listen, expected
):
    fields = [
        SimpleNamespace(
            saveas=base64.b64encode(name.encode()).decode(),
            number=number,
            datatype=datatype,
            required=True,
            extras={},
            inputtype=input_type,
        )
        for number, (name, datatype, input_type) in enumerate(
            [
                ("antenna", "text", None),
                ("modulation", "text", None),
                ("listen", "boolean", "yesnoradio"),
            ]
        )
    ]
    ask = {
        "question": SimpleNamespace(question_type="fields", fields=fields),
        "extras": {
            "show_if_js": {
                0: {
                    "expression": 'val("modulation") === "AM"',
                    "vars": ["modulation"],
                    "mode": 0,
                    "sign": True,
                },
                1: {
                    "expression": 'val("listen") === true',
                    "vars": ["listen"],
                    "mode": 0,
                    "sign": True,
                },
            }
        },
    }
    namespace = {"listen": listen, "modulation": "AM"}
    observed = describe_question_result(ask, namespace)
    assert observed["fields"][0]["visible"] is expected
    assert observed["fields"][1]["visible"] is expected
    assert namespace["modulation"] == "AM"  # Hidden does not mean cleared.
    assert "visible" not in ask["extras"]["show_if_js"][0]


def test_browser_string_work_is_bounded():
    observed = screen('val("count") == 1', count="x" * 4097)
    assert observed["fields"][1]["js_condition_diagnostic"]["code"] == "value-limit"


@pytest.mark.parametrize(
    "datatype, value, expected",
    [
        ("float", 1.0, "1.0"),
        ("number", 1.0, "1"),
        ("number", 0.00000000001, "0"),
        ("integer", 1.0, "1"),
    ],
)
def test_standardformatter_input_for_numeric_prefill_string(datatype, value, expected):
    observed = screen(f'val("count") === "{expected}"', datatype=datatype, count=value)
    assert observed["fields"][1]["visible"] is True


@pytest.mark.parametrize(
    "input_type, value, expected",
    [
        ("yesno", "True", True),
        ("yesno", "False", False),
        ("noyes", "False", True),
        ("noyes", "True", False),
    ],
)
def test_standardformatter_checkbox_prefill_tokens(input_type, value, expected):
    observed = screen(
        'val("count") === true', datatype="boolean", input_type=input_type, count=value
    )
    assert observed["fields"][1]["visible"] is expected


def test_arbitrary_rendered_default_does_not_enter_frozen_bindings():
    import json

    observed = screen('val("count") == 1', rendered_default=object())
    assert observed["fields"][1]["js_condition_diagnostic"]["code"] == "binding"
    json.dumps(observed)  # The saved/transported screen contains no host object.


def test_balanced_ast_is_node_bounded_independently_of_depth():
    branch = "true"
    for _ in range(8):
        branch = f"({branch} && {branch})"
    observed = screen('val("count") && ' + branch)
    assert observed["fields"][1]["js_condition_diagnostic"]["code"] == "ast-limit"


@pytest.mark.parametrize("sign", [True, False])
@pytest.mark.parametrize("count", [1, 2])
def test_enablement_preserves_visibility_and_controls_effective_requiredness(
    sign, count
):
    field = screen('val("count") == 1', mode=1, count=count, sign=sign)["fields"][1]
    assert field["visible"] is True
    assert field["enabled"] is ((count == 1) == sign)
    assert field["required"] is field["enabled"]
    assert field["submission_required"] is True


@pytest.mark.parametrize("sign", [True, False])
@pytest.mark.parametrize(
    "expression,triggers",
    [("true", []), ("false", []), ('val("missing")', ["missing"])],
)
def test_enablement_without_trigger_preserves_initial_enabled_input(
    sign, expression, triggers
):
    field = screen(expression, mode=1, sign=sign, triggers=triggers)["fields"][1]
    assert field["visible"] is True
    assert field["enabled"] is True
    assert field["js_condition_state"] == "no-trigger"


@pytest.mark.parametrize("input_type", ["yesnomaybe", "noyesmaybe"])
def test_app_js_nullable_radio_unanswered_is_null(input_type):
    observed = screen(
        'val("count") === null', datatype="boolean", input_type=input_type
    )
    assert observed["fields"][1]["visible"] is True


def test_app_js_absent_indexed_controller_is_null_not_namespace_lookup():
    observed = screen('!val("person.name") && val("count") == 0')
    assert observed["fields"][1]["visible"] is True


@pytest.mark.parametrize(
    "expression,expected,diagnostic",
    [
        ('val("group")', True, None),
        ('!val("group")', False, None),
        ('val("group") !== "MO"', True, None),
        ('val("group") === null', False, None),
        ('val("group") == ""', None, "coercion"),
        ('val("group") < 1', None, "coercion"),
    ],
)
def test_app_js_group_arrays_remain_arrays_not_python_truthiness(
    expression, expected, diagnostic
):
    group = SimpleNamespace(
        saveas=base64.b64encode(b"group").decode(),
        number=0,
        datatype="checkboxes",
        required=False,
        extras={},
    )
    note = SimpleNamespace(number=1, datatype="note", required=False, extras={})
    observed = describe_question_result(
        {
            "question": SimpleNamespace(question_type="fields", fields=[group, note]),
            "selectcompute": {0: [{"key": "Apple", "label": "Apple"}]},
            "extras": {
                "show_if_js": {
                    1: {
                        "expression": expression,
                        "vars": ["group"],
                        "sign": True,
                        "mode": 0,
                    }
                }
            },
        },
        {"group": {}},
    )["fields"][1]
    assert observed["visible"] is expected
    if diagnostic:
        assert observed["js_condition_diagnostic"]["code"] == diagnostic


def test_enabled_is_debug_only_in_trace_but_effective_requiredness_is_compared():
    from docassemble_simulator.trace import screen_content

    baseline = {
        "fields": [
            {"variable": "state", "type": "text", "visible": True, "required": False}
        ]
    }
    optional_disabled = {"fields": [{**baseline["fields"][0], "enabled": False}]}
    optional_enabled = {"fields": [{**baseline["fields"][0], "enabled": True}]}
    assert (
        screen_content(baseline)
        == screen_content(optional_disabled)
        == screen_content(optional_enabled)
    )
    required_enabled = {"fields": [{**optional_enabled["fields"][0], "required": True}]}
    assert screen_content(required_enabled) != screen_content(optional_disabled)


def test_legacy_screen_without_enabled_metadata_retains_submission_eligibility():
    from docassemble_simulator._js_conditions import field_eligible

    assert field_eligible({"visible": True})
    assert not field_eligible({"visible": True, "enabled": False})


@pytest.mark.parametrize("sign", [True, False])
def test_no_trigger_enablement_preserves_authored_disabled_initial_state(sign):
    field = screen("false", triggers=[], mode=1, sign=sign, disabled=True)["fields"][1]
    assert field["visible"] is True and field["enabled"] is False
    assert field["js_condition_state"] == "no-trigger"
