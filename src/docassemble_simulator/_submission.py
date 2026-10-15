"""Screen-submission acceptance: one ordered gate sequence for every caller.

An ordinary ``Answer`` and an automatic seek/replay both submit a screen the
way a browser form POST does.  The acceptance sequence they share is defined
once here: assignment authorization and application, settled visibility and
rendered defaults, browser blanks, the rendered required gate, rendered
constraints, and authored validation code.  Execution's operation methods keep
only what differs between them: advancement, rollback, reporting, and
persistence policy.  This is a package-private seam, not an execution
operation or an exported interface (ADR-0002).
"""

from __future__ import annotations

import datetime
import difflib
import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from docassemble_simulator._outcomes import (
    ErrorKind,
    ExecutionFailure,
    first_line,
)
from docassemble_simulator.describe import (
    CapturedVisibilityError,
    described_field_visible,
    field_variable,
    field_visible,
)

logger = logging.getLogger(__name__)

_MISSING = object()


@dataclass(frozen=True)
class SubmissionAccepted:
    """A screen the shared sequence accepted, with its per-assignment facts."""

    dispositions: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssignmentsRejected:
    """A rejected submission whose assignment gate stopped it.

    ``validation_error`` marks an assignment that failed docassemble's own
    per-field validation hook rather than being unknown or unparseable.
    """

    errors: tuple[str, ...]
    dispositions: tuple[dict[str, Any], ...] = ()
    validation_error: bool = False


@dataclass(frozen=True)
class RequiredRejected:
    """A rejected submission whose rendered required gate stopped it."""

    errors: tuple[str, ...]
    dispositions: tuple[dict[str, Any], ...] = ()
    missing_required: tuple[str, ...] = ()
    active_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationRejected:
    """A rejected submission whose rendered-constraint or authored-validation
    gate stopped it; both gates are reported identically."""

    errors: tuple[str, ...]
    dispositions: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()


SubmissionVerdict = (
    SubmissionAccepted | AssignmentsRejected | RequiredRejected | ValidationRejected
)


def accept_screen_submission(
    interview,
    namespace: dict[str, Any],
    screen: dict[str, Any],
    *,
    assignments: tuple[tuple[str, str], ...] = (),
    use_code: bool = False,
    blank_submissions: set[str] | frozenset[str] = frozenset(),
    partial: bool = False,
    validate: bool = True,
    strict: bool = False,
) -> SubmissionVerdict:
    """Run the acceptance sequence one browser-shaped submission must pass.

    Assignments are authorized against the screen and applied first.  Unless
    the caller asked for a partial submission, omitted rendered defaults settle
    against the post-submission visibility (the browser posts its prefilled
    values) and every remaining visible optional field takes the blank the
    browser would post.  The rendered required gate, the rendered constraints,
    and authored validation code then run in that order, each skipping the ones
    after it.  ``blank_submissions`` names the variables whose submitted value
    is an explicit empty, so the required gate can reject what a browser would
    never send.  Authored validation runs unless ``validate`` is false, and
    ``strict`` promotes its warnings to errors.

    The caller owns the transaction: a validation gate is reported as data,
    and the attempted mutations stay in the working state for the caller to
    roll back or keep.  A hard failure is still raised as
    :class:`ExecutionFailure` (captured visibility this interpreter cannot
    reuse, or defaults whose visibility never settles), because no caller can
    report it as a screen rejection.
    """
    dispositions: list[dict[str, Any]] = []
    errors = _apply_assignments(
        namespace,
        screen,
        assignments,
        use_code,
        allowed=_answerable_fields(screen),
        dispositions=dispositions,
    )
    if errors:
        return AssignmentsRejected(
            errors=tuple(errors),
            dispositions=tuple(dispositions),
            validation_error=any(
                item["disposition"] == "validation error" for item in dispositions
            ),
        )
    if partial:
        submission_screen = _submission_screen(interview, namespace, screen)
    else:
        # The whole screen is submitted: browser defaults and blanks for
        # optional fields, then the required gate.
        submission_screen = _submit_screen_fields(namespace, screen, interview)
    violations = _required_violations(namespace, submission_screen, blank_submissions)
    if violations and not partial:
        return RequiredRejected(
            errors=tuple(
                _required_message(variable, reason) for variable, reason in violations
            ),
            dispositions=tuple(dispositions),
            missing_required=tuple(variable for variable, _reason in violations),
            active_fields=tuple(_active_field_names(screen)),
        )
    constraint_errors = (
        _constraint_errors(namespace, submission_screen) if not partial else []
    )
    if constraint_errors:
        return ValidationRejected(
            errors=tuple(constraint_errors),
            dispositions=tuple(dispositions),
        )
    validation = (
        _validate(interview, namespace, screen)
        if validate
        else {"errors": [], "warnings": []}
    )
    warnings = list(validation["warnings"])
    if partial and validate:
        warnings.extend(
            _required_message(variable, reason) for variable, reason in violations
        )
    authored_errors = list(validation["errors"])
    if strict:
        authored_errors.extend(warnings)
        warnings = []
    if authored_errors:
        return ValidationRejected(
            errors=tuple(authored_errors),
            dispositions=tuple(dispositions),
            warnings=tuple(warnings),
        )
    return SubmissionAccepted(
        dispositions=tuple(dispositions), warnings=tuple(warnings)
    )


def _screen_field_targets(screen):
    """Map generic-object placeholder fields to their resolved target paths.

    A generic-object screen describes fields under a placeholder name
    (``x.date``) while ``orig_sought`` names the resolved target
    (``rav.date``).  The placeholder root is the first segment of ``sought``;
    the resolved root is ``orig_sought`` with the same remainder removed.
    """
    sought = screen.get("sought")
    orig_sought = screen.get("orig_sought")
    if not isinstance(sought, str) or not isinstance(orig_sought, str):
        return {}
    if not sought or not orig_sought or sought == orig_sought:
        return {}
    root = sought.split(".", 1)[0]
    suffix = sought[len(root) :]
    if not orig_sought.endswith(suffix):
        return {}
    resolved_root = orig_sought[: len(orig_sought) - len(suffix)]
    if not resolved_root:
        return {}
    targets = {}
    for field in screen.get("fields") or []:
        variable = field.get("variable")
        if not isinstance(variable, str) or not variable:
            continue
        if variable == root:
            targets[variable] = resolved_root
        elif variable.startswith(root + "."):
            targets[variable] = resolved_root + variable[len(root) :]
    return targets


def _field_types(screen, targets):
    return {
        variable: str(field.get("type", "")).lower()
        for variable, field in _field_specs(screen, targets).items()
    }


def _field_specs(screen, targets):
    """Field metadata by variable, including resolved generic targets."""
    specs = {
        field.get("variable"): field
        for field in screen.get("fields") or []
        if isinstance(field.get("variable"), str)
    }
    for variable, target in targets.items():
        if variable in specs:
            specs.setdefault(target, specs[variable])
    return specs


def _answerable_fields(screen):
    """Field names a submission may set, including resolved generic targets."""
    fields = screen.get("fields")
    if not fields:
        # A screen with no described fields (continue, field-less deadend)
        # keeps the pre-existing behavior of accepting explicit assignments.
        return None
    allowed = set(_screen_field_targets(screen).values())
    for field in fields:
        variable = field.get("variable")
        if isinstance(variable, str) and variable:
            allowed.add(variable)
    return allowed


def _active_field_names(screen) -> list[str]:
    names = []
    for field in screen.get("fields") or []:
        variable = field.get("variable")
        if isinstance(variable, str) and variable:
            names.append(variable)
    for target in _screen_field_targets(screen).values():
        if target not in names:
            names.append(target)
    return names


def _unknown_field_details(screen, dispositions):
    names = _active_field_names(screen)
    suggestions = {}
    for item in dispositions:
        if item.get("disposition") != "unknown field":
            continue
        variable = item["variable"]
        matches = difflib.get_close_matches(variable, names, n=1, cutoff=0.35)
        if matches:
            item["suggestion"] = matches[0]
            suggestions[variable] = matches[0]
    return {
        "active_fields": names,
        "active_field_details": screen.get("fields") or [],
        "closest_matches": suggestions,
    }


def _required_message(variable, reason):
    if reason == "unselected":
        return (
            f"required field '{variable}' has no selected option "
            "(the browser would require at least one)"
        )
    state = "empty" if reason == "empty" else "undefined"
    return (
        f"required field '{variable}' is {state} (required while visible at submission)"
    )


def _submission_screen(interview, namespace, screen):
    """Copy field facts with current visibility, preserving the rendered contract."""
    question = getattr(interview, "questions_by_name", {}).get(
        screen.get("question_name")
    )
    compiled = {
        field_variable(field): field for field in getattr(question, "fields", ())
    }
    fields = []
    for described in screen.get("fields") or []:
        field = dict(described)
        source = compiled.get(field.get("variable"))
        if "visibility" in field:
            try:
                field["visible"], _note = described_field_visible(field, namespace)
            except CapturedVisibilityError as error:
                raise ExecutionFailure(ErrorKind.STATE, str(error)) from error
        elif source is not None:
            field["visible"], _note = field_visible(source, namespace, submission=True)
        if "submission_required" in field:
            field["required"] = field["submission_required"]
        fields.append(field)
    return {**screen, "fields": fields}


def _apply_rendered_defaults(namespace, screen, interview):
    """Resolve omitted defaults against submitted visibility, with a finite bound."""
    applied = set()
    targets = _screen_field_targets(screen)
    for _round in range(2 * len(screen.get("fields") or []) + 1):
        current = _submission_screen(interview, namespace, screen)
        changed = False
        for field in current.get("fields") or []:
            variable = field.get("variable")
            if variable in applied and field.get("visible") is False:
                _exec_in_namespace(namespace, f"del {targets.get(variable, variable)}")
                applied.remove(variable)
                changed = True
        current = _submission_screen(interview, namespace, screen)
        for variable, default, _required, field in _blankable_fields(
            namespace, current
        ):
            if (
                default is not None
                and str(field.get("type", "")).lower() != "signature"
            ):
                errors = _apply_assignments(
                    namespace, screen, ((variable, default),), False
                )
                if not errors:
                    applied.add(variable)
                    changed = True
                else:
                    logger.debug(
                        "field default %r could not be applied: %s", variable, errors
                    )
        if not changed:
            return current
    raise ExecutionFailure(
        ErrorKind.VALIDATION, "rendered defaults have unstable visibility"
    )


def _early_rejection_required_guidance(namespace, screen, blank_submissions, interview):
    """Assess valid assignments and defaults without validation or blank synthesis."""
    screen = _apply_rendered_defaults(namespace, screen, interview)
    violations = _required_violations(namespace, screen, blank_submissions)
    return {
        "missing_required": [variable for variable, _reason in violations],
    }


def _field_value(namespace, variable, targets):
    """Return ``(found, value)`` for a screen field, resolved target included."""
    candidates = [variable]
    target = targets.get(variable)
    if target and target not in candidates:
        candidates.append(target)
    for candidate in candidates:
        try:
            return True, eval(candidate, namespace)
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
        ):
            continue
    return False, None


def _is_empty_value(value):
    if value is None:
        return True
    return isinstance(value, str) and value == ""


#: Multiple-choice group datatypes the browser refuses to submit empty when
#: the field is required ("check at least one option").
_CHECKBOX_GROUP_DATATYPES = frozenset({"checkboxes", "multiselect"})
_OBJECT_GROUP_DATATYPES = frozenset({"object_checkboxes", "object_multiselect"})
_GROUP_DATATYPES = _CHECKBOX_GROUP_DATATYPES | _OBJECT_GROUP_DATATYPES


def _rendered_no_object_choices(datatype, field):
    """Whether an object group reached the browser with an empty choice list.

    docassemble calls this ``is_empty_mc``: the field's resolved choices came
    back empty, so the formatter hides it and the answer path clears and
    gathers rather than applying per-choice updates (ADR-0014).
    """
    return datatype in _OBJECT_GROUP_DATATYPES and not (field.get("choices") or [])


def _has_no_selected_option(value) -> bool:
    """Whether a group value holds no truthy selection.

    docassemble stores groups as ``DADict``/``DAList`` objects; their
    ``elements`` are read directly so evaluating the required gate never
    triggers gathering. Plain mappings and sequences are accepted too.
    """
    elements = getattr(value, "elements", None)
    if isinstance(elements, dict):
        return not any(bool(item) for item in elements.values())
    if isinstance(elements, (list, tuple, set)):
        return not elements
    if isinstance(value, dict):
        return not any(bool(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return not value
    return False


def _constraint_errors(namespace, screen):
    """Check rendered form rules, independently of authored validation code."""
    errors = []
    targets = _screen_field_targets(screen)
    for field in screen.get("fields") or []:
        variable = field.get("variable")
        if not variable or field.get("visible") is False:
            continue
        found, value = _field_value(namespace, variable, targets)
        if not found or _is_empty_value(value):
            continue
        datatype = str(field.get("type", "text")).lower()
        constraints = field.get("constraints") or {}
        for key, bound in constraints.items():
            violated = False
            if key in {"minlength", "maxlength"}:
                if datatype in _GROUP_DATATYPES:
                    elements = getattr(value, "elements", value)
                    length = (
                        sum(bool(selected) for selected in elements.values())
                        if isinstance(elements, dict)
                        else len(elements)
                    )
                else:
                    # jQuery counts UTF-16 code units and removes carriage returns.
                    length = (
                        len(
                            str(value)
                            .replace("\r", "")
                            .encode("utf-16-le", errors="surrogatepass")
                        )
                        // 2
                    )
                violated = (
                    length < int(float(bound))
                    if key == "minlength"
                    else length > int(float(bound))
                )
            elif key in {"min", "max", "step"} and datatype in {
                "integer",
                "float",
                "number",
                "currency",
                "range",
            }:
                try:
                    number, limit = Decimal(str(value)), Decimal(str(bound))
                    if key == "min":
                        violated = number < limit
                    elif key == "max":
                        violated = number > limit
                    elif datatype != "range" and limit > 0:
                        # Numeric text inputs use jQuery's zero-based multiple rule.
                        violated = number % limit != 0
                except (InvalidOperation, ValueError):
                    continue
            elif key in {"min", "max"} and datatype == "date":
                rendered = (
                    value.isoformat() if hasattr(value, "isoformat") else str(value)
                )
                limit = str(bound)
                rendered, limit = rendered.replace(" ", "T"), limit.replace(" ", "T")
                if datatype == "date":
                    rendered, limit = rendered[:10], limit[:10]
                violated = rendered < limit if key == "min" else rendered > limit
            if violated:
                errors.append(f"field '{variable}' violates {key} constraint ({bound})")
    return errors


def _required_violations(namespace, screen, blank_submissions):
    """Return ``(variable, reason)`` for required visible fields that are not
    answered. Signatures are never required; hidden fields are not shown and
    therefore not assigned or required. Blank intent is keyed by the name the
    caller submitted, which for a generic-object screen may be the resolved
    target rather than the placeholder field variable."""
    violations = []
    targets = _screen_field_targets(screen)
    for field in screen.get("fields") or []:
        variable = field.get("variable")
        if not isinstance(variable, str) or not variable:
            continue
        if field.get("visible") is False:
            continue
        datatype = str(field.get("type", "")).lower()
        if datatype == "signature":
            continue
        if field.get("required") is False:
            continue
        if _rendered_no_object_choices(datatype, field):
            # A zero-choice group cannot be selected, and the answer path clears
            # and gathers it (ADR-0014), so the required gate must not demand a
            # selection it cannot receive.
            continue
        found, value = _field_value(namespace, variable, targets)
        if not found:
            violations.append((variable, "undefined"))
        elif (
            variable in blank_submissions
            or targets.get(variable) in blank_submissions
            or _is_empty_value(value)
        ):
            violations.append((variable, "empty"))
        elif datatype in _GROUP_DATATYPES and _has_no_selected_option(value):
            violations.append((variable, "unselected"))
    return violations


def _blankable_fields(namespace, screen):
    """Yield fields the browser would submit for a shown screen but that are
    not yet defined: ``(variable, default, required, field)``."""
    targets = _screen_field_targets(screen)
    for field in screen.get("fields") or []:
        variable = field.get("variable")
        if not isinstance(variable, str) or not variable:
            continue
        if field.get("visible") is False:
            continue
        if _field_value(namespace, variable, targets)[0]:
            continue
        default = field.get("default")
        if isinstance(default, (int, float, bool)):
            default = str(default)
        elif not isinstance(default, str) or not default.strip():
            default = None
        yield variable, default, field.get("required") is not False, field


def _submit_screen_fields(namespace, screen, interview):
    """Define every visible field the browser would submit, like a form POST.

    Defaults become the submitted values (the browser prefills them), optional
    blanks become the value the browser would post for the datatype, and
    signature fields become ``DAEmpty()`` so documents can still render them.
    Required fields with no value are left for the required gate.
    """
    screen = _apply_rendered_defaults(namespace, screen, interview)
    blanks = []
    targets = _screen_field_targets(screen)
    for variable, _default, required, field in _blankable_fields(namespace, screen):
        datatype = str(field.get("type", "")).lower()
        if _rendered_no_object_choices(datatype, field):
            # The browser still posts a group whose choices resolved empty, and
            # docassemble clears it rather than leaving it undefined
            # (ADR-0014). Like the other blanks, this lands on the resolved
            # target rather than a generic-object placeholder. A required
            # zero-choice group is cleared too: it has nothing to tick, so
            # requiring a selection would make the screen unanswerable.
            _clear_empty_object_group(
                namespace, targets.get(variable, variable), datatype
            )
            continue
        signature = datatype == "signature"
        if required and not signature:
            continue
        blank = _browser_blank(field)
        if blank is not _NO_BLANK:
            blanks.append((variable, blank))
    for variable, blank in blanks:
        _set_namespace_value(namespace, targets.get(variable, variable), blank)
    return screen


#: A field datatype with no faithful browser blank (for example a custom type
#: whose ``empty()`` value only the runtime knows); it is left undefined.
_NO_BLANK = object()


#: Boolean input types whose unselected state is ``None`` rather than False.
_RADIO_BOOLEAN_INPUT_TYPES = frozenset(
    {"yesnomaybe", "noyesmaybe", "yesnoradio", "noyesradio"}
)


def _browser_blank(field):
    """The value a browser form posts for an untouched visible field.

    docassemble's formatter and form processor derive this from the datatype
    and the input type: empty inputs stay empty strings, numeric types coerce
    a blank to zero, radio-style booleans and object selections become
    ``None``, and a checkbox-style boolean takes its unchecked value (False
    for ``yesno``, True for ``noyes``, matching the formatter's ``_checkboxes``
    hidden input). Custom types are left undefined because only their runtime
    class knows the empty value.
    """
    datatype = str(field.get("type", "")).lower()
    input_type = str(field.get("input_type", "")).lower()
    if datatype == "signature":
        from docassemble.base.util import DAEmpty

        return DAEmpty()
    if datatype == "integer":
        return 0
    if datatype in {"number", "float", "currency", "range"}:
        return 0.0
    if datatype == "boolean":
        if input_type in _RADIO_BOOLEAN_INPUT_TYPES:
            return None
        return input_type.startswith("noyes")
    if datatype == "settrue":
        # A continue button submits True; there is no unchecked state.
        return True
    if datatype in {"threestate", "object", "object_radio", "file"}:
        return None
    if datatype in _OBJECT_GROUP_DATATYPES:
        # Object selections need runtime initialisation; a fabricated empty
        # object could corrupt interview logic, so leave it undefined.
        return _NO_BLANK
    if datatype in _CHECKBOX_GROUP_DATATYPES:
        if not any("value" in choice for choice in field.get("choices") or []):
            return None
        return _checkbox_group_elements(field, {})
    if datatype in {
        "text",
        "email",
        "phone",
        "telephone",
        "area",
        "raw",
        "date",
        "datetime",
        "datetime-local",
        "time",
    }:
        return ""
    return _NO_BLANK


def _checkbox_group_elements(field, submitted):
    """The browser's posted state for one checkbox group.

    docassemble's formatter registers every rendered choice in the hidden
    ``_checkboxes`` state as ``False``, so a choice an explicit submission
    omits is a definite ``False`` rather than an undefined key. The blank path
    (``_browser_blank``) and the explicit-submission path both build their
    group value here, so the two cannot drift. Keys the screen did not render
    keep whatever the submission provided.
    """
    elements = {
        str(choice["value"]): False
        for choice in field.get("choices") or []
        if "value" in choice
    }
    elements.update({str(key): item for key, item in submitted.items()})
    from docassemble.base.util import DADict

    return DADict(elements=elements)


#: Namespace slots the simulator uses to carry a Python value into an exec'd
#: interview statement. Interview code sees the namespace as its globals, so a
#: carrier is briefly visible to it and the value it displaced is restored.
_CARRIER_VALUE = "__dasimulator_value"
_CARRIER_OBJECT = "__dasimulator_object"


def _exec_in_namespace(namespace, source, carrier=_MISSING, value=None):
    """exec ``source`` with the interview namespace as globals.

    ``carrier``, when given, is bound to ``value`` for the duration and the
    value it displaced is restored afterwards. Answers are applied by exec'ing
    in the namespace rather than by calling methods directly because
    docassemble resolves the interview from the caller's frame: 1.9's
    ``get_user_dict()`` scans frames for ``_internal`` and 1.10 reads a context
    variable. Interview code that resolves interview state (walkup's
    registry-backed ``ObjectReferenceList``, for example) therefore only works
    when the caller runs inside the namespace, which is where the server runs
    it.
    """
    if carrier is _MISSING:
        __builtins__["exec"](source, namespace)
        return
    previous = namespace.get(carrier, _MISSING)
    namespace[carrier] = value
    try:
        __builtins__["exec"](source, namespace)
    finally:
        if previous is _MISSING:
            namespace.pop(carrier, None)
        else:
            namespace[carrier] = previous


def _set_namespace_value(namespace, target, value):
    _exec_in_namespace(namespace, f"{target} = {_CARRIER_VALUE}", _CARRIER_VALUE, value)


def _apply_assignments(
    namespace,
    screen,
    assignments,
    use_code,
    allowed=None,
    dispositions=None,
):
    errors = []
    dispositions = dispositions if dispositions is not None else []
    targets = _screen_field_targets(screen)
    field_types = _field_types(screen, targets)
    field_specs = _field_specs(screen, targets)
    for variable, raw in assignments:
        if allowed is not None and variable not in allowed:
            errors.append(f"{variable}: not a field on the active screen")
            dispositions.append(
                {
                    "variable": variable,
                    "input": raw,
                    "disposition": "unknown field",
                }
            )
            continue
        try:
            if use_code:
                _exec_in_namespace(namespace, f"{variable} = {raw}")
                dispositions.append(
                    {"variable": variable, "input": raw, "disposition": "applied"}
                )
                continue
            value = parse_value(raw)
            datatype = field_types.get(variable, "")
            target = targets.get(variable, variable)
            custom_hook = _custom_datatype_hook(
                (field_specs.get(variable) or {}).get("type", "")
            )
            if custom_hook is not None:
                value = _coerce_custom_datatype(
                    custom_hook, raw, target, field_specs.get(variable) or {}
                )
            elif datatype == "date":
                value = _coerce_date(raw, value)
            elif raw.strip() == "":
                # An empty input is a browser submission too: numeric fields
                # become zero, booleans become None, and an empty object
                # selection is not set at all.
                if datatype == "integer":
                    value = 0
                elif datatype in {"number", "float", "currency", "range"}:
                    value = 0.0
                elif datatype in {"boolean", "threestate"}:
                    value = None
                elif datatype in {"object", "object_radio"}:
                    dispositions.append(
                        {
                            "variable": variable,
                            "input": raw,
                            "disposition": "applied",
                        }
                    )
                    continue
            if datatype in {
                "object",
                "object_radio",
                "object_multiselect",
                "object_checkboxes",
            }:
                _apply_object(namespace, variable, target, datatype, value)
            else:
                if datatype in _CHECKBOX_GROUP_DATATYPES and isinstance(value, dict):
                    value = _checkbox_group_elements(
                        field_specs.get(variable) or {}, value
                    )
                _set_namespace_value(namespace, target, value)
            dispositions.append(
                {"variable": variable, "input": raw, "disposition": "applied"}
            )
        except ExecutionFailure as error:
            errors.append(f"{variable}: {error}")
            dispositions.append(
                {
                    "variable": variable,
                    "input": raw,
                    "disposition": "validation error",
                    "error": str(error),
                }
            )
        except (
            ValueError,
            TypeError,
            RuntimeError,
            AttributeError,
            KeyError,
            IndexError,
            ImportError,
            OSError,
            LookupError,
            NameError,
            SyntaxError,
        ) as error:
            errors.append(f"{variable}: {type(error).__name__}: {error}")
            dispositions.append(
                {
                    "variable": variable,
                    "input": raw,
                    "disposition": "parse error",
                    "error": f"{type(error).__name__}: {error}",
                }
            )
    return errors


def _custom_datatype_hook(datatype):
    # Older runtimes and unregistered datatypes keep ordinary assignment semantics.
    try:
        from docassemble.base.functions import custom_types
    except ImportError:
        return None
    info = custom_types.get(datatype) or {}
    hook = info.get("class")
    if all(
        callable(getattr(hook, name, None))
        for name in ("call_validate", "call_transform")
    ):
        return hook
    return None


def _coerce_custom_datatype(hook, raw, target, field):
    data = dict(field.get("constraints") or {})
    for key in (
        "custom_parameters",
        "custom_parameters_code",
        "custom_parameters_mako",
    ):
        data.update(field.get(key) or {})
    try:
        if not hook.call_validate(raw, target, data):
            raise ValueError("You need to enter a valid value.")
        return hook.call_transform(raw, target, data)
    except Exception as error:
        raise ExecutionFailure(
            ErrorKind.VALIDATION, str(error) or "invalid value"
        ) from error


def _coerce_date(raw, parsed):
    # docassemble's form processor preserves an empty date input as the empty
    # string; optional date fields therefore remain browser-faithful.
    if raw == "" or parsed == "":
        return ""
    if not isinstance(parsed, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parsed):
        raise ValueError("date answers must use YYYY-MM-DD")
    datetime.date.fromisoformat(parsed)
    from docassemble.base.util import as_datetime

    return as_datetime(parsed)


def _validate(interview, namespace, screen):
    errors, warnings = [], []
    question = (
        interview.questions_by_name.get(screen.get("question_name"))
        if screen.get("question_name")
        else None
    )
    code = getattr(question, "validation_code", None) if question else None
    if code is not None:
        from docassemble.base.error import DAValidationError

        try:
            __builtins__["exec"](code, namespace)
        except DAValidationError as error:
            errors.append(
                (
                    f"field '{error.field}': "
                    if isinstance(getattr(error, "field", None), str)
                    else ""
                )
                + (str(error) or "validation failed")
            )
        except (
            ValueError,
            TypeError,
            RuntimeError,
            AttributeError,
            KeyError,
            IndexError,
            ImportError,
            OSError,
            LookupError,
            NameError,
            SyntaxError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
            ArithmeticError,
            AssertionError,
        ) as error:
            errors.append(
                f"validation code crashed ({type(error).__name__}): {first_line(error)}"
            )
    return {"errors": errors, "warnings": warnings}


def _object_selections(namespace, variable):
    """The objects the screen offered for one object field, keyed by choice."""
    return namespace.get("_internal", {}).get("objselections", {}).get(variable, {})


def _apply_object(namespace, variable, target, datatype, value):
    """Apply one object answer, following the server's two paths.

    ``variable`` is the field as the screen named and posted it; ``target`` is
    the resolved name the answer is applied to (the same name on an ordinary
    screen, the generic-object target on a placeholder screen). Which path
    applies is decided by whether the screen rendered any choices
    (docassemble's ``parse.is_empty_mc``): a field that rendered choices is
    applied per choice, and a field that rendered none is cleared and marked
    gathered. ADR-0014 records both.
    """
    selections = _object_selections(namespace, variable)
    if not isinstance(selections, dict):
        raise TypeError(f"no object selections are available for {variable}")
    if datatype in {"object", "object_radio"}:
        key = next(iter(value), None) if isinstance(value, dict) else value
        if key in (None, ""):
            selected = None
        elif key not in selections:
            raise ValueError(f"unknown object choice {key!r} for {variable}")
        else:
            selected = selections[key]
        _exec_in_namespace(
            namespace, f"{target} = {_CARRIER_OBJECT}", _CARRIER_OBJECT, selected
        )
        return
    if not selections:
        _clear_empty_object_group(namespace, target, datatype)
        return
    _apply_object_choice(namespace, variable, target, datatype, value, selections)


def _ensure_object_target(namespace, variable, datatype):
    """Define the group target when the flow has not created it yet.

    The browser posts ``<var>.gathered`` for an object group, and the server
    uses that to create the list before applying the per-choice answer.
    """
    try:
        eval(variable, namespace)
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
        logger.debug("object target %r not found, creating: %s", variable, exc)
        from docassemble.base.parse import ensure_object_exists

        ensure_object_exists(variable, datatype, namespace)


def _object_choice_state(raw):
    """The browser's checkbox state for one rendered choice.

    ``None`` means the server skips the key and leaves the element alone, which
    is also what it does with any value outside the ``True``/``False``/``None``
    trio.
    """
    if raw is True or raw == "True":
        return True
    if raw is False or raw == "False":
        return False
    return None


def _apply_object_choice(namespace, variable, target, datatype, value, choices):
    """Apply a group answer the way the server does when choices were rendered.

    ``variable`` names the field for diagnostics and selection lookup; every
    mutation lands on ``target``. Every rendered choice carries a checkbox, and
    the formatter registers all of them in ``_checkboxes`` as ``'False'``, so a
    choice missing from the submission is unticked. A ticked choice is appended
    only when absent and an unticked choice is removed only when present,
    mirroring the server's ``.elements`` guards. The list is never cleared, so
    an element with no checkbox on the page survives.
    """
    if value is None or isinstance(value, (str, bytes)):
        raise ValueError(
            f"object checkbox answer for {variable} must be a mapping or list"
        )
    posted = list(
        value.items() if isinstance(value, dict) else ((key, True) for key in value)
    )
    unknown = [key for key, _ in posted if key not in choices]
    if unknown:
        raise ValueError(f"unknown object choices for {variable}: {unknown!r}")
    states = {key: False for key in choices}
    for key, raw in posted:
        states[key] = _object_choice_state(raw)
    _ensure_object_target(namespace, target, datatype)
    for key, state in states.items():
        if state is None:
            continue
        selection = choices[key]
        if state:
            _exec_in_namespace(
                namespace,
                f"if {_CARRIER_OBJECT} not in {target}.elements:\n"
                f"    {target}.append({_CARRIER_OBJECT})",
                _CARRIER_OBJECT,
                selection,
            )
        else:
            _exec_in_namespace(
                namespace,
                f"if {_CARRIER_OBJECT} in {target}.elements:\n"
                f"    {target}.remove({_CARRIER_OBJECT})",
                _CARRIER_OBJECT,
                selection,
            )
    _mark_object_gathered(namespace, target)


def _clear_empty_object_group(namespace, variable, datatype):
    """Apply the server's path for a field that rendered no choices.

    docassemble routes a multiple-choice field whose resolved choices came back
    empty through the formatter's ``hiddens``; the answer handling clears the
    group and marks it gathered. ``ensure_object_exists`` leaves an existing
    target alone, so the target is created only when the flow has not defined
    it yet.
    """
    _ensure_object_target(namespace, variable, datatype)
    _exec_in_namespace(namespace, f"{variable}.clear()")
    _mark_object_gathered(namespace, variable)


def _mark_object_gathered(namespace, variable):
    """Mark an object group gathered.

    The server does not do this for a group that rendered choices, and the
    simulator must, or the screen's ``<var>.gathered`` seek never resolves and
    the same screen re-asks forever (docs/fidelity-round-2.md §1).
    """
    try:
        _exec_in_namespace(namespace, f"{variable}.gathered = True")
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        logger.debug("setting gathered failed: %s", exc)


def parse_value(raw: str):
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        import ast

        value = ast.literal_eval(raw)
        return (
            value
            if isinstance(value, (str, int, float, bool, list, dict, tuple, set))
            or value is None
            else raw
        )
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
        logger.debug("literal_eval failed for %r: %s", raw, exc)
        return raw
