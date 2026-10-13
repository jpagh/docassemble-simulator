"""Turn docassemble question objects into plain dicts for CLI/agent consumption."""

from __future__ import annotations

import ast
import base64
import binascii
import json
import logging
import re
from contextlib import contextmanager
from html import unescape
from typing import Any
from urllib.parse import unquote

logger = logging.getLogger(__name__)


def from_safeid_safe(text: str) -> str:
    """Decode a docassemble safeid (base64) back to a variable name.

    Mirrors docassemble.webapp.utils.helpers.from_safeid without importing the
    Flask-bound webapp package.
    """
    try:
        padded = text + "=" * ((4 - len(text) % 4) % 4)
        return base64.b64decode(padded).decode("utf-8")
    except (
        binascii.Error,
        ValueError,
        UnicodeDecodeError,
        AttributeError,
        TypeError,
    ) as exc:
        logger.debug("safeid decode failed for %r: %s", text, exc)
        return text


def field_variable(field: Any) -> str:
    """Return a field's decoded saved-variable name."""
    return from_safeid_safe(getattr(field, "saveas", "") or "")


def _text_of(obj: Any, user_dict: dict) -> Any:
    """Best-effort render of a TextObject / value in the interview context."""
    if obj is None:
        return None
    try:
        return obj.text(user_dict).rstrip()
    except (
        AttributeError,
        TypeError,
        ValueError,
        RuntimeError,
        KeyError,
        IndexError,
        LookupError,
    ) as exc:
        logger.debug("text() render failed for %r: %s", obj, exc)
    try:
        return str(obj)
    except (TypeError, ValueError, RuntimeError, AttributeError) as exc:
        logger.debug("str() render failed for %r: %s", obj, exc)
        return repr(obj)


def field_visible(field: Any, user_dict: dict) -> tuple[bool | None, str | None]:
    """Evaluate a field's show/hide condition exactly like Question.ask() does.

    Two compiled forms exist:
    - ``showif_code``: a code expression (`show if:` with `code:`).
    - extras['show_if_var'/'show_if_val'/'show_if_sign']: the
      `show if: variable / is: value` form (and its bare-variable form,
      which targets True). sign 0 means "hide if".

    Returns (visible, note). visible=True/False when determinable; None when
    the condition could not be evaluated (undefined references etc.), in
    which case note explains why.
    """
    extras = getattr(field, "extras", {}) or {}
    showif = getattr(field, "showif_code", None)
    if showif is not None:
        sign0 = extras.get("show_if_sign_code") == 0
        try:
            result = bool(eval(showif, user_dict))
        except (
            NameError,
            AttributeError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            SyntaxError,
            RuntimeError,
            ImportError,
            LookupError,
            OSError,
        ) as err:
            return None, f"show-if not evaluable ({type(err).__name__}: {err})"
        # show_if_sign_code == 0 marks a "hide if" condition.
        return ((not result) if sign0 else result), None

    if "show_if_var" in extras:
        hide = extras.get("show_if_sign") == 0
        try:
            actual = eval(from_safeid_safe(extras["show_if_var"]), user_dict)
        except (
            NameError,
            AttributeError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            SyntaxError,
            RuntimeError,
            ImportError,
            LookupError,
            OSError,
        ) as err:
            return None, f"show-if not evaluable ({type(err).__name__}: {err})"
        if "show_if_val" in extras:
            target = _text_of(extras["show_if_val"], user_dict)
            equal = str(actual) == str(target).strip()
        else:
            # Bare `show if: variable` targets True, like docassemble's ask().
            equal = bool(actual)
        return ((not equal) if hide else equal), None

    return True, None


def field_required(field: Any, user_dict: dict) -> bool:
    """Resolve static or computed `required`. Defaults to True like docassemble."""
    required: Any = getattr(field, "required", True)
    if isinstance(required, dict) and "compute" in required:
        try:
            return bool(eval(required["compute"], user_dict))
        except (
            NameError,
            AttributeError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            SyntaxError,
            RuntimeError,
            ImportError,
            LookupError,
            OSError,
        ) as exc:
            logger.debug("required compute eval failed: %s", exc)
            return True
    return bool(required)


def _json_assignment_value(value: Any) -> str:
    try:
        return json.dumps(value, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps(str(value))


def _assignment_example(description: dict) -> str | None:
    variable = description.get("variable")
    if not variable:
        return None
    datatype = str(description.get("type", "text")).lower()
    choices = description.get("choices") or []
    values = [choice.get("value") for choice in choices if "value" in choice]
    if datatype in {"date", "datetime", "datetime-local", "time"}:
        value = "2026-01-02" if datatype == "date" else "2026-01-02T15:04:05"
        return f"{variable}={value}"
    if datatype in {"checkboxes", "multiselect"}:
        selected = {str(value): True for value in values[:1]}
        return f"{variable}={_json_assignment_value(selected)}"
    if datatype == "object_checkboxes":
        selected = {str(value): True for value in values[:1]}
        return f"{variable}={_json_assignment_value(selected)}"
    if datatype == "object_multiselect":
        return f"{variable}={_json_assignment_value(values[:1])}"
    if datatype in {"object", "object_radio"}:
        value = values[0] if values else None
        return f"{variable}={_json_assignment_value(value)}"
    if datatype in {"yesno", "noyes", "boolean"}:
        return f"{variable}=true"
    if datatype == "integer":
        return f"{variable}=0"
    if datatype in {"number", "float", "currency", "range"}:
        return f"{variable}=0.0"
    if datatype in {"threestate", "yesnomaybe", "noyesmaybe"}:
        return f"{variable}=null"
    return f"{variable}=example"


def describe_field(
    field: Any, user_dict: dict, choices: list[dict] | None = None
) -> dict:
    out: dict[str, Any] = {}
    saveas = getattr(field, "saveas", None)
    if saveas is not None:
        out["variable"] = field_variable(field)
    datatype = getattr(field, "datatype", None)
    fieldtype = getattr(field, "fieldtype", None)
    out["type"] = fieldtype or datatype or "text"
    input_type = getattr(field, "inputtype", None)
    if input_type is not None:
        out["input_type"] = input_type
    label = getattr(field, "label", None)
    if label is not None:
        rendered = _text_of(label, user_dict)
        if isinstance(rendered, str):
            rendered = re.sub(r"<[^>]+>", "", rendered).strip()
        out["label"] = rendered

    visible, note = field_visible(field, user_dict)
    out["visible"] = visible
    if note:
        out["visibility_note"] = note
    if visible is False:
        out["required"] = False  # hidden fields cannot be answered
    else:
        out["required"] = field_required(field, user_dict)

    choices = describe_choices(field, user_dict, choices=choices)
    if choices:
        out["choices"] = choices
    default = getattr(field, "default", None)
    if default is not None:
        out["default"] = _text_of(default, user_dict)
    out["example"] = _assignment_example(out)
    if out.get("variable") and out.get("required") is False:
        out["blank_example"] = f"{out['variable']}="
    return out


def describe_choices(
    field: Any, user_dict: dict, *, choices: list[dict] | None = None
) -> list[dict]:
    if choices is None:
        choices = getattr(field, "choices", None)
    if not choices:
        return []
    out = []
    reference = getattr(choices, "instanceName", None)
    for item in choices:
        try:
            if isinstance(item, dict):
                if "compute" in item and "label" not in item and "key" not in item:
                    out.append(
                        {
                            "reference_to": _selection_reference(field)
                            or reference
                            or "<dynamic choices>"
                        }
                    )
                    break
                if "label" in item and "key" in item:
                    out.append(
                        {
                            "value": _choice_value(_text_of(item["key"], user_dict)),
                            "label": _text_of(item["label"], user_dict),
                        }
                    )
                else:
                    for key, val in item.items():
                        out.append(
                            {
                                "value": _choice_value(key),
                                "label": _text_of(val, user_dict),
                            }
                        )
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                out.append(
                    {
                        "value": _choice_value(item[0]),
                        "label": _text_of(item[1], user_dict),
                    }
                )
            elif getattr(item, "instanceName", None):
                out.append(
                    {
                        "value": item.instanceName,
                        "label": _text_of(item, user_dict),
                    }
                )
            elif reference:
                out.append({"reference_to": reference})
                break
            elif isinstance(item, (str, int, float, bool)) or item is None:
                out.append({"value": _choice_value(item), "label": None})
            else:
                out.append({"reference_to": "<dynamic choices>"})
                break
        except (
            ValueError,
            TypeError,
            RuntimeError,
            AttributeError,
            KeyError,
            IndexError,
            LookupError,
            OSError,
            NameError,
            ImportError,
        ) as err:
            logger.debug("choice render failed: %s", err)
            out.append({"value": f"<unrenderable choice: {err}>", "label": None})
    return out


def _selection_reference(field: Any) -> str | None:
    selections = getattr(field, "selections", None)
    source = selections.get("sourcecode") if isinstance(selections, dict) else None
    if not isinstance(source, str):
        return None
    try:
        expression = ast.parse(source, mode="eval").body
        if isinstance(expression, ast.Call) and expression.args:
            return ast.unparse(expression.args[0])
    except (SyntaxError, ValueError):
        pass
    return None


def _choice_value(key: Any) -> Any:
    """Choice keys may be code (quoted strings); unquote when they are literals."""
    if isinstance(key, str):
        stripped = key.strip()
        if (
            len(stripped) >= 2
            and stripped[0] == stripped[-1]
            and stripped[0] in ("'", '"')
        ):
            return stripped[1:-1]
    return key


YESNO_TYPES = {
    "yesno": ["true", "false"],
    "yesnomaybe": ["true", "false", "none"],
    "noyes": ["true", "false"],
}


@contextmanager
def _screen_variable_context(variable: str | None):
    """Expose the sought variable while dynamic field text is evaluated."""
    if not variable:
        yield
        return
    try:
        from docassemble.base.functions import this_thread

        info = getattr(this_thread, "current_info", None)
        old_info_value = info.get("variable") if isinstance(info, dict) else None
        had_info_value = isinstance(info, dict) and "variable" in info
        current_variables = getattr(this_thread, "current_variable", None)
        if isinstance(current_variables, list):
            current_variables.append(variable)
        if isinstance(info, dict):
            info["variable"] = variable
        try:
            yield
        finally:
            if isinstance(current_variables, list) and current_variables:
                current_variables.pop()
            if isinstance(info, dict):
                if had_info_value:
                    info["variable"] = old_info_value
                else:
                    info.pop("variable", None)
    except ImportError:
        yield


def rendered_row_controls(result: dict) -> list[tuple[dict[str, Any], dict]]:
    """Decode rendered browser controls, including table item_actions links."""
    actions = []
    for key in ("question_text", "subquestion_text"):
        text = unescape(str(result.get(key) or ""))
        for encoded in re.findall(r"[?&]action=([^&\s\"'<>\)]+)", text):
            try:
                encoded = unquote(encoded)
                payload = json.loads(
                    base64.b64decode(encoded + "=" * (-len(encoded) % 4))
                )
                kind = {
                    "_da_list_add": "add",
                    "_da_list_remove": "delete",
                    "_da_list_edit": "edit",
                }.get(payload.get("action"))
                arguments = payload.get("arguments", {})
                target = arguments.get("list")
                row = None
                if kind == "edit":
                    follow_up = arguments["items"][0]["follow up"][0]
                    match = re.fullmatch(
                        r"([A-Za-z_]\w*(?:\[\d+\]|\.[A-Za-z_]\w*)*)\[(\d+)\](?:\.[A-Za-z_]\w*(?:\[\d+\]|\.[A-Za-z_]\w*)*)?",
                        follow_up,
                    )
                    if match is None:
                        continue
                    target, row = match.group(1), int(match.group(2))
                    # Completion names the actual list even when the edited
                    # attribute itself contains indexed objects.
                    for item in arguments["items"]:
                        if item.get("action") == "_da_list_ensure_complete":
                            target = item["arguments"]["group"]
                            anchored = re.match(
                                re.escape(target) + r"\[(\d+)\](?:\.|$)", follow_up
                            )
                            if anchored is None:
                                raise ValueError("edit follow-up is outside its list")
                            row = int(anchored.group(1))
                            break
                if (
                    kind is None
                    or not isinstance(target, str)
                    or re.fullmatch(r"[A-Za-z_]\w*(?:\[\d+\]|\.[A-Za-z_]\w*)*", target)
                    is None
                ):
                    continue
                action = {"kind": kind, "target": target}
                if kind == "delete":
                    row = int(arguments["item"])
                    if row < 0 or str(row) != str(arguments["item"]):
                        continue
                    action["row"] = row
                if row is not None:
                    action["row"] = row
                if not any(fact == action for fact, _ in actions):
                    actions.append((action, payload))
            except (
                ValueError,
                TypeError,
                KeyError,
                IndexError,
                AttributeError,
                binascii.Error,
                UnicodeDecodeError,
            ):
                continue
    return actions


def _row_actions(result: dict) -> list[dict[str, Any]]:
    return [fact for fact, _ in rendered_row_controls(result)]


def describe_question_result(result: dict, user_dict: dict) -> dict:
    """Flatten an askfor()/assemble() result dict into a screen description."""
    question = result.get("question")
    qtype = getattr(question, "question_type", None)
    out: dict[str, Any] = {
        "kind": "question",
        "question_type": qtype,
        "question_name": getattr(question, "name", None),
        "has_validation_code": bool(getattr(question, "validation_code", None)),
        "sought": result.get("sought"),
        "orig_sought": result.get("orig_sought"),
        "question_text": result.get("question_text"),
        "subquestion_text": result.get("subquestion_text"),
        "continue_label": result.get("continue_label"),
        "fields": [],
        "row_actions": _row_actions(result),
    }
    if qtype in YESNO_TYPES:
        seen: set[str] = set()
        for field in _raw_fields(question):
            var = field_variable(field)
            if var and var not in seen:
                seen.add(var)
                visible, note = field_visible(field, user_dict)
                entry: dict[str, Any] = {
                    "variable": var,
                    "type": qtype,
                    "values": YESNO_TYPES[qtype],
                    "visible": visible,
                    "required": field_required(field, user_dict),
                }
                entry["example"] = _assignment_example(entry)
                if entry["required"] is False:
                    entry["blank_example"] = f"{var}="
                if note:
                    entry["visibility_note"] = note
                out["fields"].append(entry)
        _append_continue_field(out["fields"], getattr(question, "fields_saveas", None))
        return out
    if qtype == "signature":
        fields = describe_fields(question, user_dict)
        for f in fields:
            f["type"] = "signature"
        out["fields"] = fields
        _append_continue_field(out["fields"], getattr(question, "fields_saveas", None))
        return out
    with _screen_variable_context(result.get("sought") or result.get("orig_sought")):
        out["fields"] = describe_fields(
            question,
            user_dict,
            result.get("selectcompute") or {},
            result.get("extras") or {},
            ask_result=result,
        )
    if qtype == "review":
        _append_continue_field(out["fields"], getattr(question, "review_saveas", None))
    # Unexpanded code fields retain an honest boundary, including the error.
    if qtype == "fields" and (
        not out["fields"]
        or any(field.get("type") == "fields_code" for field in out["fields"])
    ):
        out["dynamic_fields"] = True
        out["field_source"] = "code"
    else:
        out["dynamic_fields"] = False
        out["field_source"] = "compiled"
    _append_continue_field(out["fields"], getattr(question, "fields_saveas", None))
    return out


def _append_continue_field(fields: list[dict[str, Any]], variable: Any) -> None:
    if (
        isinstance(variable, str)
        and variable
        and not any(field.get("variable") == variable for field in fields)
    ):
        fields.append(_continue_field(variable))


def _continue_field(variable: str) -> dict[str, Any]:
    """The Continue button as an answerable field.

    docassemble compiles ``continue button field`` into
    ``Question.fields_saveas`` (or ``review_saveas`` for reviews). The formatter
    posts it as ``True`` via a named button or a hidden input, rather than a
    compiled ``Field``. Model that value so whole-screen ``answer`` and
    ``--defaults`` press Continue; ``--partial`` only applies explicit input.
    """
    return {
        "variable": variable,
        "type": "settrue",
        "visible": True,
        "required": False,
        "example": f"{variable}=True",
    }


def _raw_fields(question: Any) -> list[Any]:
    return list(getattr(question, "fields", None) or [])


def describe_fields(
    question: Any,
    user_dict: dict,
    selectcompute: dict | None = None,
    extras: dict | None = None,
    *,
    ask_result: dict | None = None,
) -> list[dict]:
    """Return the single live field-facts view used by screen callers."""
    fields = []
    selectcompute = selectcompute or {}
    ask_result = ask_result or {}
    extras = extras or ask_result.get("extras") or {}
    live_fields = []
    for field in _raw_fields(question):
        if "fields_code" not in (getattr(field, "extras", {}) or {}):
            live_fields.append((field, False, None))
            continue
        try:
            # Prefer the exact controls ask() rendered, without evaluating
            # authored code a second time (it may depend on mutable state).
            sub_fields = (extras.get("sub_fields") or {}).get(field.number)
            if sub_fields is None:
                generated = question.get_question_for_field_with_sub_fields(
                    field, user_dict
                )
                sub_fields = _raw_fields(generated)
                for sub_field in sub_fields:
                    sub_field.number = f"{field.number}_{sub_field.number}"
            if not isinstance(sub_fields, list) or not sub_fields:
                raise ValueError("fields code produced no usable fields")
            live_fields.extend((sub_field, True, None) for sub_field in sub_fields)
        except Exception as err:  # noqa: BLE001 - authored expansion can raise any exception
            live_fields.append(
                (
                    field,
                    False,
                    f"could not expand fields code: {type(err).__name__}: {err}",
                )
            )
    for field, expanded, expansion_error in live_fields:
        try:
            choices = selectcompute.get(getattr(field, "number", None))
            if choices is None:
                choices = selectcompute.get(str(getattr(field, "number", "")))
            described = describe_field(field, user_dict, choices=choices)
            if expansion_error:
                described["error"] = expansion_error
            if expanded:
                number = getattr(field, "number", None)
                if number in (extras.get("ok") or {}):
                    described["visible"] = bool(extras["ok"][number])
                    described.pop("visibility_note", None)
                if number in (extras.get("required") or {}):
                    described["required"] = bool(extras["required"][number])
                if described["visible"] is False:
                    described["required"] = False
                if number in (ask_result.get("defaults") or {}):
                    described["default"] = ask_result["defaults"][number]
                if number in (ask_result.get("labels") or {}):
                    described["label"] = re.sub(
                        r"<[^>]+>", "", ask_result["labels"][number]
                    ).strip()
                described["example"] = _assignment_example(described)
                described.pop("blank_example", None)
                if described.get("variable") and described["required"] is False:
                    described["blank_example"] = f"{described['variable']}="

            number = getattr(field, "number", None)
            constraints = {}
            for key in (
                "min",
                "max",
                "minlength",
                "maxlength",
                "step",
                "scale",
                "currency symbol",
                "field metadata",
            ):
                values = (extras or {}).get(key) or {}
                if number in values:
                    value = values[number]
                    constraints[key] = (
                        value
                        if isinstance(value, (str, int, float, bool, dict, list))
                        or value is None
                        else str(value)
                    )
            if constraints:
                described["constraints"] = constraints
        except (
            ValueError,
            TypeError,
            RuntimeError,
            AttributeError,
            KeyError,
            IndexError,
            LookupError,
            OSError,
            NameError,
            ImportError,
            SyntaxError,
        ) as err:
            logger.debug("field describe failed: %s", err)
            described = {"error": f"could not describe field: {err}"}
        fields.append(described)
    return fields


__all__ = [
    "describe_choices",
    "describe_field",
    "describe_fields",
    "describe_question_result",
    "describe_seeking",
    "field_required",
    "field_variable",
    "field_visible",
    "from_safeid_safe",
]


def describe_seeking(seeking: list, limit: int = 40) -> list[dict]:
    """Summarize the interview_status.seeking debug chain."""
    out = []
    for stage in seeking[-limit:]:
        entry: dict[str, Any] = {}
        if "variable" in stage:
            entry["variable"] = stage["variable"]
        if "question" in stage:
            q = stage["question"]
            name = getattr(q, "name", None)
            if name:
                entry["question"] = name
        if "reason" in stage:
            entry["reason"] = stage["reason"]
        if entry:
            out.append(entry)
    return out
