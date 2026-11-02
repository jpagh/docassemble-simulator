"""Bounded declared JS expressions, not a JavaScript runtime.

Only frozen rendered scalar bindings reach the evaluator. In particular, an
absent DOM control is null; it is never a lookup in the interview namespace.
"""

from __future__ import annotations

import ast
import locale
import math
import re
from functools import lru_cache

import esprima

MAX_SOURCE = 4096
MAX_NODES = 256
MAX_DEPTH = 32
_UNDEFINED = object()
_COMPARISONS = {"==", "!=", "===", "!==", "<", "<=", ">", ">="}


class UnsupportedCondition(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def diagnostic(self):
        return {
            "kind": "js-condition-unsupported",
            "code": self.code,
            "message": str(self),
        }


@lru_cache(maxsize=128)
def _parse(source: str):
    if len(source) > MAX_SOURCE:
        raise UnsupportedCondition(
            "source-limit", f"JS condition exceeds {MAX_SOURCE} characters"
        )
    try:
        program = esprima.parseScript(source, tolerant=False).toDict()
    except RecursionError as error:
        raise UnsupportedCondition(
            "parser-limit", "JS condition exceeds parser recursion limit"
        ) from error
    except (esprima.Error, ValueError, OverflowError) as error:
        raise UnsupportedCondition(
            "syntax", f"Invalid JS condition: {error}"
        ) from error
    body = program.get("body", [])
    if len(body) != 1 or body[0]["type"] != "ExpressionStatement":
        raise UnsupportedCondition(
            "expression", "JS condition must contain exactly one expression"
        )
    expression = body[0]["expression"]
    pending = [(expression, 1)]
    dependencies = set()
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise UnsupportedCondition(
                "ast-limit", "JS condition exceeds AST node/depth limit"
            )
        kind = node["type"]
        children = []
        if kind == "Literal":
            if (
                "regex" in node
                or "bigint" in node
                or node.get("raw", "").endswith("n")
                or type(node.get("value")) not in (str, int, float, bool, type(None))
            ):
                raise UnsupportedCondition(
                    "literal", "Only primitive JS literals are supported"
                )
        elif kind == "Identifier" and node["name"] == "undefined":
            pass
        elif kind == "CallExpression":
            args = node.get("arguments", [])
            if (
                node["callee"].get("type") != "Identifier"
                or node["callee"].get("name") != "val"
                or len(args) != 1
                or args[0].get("type") != "Literal"
                or type(args[0].get("value")) is not str
                or node.get("optional")
            ):
                raise UnsupportedCondition(
                    "call", 'Only val("literal input name") calls are supported'
                )
            count += 2  # callee and literal argument are also expression AST nodes
            if count > MAX_NODES or depth + 1 > MAX_DEPTH:
                raise UnsupportedCondition(
                    "ast-limit", "JS condition exceeds AST node/depth limit"
                )
            dependencies.add(args[0]["value"])
        elif kind == "UnaryExpression" and node["operator"] in ("!", "+", "-"):
            # Signs are permitted only as part of numeric literals, not arithmetic.
            if node["operator"] != "!" and not (
                node["argument"]["type"] == "Literal"
                and type(node["argument"].get("value")) in (int, float)
            ):
                raise UnsupportedCondition(
                    "operator", "Unary signs require a numeric literal"
                )
            children = [node["argument"]]
        elif kind in ("LogicalExpression", "BinaryExpression") and node["operator"] in (
            _COMPARISONS | {"&&", "||"}
        ):
            children = [node["left"], node["right"]]
        else:
            raise UnsupportedCondition(
                "syntax-unsupported", f"Unsupported JS syntax: {kind}"
            )
        pending.extend((child, depth + 1) for child in children)
    return expression, tuple(sorted(dependencies))


def _truth(value):
    if value is _UNDEFINED or value is None:
        return False
    if type(value) is bool:
        return value
    if type(value) in (int, float):
        return value != 0 and not math.isnan(value)
    if type(value) is list:
        return True  # Even an empty JS array is truthy.
    return len(value) != 0


def _number(value):
    if type(value) is list:
        raise UnsupportedCondition("coercion", "Array coercion is not supported")
    if value is _UNDEFINED:
        return math.nan
    if value is None:
        return 0.0
    if type(value) is bool:
        return 1.0 if value else 0.0
    if type(value) in (int, float):
        return float(value)
    # ECMAScript StringNumericLiteral, not Python's broader float syntax.
    text = value.strip(
        " \t\r\n\v\f\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
    )
    if not text:
        return 0.0
    if re.fullmatch(r"0(?:[xX][0-9a-fA-F]+|[bB][01]+|[oO][0-7]+)", text):
        return float(int(text, 0))
    if re.fullmatch(
        r"[+-]?(?:Infinity|(?:(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?))", text
    ):
        return float(text)
    return math.nan


def _primitive_type(value):
    if value is _UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) in (int, float):
        return "number"
    if type(value) is list:
        return "array"
    return "string"


def _equal(left, right, strict):
    lt, rt = _primitive_type(left), _primitive_type(right)
    if "array" in (lt, rt):
        if strict:
            return left is right
        raise UnsupportedCondition(
            "coercion", "Array equality coercion is not supported"
        )
    if lt == rt:
        if lt == "string":
            return left.encode("utf-16-be", "surrogatepass") == right.encode(
                "utf-16-be", "surrogatepass"
            )
        return left == right
    if strict:
        return False
    if {lt, rt} == {"null", "undefined"}:
        return True
    if lt == "boolean":
        return _equal(_number(left), right, False)
    if rt == "boolean":
        return _equal(left, _number(right), False)
    if {lt, rt} == {"number", "string"}:
        return _number(left) == _number(right)
    return False


def _evaluate(node, values, budget):
    budget[0] -= 1
    if budget[0] < 0:
        raise UnsupportedCondition(
            "work-limit", "JS condition exceeds evaluation budget"
        )
    kind = node["type"]
    if kind == "Literal":
        value = node.get("value")
        return float(value) if type(value) in (int, float) else value
    if kind == "Identifier":
        return _UNDEFINED
    if kind == "CallExpression":
        value = values[node["arguments"][0]["value"]]
        # Upstream val() allocates a new selected-values array on every call.
        return list(value) if type(value) is list else value
    op = node["operator"]
    if kind == "UnaryExpression":
        value = _evaluate(node["argument"], values, budget)
        return not _truth(value) if op == "!" else (-value if op == "-" else value)
    left = _evaluate(node["left"], values, budget)
    if op == "&&" and not _truth(left) or op == "||" and _truth(left):
        return left
    right = _evaluate(node["right"], values, budget)
    if op in ("&&", "||"):
        return right
    if op in ("==", "!=", "===", "!=="):
        equal = _equal(left, right, op in ("===", "!=="))
        return not equal if op in ("!=", "!==") else equal
    if type(left) is str and type(right) is str:
        # JS compares UTF-16 code units, not Unicode code points.
        left, right = (
            left.encode("utf-16-be", "surrogatepass"),
            right.encode("utf-16-be", "surrogatepass"),
        )
    else:
        left, right = _number(left), _number(right)
    return {
        "<": lambda: left < right,
        "<=": lambda: left <= right,
        ">": lambda: left > right,
        ">=": lambda: left >= right,
    }[op]()


_SCALAR_TEXT = {
    "text",
    "area",
    "email",
    "url",
    "password",
    "integer",
    "number",
    "float",
    "currency",
}
_RADIO = {"yesnoradio", "noyesradio", "yesnomaybe", "noyesmaybe", "threestate"}
_CHECKBOX = {"yesno", "noyes", "boolean"}


def rendered_target(name: str, namespace: dict) -> str:
    """Resolve only a rendered field path; never evaluate a val() argument."""

    def resolve(node):
        if isinstance(node, ast.Name):
            if node.id in {"x", "y", "z"}:
                instance = getattr(namespace.get(node.id), "instanceName", None)
                if isinstance(instance, str):
                    return instance
            return node.id
        if isinstance(node, ast.Attribute) and not node.attr.startswith("_"):
            return resolve(node.value) + "." + node.attr
        if isinstance(node, ast.Subscript):
            index = node.slice
            if isinstance(index, ast.Name):
                value = namespace.get(index.id)
            elif isinstance(index, ast.Constant):
                value = index.value
            else:
                raise TypeError("nonliteral index")
            if type(value) not in (str, int):
                raise ValueError("unresolved index")
            return resolve(node.value) + "[" + repr(value) + "]"
        raise ValueError("not a field path")

    try:
        return resolve(ast.parse(name, mode="eval").body)
    except (SyntaxError, TypeError, ValueError, RecursionError) as error:
        raise UnsupportedCondition(
            "binding", f"Unsupported rendered field path: {name}"
        ) from error


def _binding(field: dict):
    name = field.get("variable", "")
    target = rendered_target(field.get("browser_target", name), {})
    datatype = str(field.get("type", "text")).lower()
    input_type = field.get("input_type")
    # A choice list alone uses standardformatter's default <select>, not radio.
    if datatype in {"checkboxes", "multiselect"}:
        kind = "group"
    elif datatype in _CHECKBOX and input_type in (None, "yesno", "noyes"):
        kind = "checkbox"
    elif datatype in _RADIO or input_type in _RADIO | {"radio"}:
        if datatype.startswith("object"):
            raise UnsupportedCondition(
                "binding", f"Object control is not supported: {name}"
            )
        kind = "radio"
    elif datatype in _SCALAR_TEXT or input_type in ("dropdown", "combobox"):
        kind = "text"
    else:
        raise UnsupportedCondition(
            "binding", f"Unsupported scalar control: {name} ({datatype})"
        )
    default = field.get("default")
    if kind == "group":
        default = getattr(default, "elements", default)
        if default is not None and (
            type(default) not in (list, dict)
            or len(default) > MAX_NODES
            or any(type(item) not in (str, int, float, bool) for item in default)
            or isinstance(default, dict)
            and any(type(item) is not bool for item in default.values())
        ):
            raise UnsupportedCondition(
                "binding", f"Unsupported grouped default for {name}"
            )
    if kind != "group" and type(default) not in (str, int, float, bool, type(None)):
        raise UnsupportedCondition("binding", f"Non-scalar rendered default for {name}")
    return {
        "target": target,
        "disabled": field.get("browser_disabled", False),
        "variable": name,
        "choices": [
            str(c["value"]) for c in field.get("choices") or [] if "value" in c
        ],
        "option": field.get("browser_option"),
        "nullable": datatype in {"threestate", "yesnomaybe", "noyesmaybe"}
        or input_type in {"yesnomaybe", "noyesmaybe"}
        or any(
            "value" in choice and choice["value"] is None
            for choice in field.get("choices") or []
        ),
        "kind": kind,
        "default": default,
        "default_set": "default" in field,
        "datatype": input_type or datatype,
        "step": (field.get("constraints") or {}).get("step"),
    }


def capture_condition(info: dict, fields: list[dict]):
    """Freeze ask-time source, upstream triggers and rendered scalar addresses."""
    captured = {key: info[key] for key in ("expression", "vars", "sign", "mode")}
    captured["vars"] = list(captured["vars"])
    try:
        _, dependencies = _parse(captured["expression"])
        captured["dependencies"] = list(dependencies)
        rendered = {
            field["variable"]: field
            for field in fields
            if field.get("variable")
            and field.get("rendered_input") is not False
            and field.get("type") not in ("note", "html", "settrue", "fields_code")
        }
        captured["bindings"] = {}
        for name in dependencies:
            if name in rendered:
                captured["bindings"][name] = _binding(rendered[name])
                continue
            # Option addresses are aliases only for choices actually rendered.
            try:
                address = ast.parse(name, mode="eval").body
                if not isinstance(address, ast.Subscript) or not isinstance(
                    address.slice, ast.Constant
                ):
                    continue
                parent = ast.unparse(address.value)
                field = rendered.get(parent)
                option = address.slice.value
                if (
                    field
                    and field.get("type") in {"checkboxes", "multiselect"}
                    and any(
                        str(c.get("value")) == str(option)
                        for c in field.get("choices") or []
                    )
                ):
                    captured["bindings"][name] = _binding(
                        {**field, "browser_option": str(option)}
                    )
            except (SyntaxError, ValueError, RecursionError):
                continue
        if captured["mode"] not in (0, 1):
            raise UnsupportedCondition("mode", "Unknown JS condition mode")
    except UnsupportedCondition as error:
        captured["diagnostic"] = error.diagnostic()
    return captured


def _browser_value(binding, namespace, input_values):
    # Only frozen rendered targets reach this lookup, never dependency source.
    target = binding["target"]
    try:
        value = eval(target, namespace)
        missing = False
    except (NameError, AttributeError, KeyError, IndexError):
        value = binding["default"]
        missing = True
    except (
        TypeError,
        ValueError,
        RuntimeError,
        ImportError,
        LookupError,
        OSError,
    ) as error:
        raise UnsupportedCondition(
            "value", f"Cannot read rendered browser value for {target}"
        ) from error
    submitted = input_values.get(
        target, input_values.get(binding.get("variable"), _UNDEFINED)
    )
    if submitted is not _UNDEFINED:
        value = submitted
        missing = False
    kind = binding["kind"]
    if kind == "group":
        elements = getattr(value, "elements", value)
        if isinstance(elements, (dict, list, tuple)) and len(elements) > MAX_NODES:
            raise UnsupportedCondition(
                "value-limit", "Grouped browser value exceeds bounds"
            )
        if value is None:
            selected = []
        elif isinstance(value, dict) or isinstance(
            getattr(value, "elements", None), dict
        ):
            elements = value if isinstance(value, dict) else value.elements
            selected = [
                choice for choice in binding["choices"] if elements.get(choice) is True
            ]
        elif isinstance(value, (list, tuple)) or isinstance(
            getattr(value, "elements", None), list
        ):
            elements = value if isinstance(value, (list, tuple)) else value.elements
            if any(type(item) not in (str, int, float, bool) for item in elements):
                raise UnsupportedCondition("value", "Unsupported grouped browser value")
            selected = [
                choice
                for choice in binding["choices"]
                if choice in [str(item) for item in elements]
            ]
        else:
            raise UnsupportedCondition("value", "Unsupported grouped browser value")
        if len(selected) > MAX_NODES or any(
            len(item) > MAX_SOURCE for item in selected
        ):
            raise UnsupportedCondition(
                "value-limit", "Grouped browser value exceeds bounds"
            )
        return (
            binding["option"] in selected
            if binding.get("option") is not None
            else selected
        )
    if type(value) not in (str, int, float, bool, type(None)):
        raise UnsupportedCondition(
            "value", f"Non-scalar browser value for {binding['target']}"
        )
    if type(value) is str and len(value) > MAX_SOURCE:
        raise UnsupportedCondition(
            "value-limit", f"Browser value exceeds {MAX_SOURCE} characters"
        )
    if kind == "checkbox":
        # standardformatter.input_for converts primitive defaults to strings,
        # then recognizes these false tokens before applying the checkbox sign.
        if value is None or value == "":
            return False
        token = str(value)
        false_token = token in {
            "False",
            "false",
            "FALSE",
            "no",
            "No",
            "NO",
            "Off",
            "off",
            "OFF",
            "Null",
            "null",
            "NULL",
        }
        return false_token if binding["datatype"] == "noyes" else not false_token
    if value is None:
        if submitted is None:
            return None if kind == "radio" else ""
        if kind == "radio" and binding.get("nullable") and not missing:
            return "None"
        if (
            kind == "text"
            and "None" in binding["choices"]
            and (not missing or binding.get("default_set", False))
        ):
            # input_for selects the "None" option only for an established null.
            return "None"
        return None if kind == "radio" else ""
    if type(value) is bool:
        return value if kind == "radio" else ("True" if value else "False")
    if kind == "text" and submitted is _UNDEFINED and value != "":
        datatype = binding["datatype"]
        if datatype in {"integer", "number", "currency"}:
            # Match standardformatter.input_for / locale_format_string, rather
            # than JavaScript Number.toString: these are server-rendered inputs.
            try:
                numeric = float(value)
                if not math.isfinite(numeric):
                    raise ValueError("non-finite number")
                step = binding.get("step")
                if (
                    datatype == "integer"
                    or step is not None
                    and float(step).is_integer()
                ):
                    return str(int(numeric))
                return re.sub(
                    r"[^0-9]$",
                    "",
                    locale.format_string("%.10f", numeric, grouping=False).rstrip("0"),
                )
            except (ValueError, TypeError) as error:
                raise UnsupportedCondition(
                    "value",
                    f"Unsupported numeric browser value for {binding['target']}",
                ) from error
    if type(value) in (int, float):
        if not math.isfinite(value):
            raise UnsupportedCondition("value", "Non-finite browser input value")
        return str(value)
    if kind == "radio" and value in ("True", "False"):
        return value == "True"
    return value


def condition_decision(
    captured: dict,
    namespace: dict,
    *,
    input_values: dict | None = None,
    eligible: dict | None = None,
) -> tuple[bool | None, str, dict | None]:
    """Return the mode-specific decision, trigger state and capability diagnostic."""
    if captured.get("diagnostic"):
        return None, "unsupported", captured["diagnostic"]
    try:
        expression, dependencies = _parse(captured["expression"])
        bindings = captured["bindings"]
        if not set(dependencies).intersection(captured["vars"], bindings):
            # Sign inversion only follows an actual triggered evaluation.
            initial = (
                captured.get("initial_enabled", True)
                if captured["mode"] == 1
                else False
            )
            return initial, "no-trigger", None
        values = {
            name: _browser_value(bindings[name], namespace, input_values or {})
            if name in bindings
            and (
                eligible.get(bindings[name]["variable"], True)
                if eligible is not None
                else not bindings[name].get("disabled")
            )
            else None
            for name in dependencies
        }
        result = _truth(_evaluate(expression, values, [MAX_NODES]))
        return (result if captured["sign"] else not result), "evaluated", None
    except UnsupportedCondition as error:
        return None, "unsupported", error.diagnostic()
    except (OverflowError, RecursionError):
        diagnostic = UnsupportedCondition(
            "value-limit", "JS numeric/value resource limit exceeded"
        )
        return None, "unsupported", diagnostic.diagnostic()


def settle_condition_fields(fields: list[dict], namespace: dict, *, input_values=None):
    """Settle visibility and enablement, not a general browser event loop.

    Begin at upstream initial states, never stale presentation decisions.
    Ineligible controls still register triggers, but getField/val reads null.
    Field order breaks ties deterministically; contradictory conditions reject.
    """
    fields = [dict(field) for field in fields]
    conditional = [field for field in fields if "js_condition" in field]
    if not conditional:
        return fields

    def decision_key(field):
        return "enabled" if field["js_condition"]["mode"] == 1 else "visible"

    for field in conditional:
        key = decision_key(field)
        field[key] = (
            field["js_condition"].get("initial_enabled", True)
            if key == "enabled"
            else False
        )
    # getField() also requires a conditional wrapper's isVisible marker.
    # Mode-1 wrappers remain enabled without a trigger, but never set that marker.
    lookup_available = {
        field.get("variable"): field_eligible(field) and "js_condition" not in field
        for field in fields
    }

    def settled_state():
        return tuple(
            (
                field.get(decision_key(field)),
                lookup_available.get(field.get("variable")),
            )
            for field in conditional
        )

    seen = set()
    for _round in range(2 * len(fields) + 1):
        before = settled_state()
        if before in seen:
            break
        seen.add(before)
        for field in conditional:
            decision, state, diagnostic = condition_decision(
                field["js_condition"],
                namespace,
                input_values=input_values,
                eligible=lookup_available,
            )
            field[decision_key(field)] = decision
            if field["js_condition"]["mode"] == 0:
                field["enabled"] = decision
            field["js_condition_state"] = state
            field["required"] = (
                False
                if not field_eligible(field)
                else field.get("submission_required", True)
            )
            if diagnostic:
                field["js_condition_diagnostic"] = diagnostic
                field["visibility_note"] = diagnostic["message"]
            else:
                field.pop("js_condition_diagnostic", None)
                field.pop("visibility_note", None)
            lookup_available[field.get("variable")] = (
                field_eligible(field) and state == "evaluated"
            )
        if before == settled_state():
            return fields
    names = [field.get("variable") or "<note>" for field in conditional]
    diagnostic = UnsupportedCondition(
        "nonconvergence",
        "JS visibility/enablement did not converge for "
        + ", ".join(names)
        + "; review contradictory show/hide or enable/disable conditions or refresh after editing the interview",
    ).diagnostic()
    for field in conditional:
        field[decision_key(field)] = None
        field.update(
            js_condition_state="unsupported",
            js_condition_diagnostic=diagnostic,
            visibility_note=diagnostic["message"],
        )
    return fields


def field_eligible(field: dict) -> bool:
    """Missing legacy enablement metadata retains the previous eligible state."""
    return (
        field.get("visible") is not False
        and field.get("enabled") is not False
        and field.get("rendered_input") is not False
    )
