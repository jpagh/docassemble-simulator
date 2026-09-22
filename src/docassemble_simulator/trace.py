"""Screen traces: canonical identity, append-only records, and comparison.

A *screen trace* records the screen outcome each execution operation
produced. Identity derivation and comparison are pure; only the trace
module's reader/writer touch the filesystem.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

__all__ = [
    "TRACE_SCHEMA_VERSION",
    "ComparePolicy",
    "ScreenIdentity",
    "ScreenTrace",
    "TraceComparison",
    "TraceDiff",
    "TraceEntry",
    "TraceError",
    "TraceException",
    "TraceMetadata",
    "TracePhaseComparison",
    "append_trace",
    "compare_traces",
    "error_identity",
    "is_screen_outcome",
    "load_exceptions",
    "load_trace",
    "screen_content",
    "screen_identity",
    "write_trace",
]

#: Version of the JSONL sidecar schema. A mismatch is an input error.
TRACE_SCHEMA_VERSION = 1

EXPLICIT_ID_PREFIX = "ID "

#: Numeric literal indices in resolved paths (``clients[0]``, ``foo['1']``).
_NUMERIC_INDEX = re.compile(r"\[(?:\d+|'\d+'|\"\d+\")\]")

#: A variable path's leading identifier, with the remaining index/attribute tail.
_PATH_ROOT = re.compile(r"^(?P<root>[A-Za-z_][A-Za-z0-9_]*)(?P<rest>.*)$")

#: The generic-object placeholder root docassemble uses for ``x.name`` fields.
GENERIC_PLACEHOLDER_ROOT = "x"

#: ``get_unique_name()`` is ``random_string(12)``: twelve mixed-case letters.
GENERATED_NAME_LENGTH = 12


def _is_generated_instance_name(name: str) -> bool:
    """Whether a root segment looks like a docassemble generated instance name."""
    return (
        len(name) == GENERATED_NAME_LENGTH
        and name.isalpha()
        and any(character.isupper() for character in name)
        and any(character.islower() for character in name)
    )


def _stable_path(path: str) -> str | None:
    """Canonicalize a path, or return None when its root is generated."""
    root, _ = _split_root(path)
    if root is None or _is_generated_instance_name(root):
        return None
    return _canonical_path(path)


def _split_root(path: str) -> tuple[str | None, str]:
    match = _PATH_ROOT.match(path)
    if match is None:
        return None, path
    return match.group("root"), match.group("rest")


def _canonical_path(path: str) -> str:
    """Normalize resolved indices so an occurrence index cannot be identity."""
    return _NUMERIC_INDEX.sub("[i]", path)


@dataclass(frozen=True)
class ScreenIdentity:
    """A stable key for one screen, plus the rule that produced it.

    ``occurrence`` is the 1-based, phase-scoped repetition ordinal assigned
    while recording. Identity derivation itself always yields occurrence 1.
    """

    key: str
    rule: str
    occurrence: int = 1


def screen_content(screen: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Return the order-insensitive field facts compared for a screen.

    Rendered labels and question text are deliberately excluded; the screen
    JSON already carries them for debugging, and full-text comparison reads
    them directly when requested.
    """
    fields = []
    for field in screen.get("fields") or []:
        variable = field.get("variable")
        if not isinstance(variable, str) or not variable:
            continue
        stable = _stable_path(variable)
        if stable is None:
            continue
        described: dict[str, Any] = {
            "variable": stable,
            "type": field.get("type"),
            "visible": field.get("visible"),
            "required": field.get("required"),
        }
        choices = field.get("choices")
        if choices:
            described["choices"] = sorted(
                str(choice.get("value", choice.get("reference_to", "")))
                for choice in choices
            )
        fields.append(described)
    return tuple(sorted(fields, key=lambda item: item["variable"]))


def screen_identity(screen: dict[str, Any]) -> ScreenIdentity:
    """Derive the canonical identity of a screen outcome.

    A failure outcome is never a screen, so an error envelope yields the
    ``error:`` identity instead of a ``screen:error`` category key. Screens
    that carry no ``kind`` are derived as before.
    """
    if "kind" in screen and not is_screen_outcome(screen):
        return error_identity(screen)
    question_name = screen.get("question_name")
    if isinstance(question_name, str) and question_name.startswith(EXPLICIT_ID_PREFIX):
        return ScreenIdentity(
            f"id:{question_name[len(EXPLICIT_ID_PREFIX) :]}", "explicit-id"
        )
    sought = screen.get("sought")
    orig_sought = screen.get("orig_sought")
    if (
        isinstance(sought, str)
        and isinstance(orig_sought, str)
        and sought
        and orig_sought
    ):
        if sought == orig_sought:
            stable = _stable_path(sought)
            if stable is not None:
                return ScreenIdentity(f"var:{stable}", "targeted-variable")
        else:
            sought_root, sought_tail = _split_root(sought)
            orig_root, _ = _split_root(orig_sought)
            if (
                sought_root == GENERIC_PLACEHOLDER_ROOT
                and orig_root
                and orig_root != GENERIC_PLACEHOLDER_ROOT
                and not _is_generated_instance_name(orig_root)
            ):
                return ScreenIdentity(
                    f"generic:{orig_root}{sought_tail}", "generic-object"
                )
            stable = _stable_path(sought)
            if stable is not None and stable == _stable_path(orig_sought):
                return ScreenIdentity(f"list:{stable}", "list-target")
    variables = []
    for field in screen.get("fields") or []:
        variable = field.get("variable")
        if isinstance(variable, str) and variable:
            stable = _stable_path(variable)
            if stable is not None:
                variables.append(stable)
    if variables:
        return ScreenIdentity(f"fields:{','.join(sorted(variables))}", "field-tuple")
    kind = screen.get("kind") or "unknown"
    question_type = screen.get("question_type")
    key = f"screen:{kind}"
    if question_type:
        key += f"/{question_type}"
    return ScreenIdentity(key, "category")


class TraceError(Exception):
    """A screen trace could not be read, written, or compared."""


@dataclass(frozen=True)
class TraceMetadata:
    """First-line identity for a trace sidecar."""

    interview: str
    config_fingerprint: str
    docassemble: str = "unknown"
    assemblyline: str = "not installed"
    simulator: str = "unknown"
    schema: int = TRACE_SCHEMA_VERSION
    order_policy: str = "ordered"
    phase_order: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace": self.schema,
            "interview": self.interview,
            "config_fingerprint": self.config_fingerprint,
            "docassemble": self.docassemble,
            "assemblyline": self.assemblyline,
            "simulator": self.simulator,
            "order_policy": self.order_policy,
            "phase_order": list(self.phase_order),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TraceMetadata:
        schema = data.get("trace")
        if schema != TRACE_SCHEMA_VERSION:
            raise TraceError(
                f"unsupported trace schema {schema!r}; expected {TRACE_SCHEMA_VERSION}"
            )
        phase_order = data.get("phase_order") or []
        if not isinstance(phase_order, list):
            raise TraceError("trace metadata phase_order must be a list")
        return cls(
            interview=str(data.get("interview") or ""),
            config_fingerprint=str(data.get("config_fingerprint") or ""),
            docassemble=str(data.get("docassemble") or "unknown"),
            assemblyline=str(data.get("assemblyline") or "not installed"),
            simulator=str(data.get("simulator") or "unknown"),
            schema=schema,
            order_policy=str(data.get("order_policy") or "ordered"),
            phase_order=tuple(str(item) for item in phase_order),
        )


@dataclass(frozen=True)
class TraceEntry:
    """One recorded execution operation and the screen outcome it produced."""

    seq: int
    operation: str
    phase: str | None
    submitted: dict[str, Any]
    ok: bool
    screen: dict[str, Any] | None
    identity: ScreenIdentity | None
    content: tuple[dict[str, Any], ...] = ()
    error: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "seq": self.seq,
            "operation": self.operation,
            "phase": self.phase,
            "submitted": self.submitted,
            "outcome": "ok" if self.ok else "failed",
            "screen": self.screen,
            "identity": None,
            "content": list(self.content),
        }
        if self.identity is not None:
            payload["identity"] = {
                "key": self.identity.key,
                "rule": self.identity.rule,
                "occurrence": self.identity.occurrence,
            }
        if self.error is not None:
            payload["error"] = self.error
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TraceEntry:
        identity_data = data.get("identity")
        identity = None
        if isinstance(identity_data, dict):
            identity = ScreenIdentity(
                key=str(identity_data.get("key") or ""),
                rule=str(identity_data.get("rule") or ""),
                occurrence=int(identity_data.get("occurrence") or 1),
            )
        elif identity_data is not None:
            raise TraceError("trace entry identity must be an object")
        screen = data.get("screen")
        if screen is not None and not isinstance(screen, dict):
            raise TraceError("trace entry screen must be an object")
        submitted = data.get("submitted") or {}
        if not isinstance(submitted, dict):
            raise TraceError("trace entry submitted assignments must be an object")
        content = data.get("content") or []
        if not isinstance(content, list):
            raise TraceError("trace entry content must be a list")
        return cls(
            seq=int(data.get("seq") or 0),
            operation=str(data.get("operation") or ""),
            phase=data.get("phase"),
            submitted=submitted,
            ok=data.get("outcome") != "failed",
            screen=screen,
            identity=identity,
            content=tuple(content),
            error=data.get("error"),
        )


@dataclass(frozen=True)
class ScreenTrace:
    """A trace sidecar: metadata plus the recorded operations."""

    metadata: TraceMetadata
    entries: tuple[TraceEntry, ...]


def is_screen_outcome(outcome: Any) -> bool:
    """Whether an outcome value may be treated as a screen the user saw.

    An outcome whose ``kind`` is ``error`` is a failure outcome, not a screen;
    capture and identity derivation share this one rule so a stored error can
    never be recorded as a screen.
    """
    return (
        isinstance(outcome, dict)
        and "kind" in outcome
        and outcome.get("kind") != "error"
    )


def error_identity(error: dict[str, Any]) -> ScreenIdentity:
    """Derive an identity for a failure with no active screen.

    The failure kind and sought variable are read from the failure envelope's
    nested ``details`` or from a bare error outcome, so both documented shapes
    of an error produce the same key.
    """
    kind = str(error.get("kind") or "error")
    details = error.get("details")
    details = details if isinstance(details, dict) else {}
    failure_kind = str(details.get("failure_kind") or error.get("failure_kind") or kind)
    if failure_kind == "unresolved-variable":
        variable = details.get("sought_variable") or error.get("sought_variable")
        stable = (
            _stable_path(variable) if isinstance(variable, str) and variable else None
        )
        if stable is not None:
            return ScreenIdentity(f"error:unresolved:{stable}", "error")
        return ScreenIdentity("error:unresolved", "error")
    return ScreenIdentity(f"error:{failure_kind}", "error")


def _screen_and_error(
    screen: dict[str, Any] | None, error: dict[str, Any] | None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Split a recorded outcome into a screen and a failure, if either applies."""
    if screen is not None and not is_screen_outcome(screen):
        return None, error if error is not None else screen
    return screen, error


def _compatible_metadata(expected: TraceMetadata, actual: TraceMetadata) -> None:
    for label, left, right in (
        ("schema", expected.schema, actual.schema),
        ("interview definition", expected.interview, actual.interview),
        ("config fingerprint", expected.config_fingerprint, actual.config_fingerprint),
    ):
        if left != right:
            raise TraceError(
                f"trace {label} mismatch: {left!r} does not match {right!r}; "
                "re-record with a matching interview and configuration"
            )


def _assign_identity(
    screen: dict[str, Any] | None,
    error: dict[str, Any] | None,
    entries: tuple[TraceEntry, ...],
    phase: str | None,
) -> ScreenIdentity | None:
    if screen is not None and "kind" in screen:
        identity = screen_identity(screen)
        if identity.rule == "category":
            base = identity.key
            collisions = sum(
                1
                for entry in entries
                if entry.identity is not None
                and entry.identity.rule == "category"
                and (
                    entry.identity.key == base
                    or entry.identity.key.startswith(f"{base}#")
                )
            )
            if collisions:
                identity = replace(identity, key=f"{base}#{collisions + 1}")
    elif error is not None:
        identity = error_identity(error)
    else:
        return None
    occurrence = 1 + sum(
        1
        for entry in entries
        if entry.identity is not None
        and entry.identity.key == identity.key
        and entry.phase == phase
    )
    return replace(identity, occurrence=occurrence)


def load_trace(path: str | Path) -> ScreenTrace:
    """Read a trace sidecar; malformed content or schema is an input error."""
    trace_path = Path(path)
    try:
        text = trace_path.read_text(encoding="utf-8")
    except OSError as error:
        raise TraceError(f"could not read trace {trace_path}: {error}") from error
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise TraceError(
            f"trace is empty: {trace_path}; record one with a --record execution command"
        )
    try:
        metadata_data = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise TraceError(f"trace metadata is not valid JSON: {error}") from error
    if not isinstance(metadata_data, dict):
        raise TraceError("trace metadata must be a JSON object")
    entries = []
    for number, line in enumerate(lines[1:], start=2):
        try:
            data = json.loads(line)
        except json.JSONDecodeError as error:
            raise TraceError(
                f"trace line {number} is not valid JSON: {error}"
            ) from error
        if not isinstance(data, dict):
            raise TraceError(f"trace line {number} must be a JSON object")
        entries.append(TraceEntry.from_dict(data))
    return ScreenTrace(
        metadata=TraceMetadata.from_dict(metadata_data),
        entries=tuple(entries),
    )


def write_trace(path: str | Path, trace: ScreenTrace) -> None:
    """Rewrite a trace sidecar completely (the ``--update`` write path)."""
    lines = [json.dumps(trace.metadata.as_dict(), sort_keys=True)]
    lines.extend(json.dumps(entry.as_dict(), sort_keys=True) for entry in trace.entries)
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def append_trace(
    path: str | Path,
    metadata: TraceMetadata,
    *,
    operation: str,
    ok: bool,
    screen: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    phase: str | None = None,
    submitted: dict[str, Any] | None = None,
) -> TraceEntry:
    """Append one operation entry, assigning sequence and occurrence values."""
    trace_path = Path(path)
    if trace_path.exists() and trace_path.stat().st_size > 0:
        trace = load_trace(trace_path)
        _compatible_metadata(trace.metadata, metadata)
    else:
        trace = ScreenTrace(metadata=metadata, entries=())
        trace_path.write_text(
            json.dumps(metadata.as_dict(), sort_keys=True) + "\n", encoding="utf-8"
        )
    screen, error = _screen_and_error(screen, error)
    identity = _assign_identity(screen, error, trace.entries, phase)
    entry = TraceEntry(
        seq=len(trace.entries) + 1,
        operation=operation,
        phase=phase,
        submitted=dict(submitted or {}),
        ok=ok,
        screen=screen,
        identity=identity,
        content=screen_content(screen) if screen is not None else (),
        error=error,
    )
    with trace_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry.as_dict(), sort_keys=True) + "\n")
    return entry


@dataclass(frozen=True)
class ComparePolicy:
    """How strictly two traces must agree."""

    order: str = "ordered"
    missing: str = "strict"
    extra: str = "strict"
    full_text: bool = False
    phases: tuple[str, ...] | None = None


@dataclass(frozen=True)
class TraceDiff:
    """One difference between the expected and actual runs."""

    kind: str
    identity: str | None
    position: int | None
    phase: str | None
    detail: str
    blocking: bool = True
    excepted: str | None = None
    expected: Any = None
    actual: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "identity": self.identity,
            "position": self.position,
            "phase": self.phase,
            "detail": self.detail,
            "blocking": self.blocking,
            "excepted": self.excepted,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass(frozen=True)
class TracePhaseComparison:
    """Coverage for one declared phase of a phased comparison."""

    phase: str
    expected_count: int
    actual_count: int
    matched_count: int
    missing_count: int
    extra_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "expected": self.expected_count,
            "actual": self.actual_count,
            "matched": self.matched_count,
            "missing": self.missing_count,
            "extra": self.extra_count,
        }


@dataclass(frozen=True)
class TraceComparison:
    """The verdict and the full coverage report for two traces."""

    matched: bool
    order: str
    expected_count: int
    actual_count: int
    matched_count: int
    missing_count: int
    extra_count: int
    duplicate_count: int
    phases: tuple[TracePhaseComparison, ...]
    diffs: tuple[TraceDiff, ...]
    policy: ComparePolicy

    def as_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "order": self.order,
            "counts": {
                "expected": self.expected_count,
                "actual": self.actual_count,
                "matched": self.matched_count,
                "missing": self.missing_count,
                "extra": self.extra_count,
                "duplicates": self.duplicate_count,
            },
            "phases": [phase.as_dict() for phase in self.phases],
            "diffs": [diff.as_dict() for diff in self.diffs],
            "policy": {
                "order": self.policy.order,
                "missing": self.policy.missing,
                "extra": self.policy.extra,
                "full_text": self.policy.full_text,
                "phases": list(self.policy.phases) if self.policy.phases else None,
            },
        }


@dataclass(frozen=True)
class TraceException:
    """A reviewed, reasoned tolerance for a known difference."""

    interview: str
    phase: str
    identity: str
    category: str
    reason: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.interview, self.phase, self.identity)


EXCEPTION_CATEGORIES = frozenset({"deliberate-error", "capability-boundary"})


def load_exceptions(path: str | Path) -> tuple[TraceException, ...]:
    """Read reviewed exceptions; malformed or duplicate entries are errors."""
    exception_path = Path(path)
    try:
        data = tomllib.loads(exception_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise TraceError(f"could not load trace exceptions: {error}") from error
    rows = data.get("exceptions", [])
    if not isinstance(rows, list):
        raise TraceError("trace exceptions must be an array of tables")
    exceptions = []
    seen = set()
    for row in rows:
        try:
            exception = TraceException(**row)
        except TypeError as error:
            raise TraceError(f"invalid trace exception table: {error}") from error
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                exception.interview,
                exception.phase,
                exception.identity,
                exception.reason,
            )
        ):
            raise TraceError(
                "trace exceptions require interview, phase, identity, and reason"
            )
        if exception.category not in EXCEPTION_CATEGORIES:
            raise TraceError(
                f"trace exception has unsupported category: {exception.category}"
            )
        if exception.identity.startswith("error:fault"):
            raise TraceError(
                f"trace exceptions cannot mask a simulator fault: {exception.identity}"
            )
        if exception.key in seen:
            raise TraceError(
                "overlapping trace exceptions: "
                f"{exception.interview} {exception.phase} {exception.identity}"
            )
        seen.add(exception.key)
        exceptions.append(exception)
    return tuple(exceptions)


def _entry_key(entry: TraceEntry) -> str:
    return entry.identity.key if entry.identity is not None else "<none>"


def _normalize_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return " ".join(value.split())


def _content_signature(entry: TraceEntry, full_text: bool) -> str:
    payload: dict[str, Any] = {"fields": list(entry.content)}
    if full_text:
        question_text, subquestion_text = _screen_texts(entry)
        payload["question_text"] = question_text
        payload["subquestion_text"] = subquestion_text
    return json.dumps(payload, sort_keys=True, default=str)


def _screen_texts(entry: TraceEntry) -> tuple[Any, Any]:
    if entry.screen is None:
        return (None, None)
    return (
        _normalize_text(entry.screen.get("question_text")),
        _normalize_text(entry.screen.get("subquestion_text")),
    )


def _coverage(
    expected_entries: tuple[TraceEntry, ...], actual_entries: tuple[TraceEntry, ...]
) -> tuple[Counter, Counter, int, int, int]:
    expected_counts = Counter(_entry_key(entry) for entry in expected_entries)
    actual_counts = Counter(_entry_key(entry) for entry in actual_entries)
    keys = set(expected_counts) | set(actual_counts)
    matched = sum(min(expected_counts[key], actual_counts[key]) for key in keys)
    missing = sum(max(0, expected_counts[key] - actual_counts[key]) for key in keys)
    extra = sum(max(0, actual_counts[key] - expected_counts[key]) for key in keys)
    return expected_counts, actual_counts, matched, missing, extra


def _phase_of(entries: tuple[TraceEntry, ...]) -> dict[str, str]:
    """First non-null phase seen for each identity, for coverage diffs."""
    mapping: dict[str, str] = {}
    for entry in entries:
        if entry.identity is None or entry.phase is None:
            continue
        mapping.setdefault(entry.identity.key, entry.phase)
    return mapping


def _coverage_diffs(
    expected_counts: Counter,
    actual_counts: Counter,
    policy: ComparePolicy,
    phase_for,
) -> list[TraceDiff]:
    diffs = []
    for key in sorted(set(expected_counts) | set(actual_counts)):
        phase = phase_for(key)
        shortfall = expected_counts.get(key, 0) - actual_counts.get(key, 0)
        if shortfall > 0:
            diffs.append(
                TraceDiff(
                    kind="missing",
                    identity=key,
                    position=None,
                    phase=phase,
                    detail=f"{shortfall} missing occurrence(s)",
                    blocking=policy.missing == "strict",
                    expected=expected_counts[key],
                    actual=actual_counts.get(key, 0),
                )
            )
        surplus = actual_counts.get(key, 0) - expected_counts.get(key, 0)
        if surplus > 0:
            diffs.append(
                TraceDiff(
                    kind="extra",
                    identity=key,
                    position=None,
                    phase=phase,
                    detail=f"{surplus} unexpected occurrence(s)",
                    blocking=policy.extra == "strict",
                    expected=expected_counts.get(key, 0),
                    actual=actual_counts[key],
                )
            )
    return diffs


def _content_diffs(
    expected_entries: tuple[TraceEntry, ...],
    actual_entries: tuple[TraceEntry, ...],
    policy: ComparePolicy,
    phase_for,
) -> list[TraceDiff]:
    expected_by_key: dict[str, list[TraceEntry]] = defaultdict(list)
    actual_by_key: dict[str, list[TraceEntry]] = defaultdict(list)
    for entry in expected_entries:
        expected_by_key[_entry_key(entry)].append(entry)
    for entry in actual_entries:
        actual_by_key[_entry_key(entry)].append(entry)
    diffs = []
    for key in sorted(set(expected_by_key) & set(actual_by_key)):
        paired = min(len(expected_by_key[key]), len(actual_by_key[key]))
        expected_signatures = sorted(
            _content_signature(entry, policy.full_text)
            for entry in expected_by_key[key][:paired]
        )
        actual_signatures = sorted(
            _content_signature(entry, policy.full_text)
            for entry in actual_by_key[key][:paired]
        )
        if expected_signatures != actual_signatures:
            diffs.append(
                TraceDiff(
                    kind="content-mismatch",
                    identity=key,
                    position=None,
                    phase=phase_for(key),
                    detail="matched identity with different field facts",
                    blocking=True,
                    expected=expected_signatures,
                    actual=actual_signatures,
                )
            )
    return diffs


def _ordered_diffs(
    expected_entries: tuple[TraceEntry, ...],
    actual_entries: tuple[TraceEntry, ...],
    policy: ComparePolicy,
) -> list[TraceDiff]:
    diffs = []
    for index in range(max(len(expected_entries), len(actual_entries))):
        position = index + 1
        left = expected_entries[index] if index < len(expected_entries) else None
        right = actual_entries[index] if index < len(actual_entries) else None
        if left is None and right is not None:
            diffs.append(
                TraceDiff(
                    kind="extra",
                    identity=_entry_key(right),
                    position=position,
                    phase=right.phase,
                    detail="unexpected screen at this position",
                    blocking=policy.extra == "strict",
                )
            )
        elif right is None and left is not None:
            diffs.append(
                TraceDiff(
                    kind="missing",
                    identity=_entry_key(left),
                    position=position,
                    phase=left.phase,
                    detail="expected screen missing at this position",
                    blocking=policy.missing == "strict",
                )
            )
        elif left is not None and right is not None:
            if _entry_key(left) != _entry_key(right):
                diffs.append(
                    TraceDiff(
                        kind="order",
                        identity=_entry_key(right),
                        position=position,
                        phase=right.phase,
                        detail=f"expected {_entry_key(left)!r}",
                        blocking=True,
                        expected=_entry_key(left),
                        actual=_entry_key(right),
                    )
                )
            elif _content_signature(left, policy.full_text) != _content_signature(
                right, policy.full_text
            ):
                diffs.append(
                    TraceDiff(
                        kind="content-mismatch",
                        identity=_entry_key(right),
                        position=position,
                        phase=right.phase,
                        detail="matched identity with different field facts",
                        blocking=True,
                        expected=_content_signature(left, policy.full_text),
                        actual=_content_signature(right, policy.full_text),
                    )
                )
    return diffs


def _observed_phase_order(entries: tuple[TraceEntry, ...]) -> list[str]:
    seen: list[str] = []
    for entry in entries:
        if entry.phase is not None and entry.phase not in seen:
            seen.append(entry.phase)
    return seen


def _phased_result(
    expected: ScreenTrace,
    actual: ScreenTrace,
    policy: ComparePolicy,
    exceptions: tuple[TraceException, ...],
) -> TraceComparison:
    declared = (
        list(policy.phases) if policy.phases else list(expected.metadata.phase_order)
    )
    if not declared:
        # The golden is the reference: when nothing declares an order, the
        # order its own entries were recorded in is the declaration.
        declared = _observed_phase_order(expected.entries)
    if not declared:
        raise TraceError(
            "phased comparison requires a declared phase order: pass "
            "--phases NAME,NAME or compare against a golden recorded with "
            "--phase NAME"
        )
    for entry in (*expected.entries, *actual.entries):
        if entry.phase is None:
            raise TraceError(
                "phased comparison requires every entry to carry a phase; "
                "record operations with --phase NAME"
            )
    diffs: list[TraceDiff] = []
    expected_order = _observed_phase_order(expected.entries)
    actual_order = _observed_phase_order(actual.entries)
    if expected_order != declared:
        diffs.append(
            TraceDiff(
                kind="phase-order",
                identity=None,
                position=None,
                phase=None,
                detail=f"expected phases {expected_order!r}, declared {declared!r}",
                blocking=True,
                expected=expected_order,
                actual=declared,
            )
        )
    if actual_order != expected_order:
        diffs.append(
            TraceDiff(
                kind="phase-order",
                identity=None,
                position=None,
                phase=None,
                detail=f"expected phases {expected_order!r}, observed {actual_order!r}",
                blocking=True,
                expected=expected_order,
                actual=actual_order,
            )
        )
    phase_names = list(dict.fromkeys([*declared, *expected_order, *actual_order]))
    phases = []
    expected_total = actual_total = matched_total = missing_total = extra_total = 0
    for phase in phase_names:
        expected_phase = tuple(
            entry for entry in expected.entries if entry.phase == phase
        )
        actual_phase = tuple(entry for entry in actual.entries if entry.phase == phase)
        (
            expected_counts,
            actual_counts,
            matched,
            missing,
            extra,
        ) = _coverage(expected_phase, actual_phase)
        phases.append(
            TracePhaseComparison(
                phase=phase,
                expected_count=len(expected_phase),
                actual_count=len(actual_phase),
                matched_count=matched,
                missing_count=missing,
                extra_count=extra,
            )
        )
        expected_total += len(expected_phase)
        actual_total += len(actual_phase)
        matched_total += matched
        missing_total += missing
        extra_total += extra
        diffs.extend(
            _coverage_diffs(
                expected_counts,
                actual_counts,
                policy,
                lambda key, current=phase: current,
            )
        )
        diffs.extend(
            _content_diffs(
                expected_phase,
                actual_phase,
                policy,
                lambda key, current=phase: current,
            )
        )
    expected_counts, actual_counts, _, _, _ = _coverage(
        expected.entries, actual.entries
    )
    duplicate_count = sum(count - 1 for count in expected_counts.values())
    diffs = _apply_exceptions(diffs, exceptions, expected.metadata.interview)
    matched_verdict = not any(diff.blocking and diff.excepted is None for diff in diffs)
    return TraceComparison(
        matched=matched_verdict,
        order=policy.order,
        expected_count=expected_total,
        actual_count=actual_total,
        matched_count=matched_total,
        missing_count=missing_total,
        extra_count=extra_total,
        duplicate_count=duplicate_count,
        phases=tuple(phases),
        diffs=tuple(diffs),
        policy=policy,
    )


def _matching_exception(
    diff: TraceDiff,
    exceptions: tuple[TraceException, ...],
    interview: str,
) -> TraceException | None:
    for exception in exceptions:
        if exception.interview != interview:
            continue
        if exception.identity != diff.identity:
            continue
        if exception.phase not in {"*", diff.phase}:
            continue
        return exception
    return None


def _apply_exceptions(
    diffs: list[TraceDiff],
    exceptions: tuple[TraceException, ...],
    interview: str,
) -> list[TraceDiff]:
    if not exceptions:
        return diffs
    used: set[tuple[str, str, str]] = set()
    output = []
    for diff in diffs:
        exception = _matching_exception(diff, exceptions, interview)
        if exception is None:
            output.append(diff)
            continue
        used.add(exception.key)
        if diff.kind in {"order", "phase-order"}:
            output.append(diff)
        else:
            output.append(replace(diff, excepted=exception.reason))
    unused = [exception for exception in exceptions if exception.key not in used]
    if unused:
        names = ", ".join(exception.identity for exception in unused)
        raise TraceError(f"unused trace exceptions: {names}; remove the stale entries")
    return output


def compare_traces(
    expected: ScreenTrace,
    actual: ScreenTrace,
    policy: ComparePolicy | None = None,
    exceptions: tuple[TraceException, ...] = (),
) -> TraceComparison:
    """Compare two traces under one order model and report full coverage."""
    policy = policy or ComparePolicy()
    if policy.order not in {"ordered", "unordered", "phased"}:
        raise TraceError(f"unsupported comparison order: {policy.order}")
    for label, value in (("missing", policy.missing), ("extra", policy.extra)):
        if value not in {"strict", "allow"}:
            raise TraceError(f"unsupported {label} policy: {value}")
    _compatible_metadata(expected.metadata, actual.metadata)
    if policy.order == "phased":
        return _phased_result(expected, actual, policy, exceptions)
    expected_counts, actual_counts, matched, missing, extra = _coverage(
        expected.entries, actual.entries
    )
    duplicate_count = sum(count - 1 for count in expected_counts.values())
    if policy.order == "ordered":
        diffs = _ordered_diffs(expected.entries, actual.entries, policy)
    else:
        phases = _phase_of((*expected.entries, *actual.entries))
        diffs = _coverage_diffs(
            expected_counts, actual_counts, policy, lambda key: phases.get(key)
        )
        diffs.extend(
            _content_diffs(
                expected.entries,
                actual.entries,
                policy,
                lambda key: phases.get(key),
            )
        )
    diffs = _apply_exceptions(diffs, exceptions, expected.metadata.interview)
    matched_verdict = not any(diff.blocking and diff.excepted is None for diff in diffs)
    return TraceComparison(
        matched=matched_verdict,
        order=policy.order,
        expected_count=len(expected.entries),
        actual_count=len(actual.entries),
        matched_count=matched,
        missing_count=missing,
        extra_count=extra,
        duplicate_count=duplicate_count,
        phases=(),
        diffs=tuple(diffs),
        policy=policy,
    )
