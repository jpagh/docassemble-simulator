"""Turn docassemble question objects into plain dicts for CLI/agent consumption."""
from __future__ import annotations

import ast
import base64
import re
from typing import Any


def from_safeid_safe(text: str) -> str:
    """Decode a docassemble safeid (base64) back to a variable name.

    Mirrors docassemble.webapp.utils.helpers.from_safeid without importing the
    Flask-bound webapp package.
    """
    try:
        padded = text + "=" * ((4 - len(text) % 4) % 4)
        return base64.b64decode(padded).decode("utf-8")
    except Exception:
        return text


def _text_of(obj: Any, user_dict: dict) -> Any:
    """Best-effort render of a TextObject / value in the interview context."""
    if obj is None:
        return None
    try:
        return obj.text(user_dict).rstrip()
    except Exception:
        pass
    try:
        return str(obj)
    except Exception:
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
        except Exception as err:
            return None, f"show-if not evaluable ({type(err).__name__}: {err})"
        # show_if_sign_code == 0 marks a "hide if" condition.
        return ((not result) if sign0 else result), None

    if "show_if_var" in extras:
        hide = extras.get("show_if_sign") == 0
        try:
            actual = eval(from_safeid_safe(extras["show_if_var"]), user_dict)
        except Exception as err:
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
        except Exception:
            return True
    return bool(required)


def describe_field(field: Any, user_dict: dict) -> dict:
    out: dict[str, Any] = {}
    saveas = getattr(field, "saveas", None)
    if saveas is not None:
        out["variable"] = from_safeid_safe(saveas)
    datatype = getattr(field, "datatype", None)
    fieldtype = getattr(field, "fieldtype", None)
    out["type"] = fieldtype or datatype or "text"
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

    choices = describe_choices(field, user_dict)
    if choices:
        out["choices"] = choices
    default = getattr(field, "default", None)
    if default is not None:
        out["default"] = _text_of(default, user_dict)
    return out


def describe_choices(field: Any, user_dict: dict) -> list[dict]:
    choices = getattr(field, "choices", None)
    if not choices:
        return []
    out = []
    reference = getattr(choices, "instanceName", None)
    for item in choices:
        try:
            if isinstance(item, dict):
                if "compute" in item and "label" not in item and "key" not in item:
                    out.append({"reference_to": _selection_reference(field) or reference or "<dynamic choices>"})
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
                            {"value": _choice_value(key), "label": _text_of(val, user_dict)}
                        )
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                out.append({"value": _choice_value(item[0]), "label": _text_of(item[1], user_dict)})
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
        except Exception as err:
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
        if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ("'", '"'):
            return stripped[1:-1]
    return key


YESNO_TYPES = {
    "yesno": ["true", "false"],
    "yesnomaybe": ["true", "false", "none"],
    "noyes": ["true", "false"],
}


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
    }
    if qtype in YESNO_TYPES:
        seen: set[str] = set()
        for field in _raw_fields(question):
            var = from_safeid_safe(getattr(field, "saveas", "") or "")
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
                if note:
                    entry["visibility_note"] = note
                out["fields"].append(entry)
        return out
    if qtype == "signature":
        fields = _describe_all_fields(question, user_dict)
        for f in fields:
            f["type"] = "signature"
        out["fields"] = fields
        return out
    out["fields"] = _describe_all_fields(question, user_dict)
    return out


def _raw_fields(question: Any) -> list[Any]:
    return list(getattr(question, "fields", None) or [])


def _describe_all_fields(question: Any, user_dict: dict) -> list[dict]:
    fields = []
    for field in _raw_fields(question):
        try:
            described = describe_field(field, user_dict)
        except Exception as err:
            described = {"error": f"could not describe field: {err}"}
        fields.append(described)
    return fields


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
