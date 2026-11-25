"""Package-private attachment entry discovery, matching, and single-entry assembly."""

from __future__ import annotations

import ast
import logging
import re
import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docassemble_simulator._artifacts import docx_paragraph_count
from docassemble_simulator._runtime import (
    PDF_UNAVAILABLE_MESSAGE,
    PDFConversionUnavailable,
)

logger = logging.getLogger(__name__)

INDEX_NAMES: tuple[str, ...] = ("i", "j", "k", "l", "m", "n")
_INDEX_PATTERN = re.compile(r"\[([ijklmn])\]")
_LEADING_IDENTIFIER = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(.*)$", re.DOTALL)
_SKIP_TYPES = (
    str,
    bytes,
    bytearray,
    int,
    float,
    bool,
    type(None),
    types.ModuleType,
    types.FunctionType,
    types.BuiltinFunctionType,
    types.MethodType,
    types.CodeType,
    type,
)


class AttachmentInputError(ValueError):
    """Typed user-input failure during attachment resolution or validation."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = dict(details) if details else {}


class AttachmentAssemblyError(Exception):
    """Failure during attachment preparation or finalization with captured context."""

    def __init__(
        self,
        cause: BaseException,
        *,
        canonical_variable: str,
        prepared_bindings: Mapping[str, Any] | None = None,
    ):
        super().__init__(str(cause))
        self.cause = cause
        self.canonical_variable = canonical_variable
        self.prepared_bindings = dict(prepared_bindings) if prepared_bindings else {}


@dataclass(frozen=True)
class AttachmentEntry:
    """One compiled ``attachments:`` list item on a docassemble ``Question``."""

    question: Any
    attachment: dict[str, Any]
    question_name: str
    indexno: int
    variable_name: str
    is_generic: bool
    docx_templates: tuple[str, ...]
    has_docx_template: bool
    is_manual: bool = False
    is_pdf_template: bool = False
    is_raw: bool = False

    @property
    def is_concrete(self) -> bool:
        """Whether the variable template has no generic ``x`` or index placeholders."""
        if self.is_generic and re.match(r"^x\b", self.variable_name):
            return False
        return _INDEX_PATTERN.search(self.variable_name) is None

    @property
    def first_template(self) -> str | None:
        if not self.docx_templates:
            return None
        return _normalize_template_ref(self.docx_templates[0])


@dataclass(frozen=True)
class AttachmentTarget:
    """One attachment entry plus the concrete ``x`` and index bindings to assemble it."""

    entry: AttachmentEntry
    variable: str
    x_expression: str | None = None
    index_values: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class AttachmentAssembly:
    """Observable result of assembling one attachment target."""

    canonical_variable: str
    docx_path: Path
    filename: str
    template: str
    paragraphs: int


def _normalize_template_ref(reference: str) -> str:
    """Normalize a ``FileInPackage.original_reference()`` to a relative path."""
    text = str(reference).strip()
    if ":" in text:
        _, _, text = text.partition(":")
    text = text.replace("\\", "/").lstrip("/")
    if text.startswith("data/templates/"):
        text = text.removeprefix("data/templates/")
    return Path(text).as_posix()


def _extract_static_docx_templates(raw_docx: Any) -> tuple[str, ...]:
    if not raw_docx:
        return ()
    items = raw_docx if isinstance(raw_docx, (list, tuple)) else (raw_docx,)
    templates: list[str] = []
    for item in items:
        if getattr(item, "is_code", False):
            continue
        original = getattr(item, "original_reference", None)
        if callable(original):
            ref = original()
        elif isinstance(item, str):
            ref = item
        else:
            continue
        if isinstance(ref, str) and ref.strip():
            templates.append(ref.strip())
    return tuple(templates)


def enumerate_entries(interview: Any) -> tuple[AttachmentEntry, ...]:
    """Enumerate every named ``attachments:`` entry across ``interview.all_questions``."""
    entries: list[AttachmentEntry] = []
    questions = getattr(interview, "all_questions", None) or ()
    for question in questions:
        attachments = getattr(question, "attachments", None)
        if not attachments:
            continue
        is_generic = bool(getattr(question, "is_generic", False))
        q_name = getattr(question, "name", None) or ""
        for idx, attachment in enumerate(attachments):
            if not isinstance(attachment, dict):
                continue
            variable_name = attachment.get("variable_name")
            if not isinstance(variable_name, str) or not variable_name.strip():
                continue
            options = attachment.get("options")
            if not isinstance(options, dict):
                options = {}
            raw_docx = options.get("docx_template_file")
            question_name = attachment.get("question_name") or q_name
            raw_indexno = attachment.get("indexno")
            indexno = int(raw_indexno) if raw_indexno is not None else idx
            entries.append(
                AttachmentEntry(
                    question=question,
                    attachment=attachment,
                    question_name=str(question_name),
                    indexno=indexno,
                    variable_name=variable_name.strip(),
                    is_generic=is_generic,
                    docx_templates=_extract_static_docx_templates(raw_docx),
                    has_docx_template=bool(raw_docx),
                    is_manual=("manual" in options or "manual code" in options),
                    is_pdf_template="pdf_template_file" in options,
                    is_raw=bool(attachment.get("raw")),
                )
            )
    return tuple(entries)


def compile_matcher(entry: AttachmentEntry) -> tuple[re.Pattern[str], tuple[str, ...]]:
    """Compile a regex matching concrete variable names for ``entry``.

    Leading ``x`` is a root-expression placeholder only when ``entry.is_generic``
    is true. ``[i]``..``[n]`` match integer indices; repeated occurrences of the
    same index name require equal values. All other text matches literally.
    """
    template = entry.variable_name
    parts: list[str] = ["^"]
    pos = 0
    if entry.is_generic and re.match(r"^x\b", template):
        parts.append("(?P<x>.+)")
        pos = 1

    seen_indices: list[str] = []
    for match in _INDEX_PATTERN.finditer(template, pos):
        if match.start() > pos:
            parts.append(re.escape(template[pos : match.start()]))
        idx_name = match.group(1)
        if idx_name in seen_indices:
            parts.append(rf"\[(?P={idx_name})\]")
        else:
            seen_indices.append(idx_name)
            parts.append(rf"\[(?P<{idx_name}>-?\d+)\]")
        pos = match.end()

    if pos < len(template):
        parts.append(re.escape(template[pos:]))
    parts.append("$")
    return re.compile("".join(parts)), tuple(seen_indices)


def _is_valid_expression(expression: str) -> bool:
    if not expression or not expression.strip():
        return False
    try:
        ast.parse(expression, mode="eval")
    except SyntaxError:
        return False
    return True


def _root_identifier(expression: str) -> str | None:
    """Return the root identifier of a Python attribute/subscript expression."""
    try:
        node = ast.parse(expression, mode="eval").body
    except SyntaxError:
        return None
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    return None


def _is_generic_x_root(expression: str) -> bool:
    """Whether ``expression`` is rooted at docassemble's generic ``x`` placeholder."""
    return _root_identifier(expression) == "x"


def _match_variable_raw(
    entry: AttachmentEntry, variable: str
) -> AttachmentTarget | None:
    cleaned = variable.strip()
    if not cleaned:
        return None
    pattern, index_names = compile_matcher(entry)
    match = pattern.fullmatch(cleaned)
    if match is None:
        return None
    groups = match.groupdict()
    x_expression = groups.get("x")
    if x_expression is not None and not _is_valid_expression(x_expression):
        return None
    index_values = tuple((name, int(groups[name])) for name in index_names)
    return AttachmentTarget(
        entry=entry,
        variable=cleaned,
        x_expression=x_expression,
        index_values=index_values,
    )


def match_variable(entry: AttachmentEntry, variable: str) -> AttachmentTarget | None:
    """Match a concrete variable spelling against one ``AttachmentEntry``.

    Rejects generic ``x``-rooted operands (such as ``x.original[0].petition``)
    when ``entry.is_generic`` is true, because ``x`` is a generic-object
    placeholder rather than a concrete session variable.
    """
    target = _match_variable_raw(entry, variable)
    if target is None:
        return None
    if target.x_expression is not None and _is_generic_x_root(target.x_expression):
        return None
    return target


def _partition_targets(
    entries: Sequence[AttachmentEntry], variable: str
) -> tuple[list[AttachmentTarget], list[AttachmentTarget]]:
    """Partition matching targets into concrete matches and rejected ``x``-rooted ones."""
    cleaned = variable.strip()
    if not cleaned:
        return [], []
    concrete: list[AttachmentTarget] = []
    generic_x: list[AttachmentTarget] = []
    for entry in entries:
        target = _match_variable_raw(entry, cleaned)
        if target is None:
            continue
        if target.x_expression is not None and _is_generic_x_root(target.x_expression):
            generic_x.append(target)
        else:
            concrete.append(target)
    if not concrete and not generic_x:
        normalized = _INDEX_PATTERN.sub("[0]", cleaned)
        if normalized != cleaned:
            for entry in entries:
                if not entry.is_generic:
                    continue
                target = _match_variable_raw(entry, normalized)
                if (
                    target is not None
                    and target.x_expression is not None
                    and _is_generic_x_root(target.x_expression)
                ):
                    generic_x.append(target)
    return concrete, generic_x


def resolve_targets(
    entries: Sequence[AttachmentEntry], variable: str
) -> list[AttachmentTarget]:
    """Return every concrete ``AttachmentTarget`` whose template matches ``variable``."""
    concrete, _ = _partition_targets(entries, variable)
    return concrete


def _generic_x_error(
    variable: str,
    generic_targets: Sequence[AttachmentTarget],
    *,
    namespace: Mapping[str, Any] | None = None,
) -> AttachmentInputError:
    cleaned = variable.strip()
    matched_entries = [target.entry for target in generic_targets]
    concrete_candidates = (
        [var for var, _ in discover_candidates(namespace, matched_entries)]
        if namespace is not None
        else []
    )
    details: dict[str, Any] = {"attachment": cleaned}
    if concrete_candidates:
        details["candidates"] = concrete_candidates
        if len(concrete_candidates) == 1:
            hint = (
                f"pass a concrete object path instead (e.g. {concrete_candidates[0]!r})"
            )
        else:
            shown = concrete_candidates[:5]
            listed = ", ".join(shown)
            if len(concrete_candidates) > 5:
                listed = f"{listed}, ..."
            hint = f"pass a concrete object path instead (available: {listed})"
    else:
        first = generic_targets[0]
        x_expr = first.x_expression or "x"
        normalized = _INDEX_PATTERN.sub("[0]", cleaned)
        suffix = normalized[len(x_expr) :] if normalized.startswith(x_expr) else ""
        example = f"<object>{suffix}"
        hint = f"pass a concrete object path instead (for example, {example})"
    return AttachmentInputError(
        f"attachment {cleaned!r} uses generic placeholder 'x'; {hint}",
        details,
    )


def _last_segment(variable: str) -> str:
    cleaned = variable.strip()
    if "." in cleaned:
        return cleaned.rsplit(".", 1)[-1]
    return cleaned


def _ambiguous_explicit_error(
    cleaned: str, matches: Sequence[AttachmentTarget]
) -> AttachmentInputError:
    counts: dict[str, int] = {}
    for target in matches:
        name = target.entry.variable_name
        counts[name] = counts.get(name, 0) + 1
    entries = sorted(
        (
            {
                "variable_name": target.entry.variable_name,
                "question": target.entry.question_name,
                "indexno": target.entry.indexno,
            }
            for target in matches
        ),
        key=lambda item: (item["variable_name"], item["question"], item["indexno"]),
    )
    labels: list[str] = []
    for item in entries:
        name = item["variable_name"]
        if counts[name] > 1:
            qualifier = (
                f"{item['question']}[{item['indexno']}]"
                if item["question"]
                else f"#{item['indexno']}"
            )
            labels.append(f"{name} ({qualifier})")
        else:
            labels.append(name)
    candidates = sorted({target.entry.variable_name for target in matches})
    listed = ", ".join(labels)
    return AttachmentInputError(
        f"{cleaned!r} matches {len(matches)} attachment entries: {listed}",
        {"candidates": candidates, "entries": entries},
    )


def resolve_explicit_target(
    entries: Sequence[AttachmentEntry],
    variable: str,
    *,
    namespace: Mapping[str, Any] | None = None,
) -> AttachmentTarget:
    """Resolve an explicit ``--attachment VAR`` to exactly one target."""
    cleaned = variable.strip()
    matches, generic_x_matches = _partition_targets(entries, cleaned)
    if not matches:
        if generic_x_matches:
            raise _generic_x_error(cleaned, generic_x_matches, namespace=namespace)
        tail = _last_segment(cleaned)
        similar = sorted(
            {
                entry.variable_name
                for entry in entries
                if _last_segment(entry.variable_name) == tail
            }
        )
        if similar:
            listed = ", ".join(similar)
            raise AttachmentInputError(
                f"no attachment block declares {cleaned!r} (similar entries: {listed})",
                {"candidates": similar},
            )
        raise AttachmentInputError(f"no attachment block declares {cleaned!r}")
    if len(matches) > 1:
        raise _ambiguous_explicit_error(cleaned, matches)
    return matches[0]


def match_template_entries(
    entries: Sequence[AttachmentEntry], template: str
) -> tuple[AttachmentEntry, ...]:
    """Return entries whose static ``docx_template_file`` matches ``template``."""
    requested = _normalize_template_ref(template)
    return tuple(
        entry
        for entry in entries
        if any(
            _normalize_template_ref(ref) == requested for ref in entry.docx_templates
        )
    )


def _safe_vars(value: Any) -> dict[str, Any] | None:
    try:
        return vars(value)
    except TypeError:
        return None


def _safe_instance_name(value: Any) -> str | None:
    obj_dict = _safe_vars(value)
    if obj_dict is not None:
        name = obj_dict.get("instanceName")
        if isinstance(name, str) and name:
            return name
    return None


def discover_candidates(
    namespace: Mapping[str, Any],
    entries: Sequence[AttachmentEntry],
    *,
    max_depth: int = 16,
) -> list[tuple[str, AttachmentEntry]]:
    """Walk ``namespace`` for assembled ``DAFileCollection`` instances matching ``entries``.

    Walks ``elements`` (dict/list/set) and ``vars()`` rather than ``attrList``
    (which is empty for objects populated in code blocks), avoiding ``__getattr__``
    so docassemble objects never trigger lazy variable seeking.
    """
    by_key: dict[tuple[str, int], AttachmentEntry] = {}
    for entry in entries:
        if entry.question_name:
            by_key[(entry.question_name, entry.indexno)] = entry
    if not by_key:
        return []

    found: dict[str, AttachmentEntry] = {}
    seen: set[int] = set()

    def visit(value: Any, depth: int) -> None:
        if isinstance(value, _SKIP_TYPES):
            return
        obj_id = id(value)
        if obj_id in seen:
            return
        seen.add(obj_id)

        obj_dict = _safe_vars(value)
        if obj_dict is not None:
            info = obj_dict.get("info")
            instance_name = obj_dict.get("instanceName")
            if (
                isinstance(info, dict)
                and isinstance(instance_name, str)
                and instance_name
            ):
                attachment_meta = info.get("attachment")
                if isinstance(attachment_meta, dict):
                    att_name = attachment_meta.get("name")
                    att_number = attachment_meta.get("number")
                    if isinstance(att_name, str) and isinstance(att_number, int):
                        matched_entry = by_key.get((att_name, att_number))
                        if matched_entry is not None:
                            found.setdefault(instance_name, matched_entry)

        if depth >= max_depth:
            return

        if isinstance(value, dict):
            for child in value.values():
                visit(child, depth + 1)
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for child in value:
                visit(child, depth + 1)
            return
        if obj_dict is None:
            return

        elements = obj_dict.get("elements")
        if isinstance(elements, dict):
            for child in elements.values():
                visit(child, depth + 1)
        elif isinstance(elements, (list, tuple, set, frozenset)):
            for child in elements:
                visit(child, depth + 1)

        for key, child in obj_dict.items():
            if key in {"elements", "info", "instanceName", "attrList", "_internal"}:
                continue
            if isinstance(key, str) and key.startswith("__"):
                continue
            visit(child, depth + 1)

    for key, value in namespace.items():
        if key in {"_internal", "__builtins__"}:
            continue
        visit(value, 0)

    return sorted(found.items(), key=lambda item: item[0])


def resolve_automatic_target(
    entries: Sequence[AttachmentEntry],
    namespace: Mapping[str, Any],
    template: str,
) -> AttachmentTarget | None:
    """Resolve ``render TEMPLATE`` against matching attachment entries.

    Returns ``None`` when no compiled attachment entry statically references
    ``template``, allowing the caller to fall back to the direct render path.
    """
    matching_entries = match_template_entries(entries, template)
    if not matching_entries:
        return None

    candidates = discover_candidates(namespace, matching_entries)
    if len(candidates) == 1:
        candidate_var, entry = candidates[0]
        target = match_variable(entry, candidate_var)
        if target is not None:
            return target
        return AttachmentTarget(entry=entry, variable=candidate_var)

    if len(candidates) > 1:
        candidate_vars = [var for var, _ in candidates]
        raise AttachmentInputError(
            f"{template} matches {len(candidate_vars)} attachment variables; "
            "pass --attachment",
            {"template": template, "candidates": candidate_vars},
        )

    if len(matching_entries) == 1 and matching_entries[0].is_concrete:
        entry = matching_entries[0]
        return AttachmentTarget(entry=entry, variable=entry.variable_name)

    entry_templates = sorted({entry.variable_name for entry in matching_entries})
    listed = ", ".join(entry_templates)
    hint = (
        "pass --attachment with a concrete object path "
        "(replacing generic 'x' or index placeholders)"
        if any(not entry.is_concrete for entry in matching_entries)
        else "pass --attachment VAR"
    )
    raise AttachmentInputError(
        f"{template} has no assembled attachment instance in this session "
        f"(declared by {listed}); {hint}",
        {"template": template, "candidates": entry_templates},
    )


def _substitute_indices(
    text: str,
    *mappings: Mapping[str, Any] | None,
    int_only: bool = False,
) -> str:
    result = text
    for idx_name in INDEX_NAMES:
        found = False
        idx_value: Any = None
        for mapping in mappings:
            if mapping is not None and idx_name in mapping:
                idx_value = mapping[idx_name]
                found = True
                break
        if not found:
            continue
        if int_only and (not isinstance(idx_value, int) or isinstance(idx_value, bool)):
            continue
        replacement = f"[{idx_value!r}]"
        result = re.sub(
            r"\[" + idx_name + r"\]",
            lambda _match, rep=replacement: rep,
            result,
        )
    return result


def _fallback_canonical_name(
    template_name: str,
    namespace: Mapping[str, Any],
    *,
    is_generic: bool = False,
) -> str:
    var = template_name
    if is_generic and "x" in namespace and namespace["x"] is not None:
        instance_name = _safe_instance_name(namespace["x"])
        if instance_name:
            var = re.sub(r"^x\b", lambda _match: instance_name, var)
    return _substitute_indices(var, namespace)


def canonical_variable_name(
    template_name: str,
    namespace: dict[str, Any],
    *,
    is_generic: bool = False,
) -> str:
    """Resolve generic ``x`` and index ``[i]``..``[n]`` via docassemble's helpers."""
    try:
        from docassemble.base.functions import intrinsic_name_of
        from docassemble.base.parse import substitute_vars_from_user_dict

        return substitute_vars_from_user_dict(
            intrinsic_name_of(template_name, the_user_dict=namespace),
            namespace,
            is_generic=is_generic,
        )
    except ImportError:
        return _fallback_canonical_name(template_name, namespace, is_generic=is_generic)


def _undefine_variables(user_dict: dict[str, Any], *names: str) -> None:
    # Keep the parameter named ``user_dict``: docassemble 1.9.x's
    # ``undefine()`` walks ``frame.f_locals`` looking for ``'user_dict'``
    # (1.10+ reads the active ContextVar instead).
    unique = list(dict.fromkeys(name for name in names if name))
    if not unique:
        return
    try:
        from docassemble.base.functions import undefine

        undefine(*unique)
        return
    except ImportError:
        pass
    for name in unique:
        if name.isidentifier():
            user_dict.pop(name, None)
            continue
        try:
            tree = ast.parse(name, mode="eval").body
            if isinstance(tree, ast.Attribute):
                parent = eval(ast.unparse(tree.value), user_dict)
                if hasattr(parent, tree.attr):
                    delattr(parent, tree.attr)
            elif isinstance(tree, ast.Subscript):
                parent = eval(ast.unparse(tree.value), user_dict)
                key = eval(ast.unparse(tree.slice), user_dict)
                del parent[key]
        except Exception as error:  # noqa: BLE001 - best-effort stub undefine
            logger.debug("could not undefine stub variable %r: %s", name, error)


def resolve_expect_missing_alias(
    var: str,
    bindings: Mapping[str, Any] | None,
    namespace: Mapping[str, Any],
) -> str | None:
    """Resolve an aliased ``--expect-missing`` operand (e.g. ``S.flag``).

    Looks up the leading identifier in the attachment's prepared ``field_data``
    (then ``namespace``) and replaces it with the object's ``instanceName``,
    applying any bound index values (``[i]``..``[n]``) in the suffix.
    """
    cleaned = var.strip()
    match = _LEADING_IDENTIFIER.match(cleaned)
    if match is None:
        return None
    head, tail = match.group(1), match.group(2)

    substituted_tail = _substitute_indices(tail, bindings, namespace, int_only=True)

    target_obj = None
    if bindings and head in bindings:
        target_obj = bindings[head]
    elif head in namespace:
        target_obj = namespace[head]

    instance_name = _safe_instance_name(target_obj) if target_obj is not None else None
    if instance_name and instance_name != head:
        resolved = instance_name + substituted_tail
        return resolved if resolved != cleaned else None
    if substituted_tail != tail:
        return head + substituted_tail
    return None


def _validate_docx_entry(target: AttachmentTarget) -> None:
    entry = target.entry
    if entry.is_manual:
        raise AttachmentInputError(
            f"attachment {target.variable!r} uses manual/manual code and cannot be "
            "rendered as a DOCX template"
        )
    if entry.is_pdf_template:
        raise AttachmentInputError(
            f"attachment {target.variable!r} uses pdf template file and cannot be "
            "rendered as a DOCX template"
        )
    if entry.is_raw:
        raise AttachmentInputError(
            f"attachment {target.variable!r} is a raw attachment, not a DOCX template"
        )
    if not entry.has_docx_template:
        valid_formats = entry.attachment.get("valid_formats")
        if (
            isinstance(valid_formats, (list, tuple))
            and valid_formats
            and set(valid_formats) == {"pdf"}
        ):
            raise PDFConversionUnavailable(PDF_UNAVAILABLE_MESSAGE)
        raise AttachmentInputError(
            f"attachment {target.variable!r} does not define a docx template file"
        )


def assemble_attachment(
    namespace: dict[str, Any],
    target: AttachmentTarget,
) -> AttachmentAssembly:
    """Assemble one attachment target in the already-entered runtime context."""
    _validate_docx_entry(target)

    if target.x_expression is not None:
        try:
            namespace["x"] = eval(target.x_expression, namespace)
        except Exception as error:
            raise AttachmentInputError(
                f"attachment target root {target.x_expression!r} could not be "
                f"resolved: {type(error).__name__}: {error}"
            ) from error
    for idx_name, idx_value in target.index_values:
        namespace[idx_name] = int(idx_value)

    canonical = (
        canonical_variable_name(
            target.entry.variable_name,
            namespace,
            is_generic=target.entry.is_generic,
        )
        or target.variable
    )
    _undefine_variables(namespace, target.variable, canonical)

    question = target.entry.question
    prepared_bindings: dict[str, Any] = {}
    try:
        prepared = question.prepare_attachment(target.entry.attachment, namespace)
        if isinstance(prepared, dict):
            field_data = prepared.get("field_data")
            if isinstance(field_data, dict):
                prepared_bindings = dict(field_data)

        finalized = question.finalize_attachment(
            target.entry.attachment, prepared, namespace
        )
        formats_to_use = finalized.get("formats_to_use") or ()
        if "pdf" in formats_to_use:
            raise RuntimeError(
                "unexpected PDF format remained after attachment finalization"
            )
        files_map = finalized.get("file") or {}
        if "docx" not in files_map or files_map["docx"] is None:
            raise AttachmentInputError(
                f"attachment {canonical!r} did not produce a DOCX output"
            )

        collection = eval(canonical, namespace)
        docx_file = getattr(collection, "docx", None)
        if docx_file is None:
            raise AttachmentInputError(
                f"attachment {canonical!r} collection has no docx format"
            )
        docx_path = Path(docx_file.path()).resolve()
    except AttachmentInputError:
        raise
    except Exception as error:
        raise AttachmentAssemblyError(
            error,
            canonical_variable=canonical,
            prepared_bindings=prepared_bindings,
        ) from error
    raw_filename = getattr(docx_file, "filename", None)
    if isinstance(raw_filename, str) and raw_filename.strip():
        filename = Path(raw_filename.strip()).name
    else:
        base = str(finalized.get("filename") or "attachment").strip() or "attachment"
        filename = base if base.lower().endswith(".docx") else f"{base}.docx"

    template_name = target.entry.first_template or filename
    return AttachmentAssembly(
        canonical_variable=canonical,
        docx_path=docx_path,
        filename=filename,
        template=template_name,
        paragraphs=docx_paragraph_count(docx_path),
    )


__all__ = [
    "INDEX_NAMES",
    "AttachmentAssembly",
    "AttachmentAssemblyError",
    "AttachmentEntry",
    "AttachmentInputError",
    "AttachmentTarget",
    "assemble_attachment",
    "canonical_variable_name",
    "compile_matcher",
    "discover_candidates",
    "enumerate_entries",
    "match_template_entries",
    "match_variable",
    "resolve_automatic_target",
    "resolve_expect_missing_alias",
    "resolve_explicit_target",
    "resolve_targets",
]
