"""Transactional interview execution behind one typed-operation interface."""

from __future__ import annotations

import ast
import datetime
import difflib
import hashlib
import json
import logging
import pickle
import re
import shlex
import traceback
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import wraps
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from docassemble_simulator._artifacts import capture_published_attachments
from docassemble_simulator._diagnostics import (
    capture_diagnostics,
    is_capture_enabled,
    record_culprit,
    record_seeking,
)
from docassemble_simulator._files import (
    DestinationError,
    ProtectedDirectory,
    ProtectedPath,
    atomic_replace,
    flock,
    validate_destinations,
)
from docassemble_simulator._outcomes import ErrorKind, Failure, Outcome
from docassemble_simulator.catalog import (
    CatalogFailure,
    CompileFailure,
    InterviewCatalog,
    _provenance,
)
from docassemble_simulator.compatibility import (
    AssemblyLineCompatibilityError,
    require_assemblyline_compatibility,
)
from docassemble_simulator.describe import (
    describe_question_result,
    describe_seeking,
    field_variable,
    field_visible,
)

STATE_SCHEMA = 1
_MISSING = object()
_NAMESPACE_COPY_ERRORS = (
    TypeError,
    ValueError,
    RuntimeError,
    AttributeError,
    OSError,
    MemoryError,
    RecursionError,
)


def _activate_runtime(method):
    """Keep runtime resources scoped to each external execution operation."""

    @wraps(method)
    def run_with_runtime(self, *args, **kwargs):
        from docassemble_simulator._runtime import SimulatorRuntime

        with SimulatorRuntime().activate(self.root):
            return method(self, *args, **kwargs)

    return run_with_runtime


ExecutionError = Failure
ExecutionOutcome = Outcome


@dataclass(frozen=True)
class Start:
    pass


@dataclass(frozen=True)
class Status:
    pass


@dataclass(frozen=True)
class Refresh:
    pass


@dataclass(frozen=True)
class Answer:
    assignments: tuple[tuple[str, str], ...]
    code: bool = False
    validate: bool = True
    strict: bool = False
    partial: bool = False
    defaults: bool = False


@dataclass(frozen=True)
class RowAction:
    kind: str
    target: str
    row: int | None = None


@dataclass(frozen=True)
class Seek:
    variable: str
    fresh: bool = False
    activate: bool = False
    trace: bool = False
    auto: bool = False


@dataclass(frozen=True)
class ReplayAutomatic:
    """Replay one automatic seek in one operation-local namespace.

    Empty assignment tuples retain the recorded browser-default submission;
    non-empty tuples are explicit per-step replay overrides.  Keeping the
    seek and all of its submissions in one operation prevents a non-activated
    walk from being persisted between adapter calls.  Expected screen facts,
    the trailing post-walk expectation, and prior identity context are
    immutable trace values, never Working state.
    """

    variable: str
    submissions: tuple[tuple[tuple[str, str], ...], ...] = ()
    fresh: bool = False
    activate: bool = False
    expectations: tuple[Any, ...] = ()
    identity_context: tuple[tuple[str, str, str | None], ...] = ()
    trailing: Any = None


@dataclass(frozen=True)
class Evaluate:
    expression: str


@dataclass(frozen=True)
class Variables:
    contains: str | None = None


@dataclass(frozen=True)
class ExplainResolution:
    """Explain one variable's runtime resolution without committing state."""

    variable: str


@dataclass(frozen=True)
class Execute:
    code: str
    assemble: bool = True


@dataclass(frozen=True)
class SeedState:
    """Explicit local test escape hatch for seeding an interview namespace."""

    code: str
    assemble: bool = False


@dataclass(frozen=True)
class SavedSessionSource:
    pass


@dataclass(frozen=True)
class FreshSource:
    pass


@dataclass(frozen=True)
class SnapshotSource:
    path: Path


@dataclass(frozen=True)
class FixtureSource:
    path: Path


RenderSource = SavedSessionSource | FreshSource | SnapshotSource | FixtureSource


@dataclass(frozen=True)
class _RenderPreparation:
    source: RenderSource
    assemble: bool
    save_snapshot: Path | None
    effect_destinations: tuple[Path, ...]
    protected: tuple[ProtectedPath, ...] = ()


Operation = (
    Start
    | Status
    | Refresh
    | Answer
    | RowAction
    | Seek
    | ReplayAutomatic
    | Evaluate
    | Variables
    | ExplainResolution
    | Execute
    | SeedState
)


class ExecutionFailure(Exception):
    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.kind = ErrorKind(kind)
        self.details = details


def _configuration_mismatch_message(
    root: Path, identity: str, context: dict[str, Any]
) -> str:
    fingerprint = context.get("fingerprint") or "unknown"
    files = context.get("config_files") or []
    override = context.get("config_override")
    command_overrides = context.get("command_overrides") or {}
    command = [
        "docassemble-simulator",
        "--root",
        str(root),
        "--interview",
        identity,
    ]
    if override:
        command.extend(("--config", str(override)))
    if command_overrides.get("offline"):
        command.append("--offline")
    if command_overrides.get("background_actions"):
        command.extend(
            ("--background-actions", str(command_overrides["background_actions"]))
        )
    if command_overrides.get("seek_diagnostics"):
        command.extend(
            ("--seek-diagnostics", str(command_overrides["seek_diagnostics"]))
        )
    command.append("status")
    source = [f"stored configuration fingerprint: {fingerprint}"]
    if files:
        source.append("stored config files: " + ", ".join(map(str, files)))
    if override:
        source.append(f"stored --config override: {override}")
    source.append("resume with: " + shlex.join(command))
    return (
        "a saved session exists under a different effective configuration; "
        + "; ".join(source)
    )


@dataclass(frozen=True)
class _PayloadPolicy:
    unreadable: str
    unsupported: str
    wrong_interview: str
    invalid_blob: str
    namespace_unreadable: str
    invalid_namespace: str


_SAVED_PAYLOAD = _PayloadPolicy(
    unreadable="saved state is unreadable; run `start` again ({error})",
    unsupported="saved state uses an unsupported format; run `start` again",
    wrong_interview="saved state belongs to another interview; run `start` again",
    invalid_blob="saved state is invalid; run `start` again",
    namespace_unreadable="saved namespace is unreadable; run `start` again ({error})",
    invalid_namespace="saved state is invalid; run `start` again",
)
_SNAPSHOT_PAYLOAD = _PayloadPolicy(
    unreadable="could not load snapshot {path}: {error}",
    unsupported="snapshot uses an unsupported format",
    wrong_interview="snapshot belongs to another interview",
    invalid_blob="snapshot does not contain an interview namespace",
    namespace_unreadable="snapshot namespace is unreadable: {error}",
    invalid_namespace="snapshot does not contain an interview namespace",
)


def _read_payload(
    path: Path,
    *,
    identity: str,
    load_namespace: bool,
    policy: _PayloadPolicy,
    display_path: Path | None = None,
) -> dict[str, Any]:
    """Read and validate a saved-session or snapshot payload."""
    try:
        with path.open("rb") as handle:
            payload = pickle.load(handle)
    except Exception as error:
        message = policy.unreadable.format(error=error, path=display_path or path)
        raise ExecutionFailure(ErrorKind.STATE, message) from error

    if not isinstance(payload, dict) or payload.get("schema") != STATE_SCHEMA:
        raise ExecutionFailure(ErrorKind.STATE, policy.unsupported)
    if payload.get("interview") != identity:
        raise ExecutionFailure(ErrorKind.STATE, policy.wrong_interview)

    blob = payload.get("namespace")
    if not isinstance(blob, bytes):
        raise ExecutionFailure(ErrorKind.STATE, policy.invalid_blob)
    if not load_namespace:
        return payload

    try:
        payload["namespace"] = pickle.loads(blob)
    except Exception as error:
        raise ExecutionFailure(
            ErrorKind.STATE,
            policy.namespace_unreadable.format(error=error),
        ) from error
    if not isinstance(payload["namespace"], dict):
        raise ExecutionFailure(ErrorKind.STATE, policy.invalid_namespace)
    return payload


class StateStore:
    """Versioned, per-interview trusted-local state with atomic replacement.

    The session path and payload are qualified by the effective configuration
    fingerprint, so a different config can never load or overwrite the state
    another config produced.
    """

    def __init__(
        self,
        root: Path,
        identity: str,
        config_fingerprint: str = "",
        config_context: dict[str, Any] | None = None,
        session_directory: Path | None = None,
    ):
        self.root = root
        self.identity = identity
        self.config_fingerprint = config_fingerprint
        self.config_context = config_context or {"fingerprint": config_fingerprint}
        # An unset fingerprint reproduces the original interview-only path so
        # zero-config workspaces keep their existing saved sessions.
        if config_fingerprint:
            digest = hashlib.sha256(
                f"{identity}\x00{config_fingerprint}".encode()
            ).hexdigest()[:16]
        else:
            digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
        self.slug = (
            re.sub(r"[^A-Za-z0-9_.-]+", "-", identity).strip("-")[-80:] or "interview"
        )
        self.directory = session_directory or root / ".simulator" / "sessions"
        self.path = self.directory / f"{self.slug}-{digest}.pkl"
        self.lock_path = self.path.with_suffix(".lock")

    @contextmanager
    def lock(self):
        with flock(self.lock_path):
            yield

    def _other_configuration(self) -> tuple[Path, dict[str, Any]] | None:
        """Return a same-interview session stored under another fingerprint."""
        for candidate in sorted(self.directory.glob(f"{self.slug}-*.pkl")):
            if candidate == self.path:
                continue
            try:
                payload = _read_payload(
                    candidate,
                    identity=self.identity,
                    load_namespace=False,
                    policy=_SAVED_PAYLOAD,
                )
            except Exception as error:  # noqa: BLE001 - unrelated or unreadable session files
                logger.debug("ignoring session file %r: %s", candidate, error)
                continue
            if (payload.get("config_fingerprint") or "") != self.config_fingerprint:
                return candidate, payload.get("config_context") or {
                    "fingerprint": payload.get("config_fingerprint") or ""
                }
        return None

    def load(self, *, namespace: bool = True) -> dict[str, Any]:
        if not self.path.exists():
            other = self._other_configuration()
            if other is not None:
                _path, context = other
                raise ExecutionFailure(
                    ErrorKind.STATE,
                    _configuration_mismatch_message(
                        self.root,
                        self.identity,
                        context,
                    ),
                    {"stored_configuration": context},
                )
            raise ExecutionFailure(
                ErrorKind.STATE, "no saved session; run `start` first"
            )
        payload = _read_payload(
            self.path,
            identity=self.identity,
            load_namespace=namespace,
            policy=_SAVED_PAYLOAD,
        )
        stored = payload.get("config_fingerprint") or ""
        if stored != self.config_fingerprint:
            context = payload.get("config_context") or {
                "fingerprint": stored,
            }
            raise ExecutionFailure(
                ErrorKind.STATE,
                _configuration_mismatch_message(self.root, self.identity, context),
                {"stored_configuration": context},
            )
        return payload

    def reset(self) -> None:
        """Remove this store's payload; the advisory lock itself is harmless."""
        self.path.unlink(missing_ok=True)

    def save(
        self,
        namespace: dict[str, Any],
        outcome: dict[str, Any],
        active_seek: str | None = None,
    ) -> None:
        payload = {
            "schema": STATE_SCHEMA,
            "interview": self.identity,
            "config_fingerprint": self.config_fingerprint,
            "config_context": self.config_context,
            "namespace": pickle.dumps(_picklable_view(namespace)),
            "outcome": _jsonable(outcome),
            "active_seek": active_seek,
        }
        _atomic_pickle(self.path, payload, lock_destination=False)

    def save_snapshot(self, path: Path, namespace: dict[str, Any]) -> None:
        payload = {
            "schema": STATE_SCHEMA,
            "interview": self.identity,
            "namespace": pickle.dumps(_picklable_view(namespace)),
        }
        _atomic_pickle(path.expanduser().resolve(), payload, lock_destination=True)

    def load_snapshot(self, path: Path) -> dict[str, Any]:
        return _read_payload(
            path.expanduser(),
            identity=self.identity,
            load_namespace=True,
            policy=_SNAPSHOT_PAYLOAD,
            display_path=path,
        )["namespace"]


@dataclass(frozen=True)
class ExecutionScope:
    """Immutable identity and durable-state locations; never runtime objects."""

    root: Path
    interview: str
    config_fingerprint: str
    session_root: Path
    session: Path

    @property
    def session_id(self) -> str:
        return self.session.stem


class InterviewExecution:
    """Run complete operations; mutable runtime state never crosses this interface."""

    def __init__(
        self,
        root: str | Path,
        selector: str | None = None,
        *,
        config_fingerprint: str = "",
        config_context: dict[str, Any] | None = None,
        session_directory: Path | None = None,
        cache_interview: bool = False,
        compile_failure: CompileFailure | None = None,
    ):
        self.root = Path(root).resolve()
        self._catalog = InterviewCatalog(
            self.root,
            selector,
            cache_compiled=cache_interview,
            compile_failure=compile_failure,
        )
        self._identity = self._catalog.identity
        self._store = StateStore(
            self.root,
            self._identity,
            config_fingerprint,
            config_context,
            session_directory=session_directory,
        )

    @property
    def scope(self) -> ExecutionScope:
        """Describe this execution's project/interview/configuration boundary."""
        return ExecutionScope(
            self.root,
            self._identity,
            self._store.config_fingerprint,
            self._store.directory,
            self._store.path,
        )

    @_activate_runtime
    def run(self, operation: Operation) -> ExecutionOutcome:
        with (
            capture_diagnostics() as diagnostics,
            capture_published_attachments() as attachments,
        ):
            try:
                if isinstance(operation, Status):
                    outcome = ExecutionOutcome(
                        True, self._store.load(namespace=False)["outcome"]
                    )
                elif isinstance(operation, Start):
                    outcome = self._mutate(lambda: self._start())
                elif isinstance(operation, Refresh):
                    outcome = self._mutate(lambda: self._refresh())
                elif isinstance(operation, Answer):
                    outcome = self._mutate(lambda: self._answer(operation))
                elif isinstance(operation, RowAction):
                    outcome = self._mutate(lambda: self._row_action(operation))
                elif isinstance(operation, Seek):
                    outcome = self._seek_operation(operation)
                elif isinstance(operation, ReplayAutomatic):
                    outcome = self._replay_automatic_operation(operation)
                elif isinstance(operation, Evaluate):
                    outcome = self._evaluate(operation)
                elif isinstance(operation, Variables):
                    outcome = self._variables(operation)
                elif isinstance(operation, ExplainResolution):
                    outcome = self._explain_resolution(operation)
                elif isinstance(operation, Execute):
                    outcome = self._mutate(lambda: self._execute(operation))
                elif isinstance(operation, SeedState):
                    outcome = self._mutate(lambda: self._seed_state(operation))
                else:
                    raise TypeError(f"unknown operation: {type(operation).__name__}")
            except ExecutionFailure as error:
                outcome = ExecutionOutcome(
                    False, error=ExecutionError(error.kind, str(error), error.details)
                )
            except AssemblyLineCompatibilityError as error:
                outcome = _compatibility_failure_outcome(error)
            except Exception as error:
                unresolved = _unresolved_variable(error)
                if unresolved is not None:
                    culprit = _unresolved_culprit(error)
                    details = {"sought_variable": unresolved}
                    if culprit is not None:
                        details["culprit"] = culprit
                    outcome = ExecutionOutcome(
                        False,
                        error=ExecutionError(
                            ErrorKind.UNRESOLVED_VARIABLE,
                            str(error),
                            details,
                        ),
                    )
                elif _is_compile_failure(error):
                    outcome = _compile_failure_outcome(error)
                elif isinstance(
                    error,
                    (
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
                    ),
                ):
                    logger.exception("unhandled execution fault")
                    outcome = ExecutionOutcome(
                        False,
                        error=ExecutionError(
                            ErrorKind.FAULT, f"{type(error).__name__}: {error}"
                        ),
                    )
                else:
                    raise
        return replace(
            outcome,
            diagnostics=tuple(diagnostics),
            attachments=tuple(attachments),
        )

    def _mutate(self, action: Callable[[], Any]) -> ExecutionOutcome:
        # Run the runtime compatibility gate before acquiring the session lock:
        # an incompatible environment must not create or mutate Saved state.
        require_assemblyline_compatibility()
        with self._store.lock():
            result = action()
        if isinstance(result, dict) and result.get("kind") == "error":
            return ExecutionOutcome(
                False,
                error=ExecutionError(
                    result.get("failure_kind", ErrorKind.EXECUTION),
                    result.get("message", "interview assembly failed"),
                    result,
                ),
            )
        return ExecutionOutcome(True, result)

    @contextmanager
    def _prepared_operation(self, namespace: dict[str, Any]):
        """Own compile, context entry, and namespace preparation once."""
        interview = self._catalog._compile()
        previous_debug = getattr(interview, "debug", _MISSING)
        forced_debug = is_capture_enabled() and previous_debug is not True
        if forced_debug:
            interview.debug = True
        try:
            with _interview_context(interview, namespace) as status:
                self._prepare(interview, namespace)
                yield interview, status
        finally:
            if forced_debug:
                if previous_debug is _MISSING:
                    del interview.debug
                else:
                    interview.debug = previous_debug

    def reset(self) -> None:
        """Discard this worker's saved session without changing its scope."""
        with self._store.lock():
            self._store.reset()

    def ensure_current(self, identity: str | None = None) -> CompileFailure | None:
        """Refresh the compiled interview a non-operation caller is about to read.

        The worker's catalog and trace adapter requests compile outside an
        execution operation, through their own catalog instances, so they ask
        this execution to run the source reload check
        (:meth:`InterviewCatalog.ensure_current`) before they compile. That
        keeps this execution's catalog, which owns the worker's watches, the
        process's record of what was compiled from which source. A refresh that
        could not compile returns its :class:`CompileFailure` for the caller to
        hand to the catalog or execution that would otherwise compile the same
        broken source again.
        """
        return self._catalog.ensure_current(identity)

    def _start(self):
        namespace = _fresh_namespace()
        with self._prepared_operation(namespace) as (interview, status):
            outcome = _assemble(interview, namespace, status)
        self._store.save(namespace, outcome)
        return outcome

    def _refresh(self):
        payload = self._store.load()
        namespace = payload["namespace"]
        with self._prepared_operation(namespace) as (interview, status):
            active = payload.get("active_seek")
            outcome = (
                _seek(interview, namespace, status, active, False)
                if active
                else _assemble(interview, namespace, status)
            )
        self._store.save(namespace, outcome, active)
        return outcome

    def _row_action(self, operation: RowAction):
        payload = self._store.load()
        screen = payload.get("outcome") or {}
        requested = {"kind": operation.kind, "target": operation.target}
        if operation.row is not None:
            requested["row"] = operation.row
        valid_row = (
            operation.row is None
            if operation.kind == "add"
            else (type(operation.row) is int and operation.row >= 0)
        )
        if (
            operation.kind not in {"add", "delete"}
            or not valid_row
            or requested not in screen.get("row_actions", [])
        ):
            raise ExecutionFailure(
                ErrorKind.ANSWER_INPUT,
                "row action is not available on the saved screen",
            )
        namespace = payload["namespace"]
        with self._prepared_operation(namespace) as (interview, status):
            arguments = {"list": operation.target}
            if operation.kind == "delete":
                arguments["item"] = str(operation.row)
            status.current_info.update(
                {
                    "action": "_da_list_add"
                    if operation.kind == "add"
                    else "_da_list_remove",
                    "arguments": arguments,
                }
            )
            outcome = _assemble(interview, namespace, status)
        if outcome.get("kind") != "error":
            self._store.save(namespace, outcome)
        return outcome

    def _answer(self, operation: Answer):
        if not operation.assignments and not operation.defaults:
            raise ExecutionFailure(
                ErrorKind.INPUT, "no VAR=VALUE assignments were provided"
            )
        payload = self._store.load()
        namespace = payload["namespace"]
        screen = payload.get("outcome") or {}
        if screen.get("kind") not in {"question", "continue"}:
            raise ExecutionFailure(
                ErrorKind.INPUT, "the saved outcome is not an answerable screen"
            )
        blank_submissions = _blank_submissions(operation)
        dispositions = []
        submission_snapshot = _snapshot_namespace(namespace)
        with self._prepared_operation(namespace) as (interview, status):
            errors = _apply_assignments(
                namespace,
                screen,
                operation.assignments,
                operation.code,
                allowed=_answerable_fields(screen),
                dispositions=dispositions,
            )
            if errors:
                try:
                    details = {"errors": errors, "assignments": dispositions}
                    # Keep the established active-field and closest-name guidance
                    # on every early rejection, including parse errors.
                    details.update(_unknown_field_details(screen, dispositions))
                    details.update(
                        _early_rejection_required_guidance(
                            namespace,
                            screen,
                            blank_submissions,
                            interview,
                        )
                    )
                finally:
                    # A rejected submission is not a partial commit. Guidance
                    # may inspect valid assignments and defaults in this
                    # operation-local namespace, but it must not leave them in
                    # Working state or let them reach persistence.
                    _restore_namespace(namespace, submission_snapshot)
                raise ExecutionFailure(
                    ErrorKind.VALIDATION
                    if any(d["disposition"] == "validation error" for d in dispositions)
                    else ErrorKind.ANSWER_INPUT,
                    "one or more answers could not be applied",
                    details,
                )
            if not operation.partial:
                # The whole screen is submitted: browser defaults and blanks
                # for optional fields, then the required gate.
                submission_screen = _submit_screen_fields(namespace, screen, interview)
            else:
                submission_screen = _submission_screen(interview, namespace, screen)
            violations = _required_violations(
                namespace, submission_screen, blank_submissions
            )
            if violations and not operation.partial:
                raise ExecutionFailure(
                    ErrorKind.VALIDATION,
                    "screen rejected; all answers were discarded",
                    {
                        "errors": [
                            _required_message(variable, reason)
                            for variable, reason in violations
                        ],
                        "warnings": [],
                        "assignments": dispositions,
                        "missing_required": [
                            variable for variable, _reason in violations
                        ],
                        "active_fields": _active_field_names(screen),
                    },
                )
            validation = (
                _validate(interview, namespace, screen)
                if operation.validate
                else {"errors": [], "warnings": []}
            )
            if operation.partial and operation.validate:
                validation["warnings"].extend(
                    _required_message(variable, reason)
                    for variable, reason in violations
                )
            if operation.strict:
                validation["errors"].extend(validation["warnings"])
                validation["warnings"] = []
            if validation["errors"]:
                validation["assignments"] = dispositions
                raise ExecutionFailure(
                    ErrorKind.VALIDATION,
                    "screen rejected; all answers were discarded",
                    validation,
                )
            _mark_answered(interview, namespace, screen.get("question_name"))
            outcome = _assemble(interview, namespace, status)
        if dispositions:
            outcome["assignments"] = dispositions
        if validation["warnings"]:
            outcome["warnings"] = validation["warnings"]
        self._store.save(namespace, outcome)
        return outcome

    def _seek_operation(self, operation: Seek):
        def work():
            namespace = (
                _fresh_namespace()
                if operation.fresh
                else self._store.load()["namespace"]
            )
            with self._prepared_operation(namespace) as (interview, status):
                outcome = (
                    _seek_auto(
                        interview,
                        namespace,
                        status,
                        operation.variable,
                        operation.trace,
                    )
                    if operation.auto
                    else _seek(
                        interview,
                        namespace,
                        status,
                        operation.variable,
                        operation.trace,
                    )
                )
            if operation.activate:
                self._store.save(namespace, outcome, operation.variable)
            return outcome

        if operation.activate:
            return self._mutate(work)
        try:
            result = work()
            if isinstance(result, dict) and result.get("kind") == "error":
                return ExecutionOutcome(
                    False,
                    error=ExecutionError(
                        result.get("failure_kind", ErrorKind.EXECUTION),
                        result.get("message", "interview assembly failed"),
                        result,
                    ),
                )
            return ExecutionOutcome(True, result)
        except ExecutionFailure as error:
            return ExecutionOutcome(
                False, error=ExecutionError(error.kind, str(error), error.details)
            )
        except Exception as error:
            if _unresolved_variable(error) is not None:
                raise
            if not isinstance(
                error,
                (
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
                ),
            ):
                raise
            logger.exception("seek failed")
            return ExecutionOutcome(
                False,
                error=ExecutionError(
                    ErrorKind.SEEK, f"{type(error).__name__}: {error}"
                ),
            )

    def _replay_automatic_operation(self, operation: ReplayAutomatic):
        """Replay an automatic seek without splitting its local state.

        The command adapter may need to verify each pre-submission screen, but
        a browser-shaped automatic walk is still one execution operation.  In
        particular, a non-activated walk must not save its temporary target
        context before the next submission or observation.
        """

        def work():
            namespace = (
                _fresh_namespace()
                if operation.fresh
                else self._store.load()["namespace"]
            )
            with self._prepared_operation(namespace) as (interview, status):
                outcome = _seek_auto(
                    interview,
                    namespace,
                    status,
                    operation.variable,
                    False,
                    operation.submissions,
                    operation.expectations,
                    operation.identity_context,
                    operation.trailing,
                )
            if operation.activate and not outcome.get("replay_divergence"):
                self._store.save(namespace, outcome, operation.variable)
            return outcome

        if operation.activate:
            return self._mutate(work)
        try:
            result = work()
            if isinstance(result, dict) and result.get("kind") == "error":
                return ExecutionOutcome(
                    False,
                    error=ExecutionError(
                        result.get("failure_kind", ErrorKind.EXECUTION),
                        result.get("message", "interview assembly failed"),
                        result,
                    ),
                )
            return ExecutionOutcome(True, result)
        except ExecutionFailure as error:
            return ExecutionOutcome(
                False, error=ExecutionError(error.kind, str(error), error.details)
            )
        except Exception as error:
            if _unresolved_variable(error) is not None:
                raise
            if not isinstance(
                error,
                (
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
                ),
            ):
                raise
            logger.exception("automatic replay failed")
            return ExecutionOutcome(
                False,
                error=ExecutionError(
                    ErrorKind.SEEK, f"{type(error).__name__}: {error}"
                ),
            )

    def _explain_resolution(self, operation: ExplainResolution):
        """Run the saved-session catalog explanation read-only.

        The saved namespace is prepared inside the normal execution context,
        but this operation never calls ``save``.  With no session, catalog
        inspection remains static and does not create one.
        """
        namespace = None
        try:
            namespace = self._store.load()["namespace"]
        except ExecutionFailure as error:
            if error.kind != ErrorKind.STATE or str(error) != (
                "no saved session; run `start` first"
            ):
                raise

        if namespace is None:
            return ExecutionOutcome(
                True,
                self._resolution_report(self._catalog.why(operation.variable)),
            )

        with self._prepared_operation(namespace) as (interview, _status):
            language = None
            try:
                from docassemble.base.functions import get_language

                language = get_language()
            except (ImportError, AttributeError, RuntimeError):
                pass
            report = self._catalog.why(
                operation.variable,
                namespace,
                interview=interview,
                language=language,
            )
        return ExecutionOutcome(True, self._resolution_report(report))

    def _resolution_report(self, outcome):
        """Keep catalog identity in the plain execution result."""
        interview = getattr(outcome, "interview", None) or self._identity
        return outcome.result | {"interview": interview}

    def _evaluate(self, operation: Evaluate):
        payload = self._store.load()
        namespace = payload["namespace"]
        with self._prepared_operation(namespace):
            try:
                value = eval(operation.expression, namespace)
            except Exception as error:
                raise ExecutionFailure(
                    ErrorKind.EXECUTION, f"{type(error).__name__}: {error}"
                ) from error
            # Gatherable objects (DADict/DAList) trigger gathering in repr(),
            # which needs the active runtime context.
            rendered = _safe_repr(value)
        return ExecutionOutcome(
            True, {"expression": operation.expression, "value": rendered}
        )

    def _variables(self, operation: Variables):
        payload = self._store.load()
        namespace = payload["namespace"]
        values = {}
        with self._prepared_operation(namespace):
            for name in sorted(
                key for key in namespace if key not in {"_internal", "__builtins__"}
            ):
                if (
                    operation.contains
                    and operation.contains.lower() not in name.lower()
                ):
                    continue
                try:
                    values[name] = _safe_repr(eval(name, namespace))
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
                    values[name] = f"<{type(error).__name__}: {str(error)[:80]}>"
        return ExecutionOutcome(True, values)

    def _seed_state(self, operation: SeedState):
        return self._execute(
            Execute(operation.code, operation.assemble), state_seed=True
        )

    def _execute(self, operation: Execute, *, state_seed: bool = False):
        payload = self._store.load()
        namespace = payload["namespace"]
        with self._prepared_operation(namespace) as (interview, status):
            try:
                __builtins__["exec"](operation.code, namespace)
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
                raise ExecutionFailure(
                    ErrorKind.EXECUTION, f"{type(error).__name__}: {error}"
                ) from error
            outcome = (
                _assemble(interview, namespace, status)
                if operation.assemble
                else {"kind": "executed"}
            )
            if state_seed:
                outcome = dict(outcome)
                outcome["state_seeded"] = True
                outcome["operation"] = "state-seed"
                if not operation.assemble:
                    outcome["kind"] = "state-seeded"
        self._store.save(
            namespace,
            outcome,
            payload.get("active_seek") if not operation.assemble else None,
        )
        return outcome

    @_activate_runtime
    def _with_render_state(
        self,
        preparation: _RenderPreparation,
        action: Callable[[dict[str, Any]], Any],
    ) -> ExecutionOutcome:
        """Invoke render's private action while prepared context remains active."""
        with (
            capture_diagnostics() as diagnostics,
            capture_published_attachments() as attachments,
        ):
            outcome = self._with_render_state_impl(preparation, action)
        return replace(
            outcome,
            diagnostics=tuple(diagnostics),
            attachments=tuple(attachments),
        )

    def _with_render_state_impl(
        self,
        preparation: _RenderPreparation,
        action: Callable[[dict[str, Any]], Any],
    ) -> ExecutionOutcome:
        try:
            protected = preparation.protected + (
                ProtectedDirectory(
                    self._store.directory,
                    "saved-session storage",
                ),
            )
            try:
                validate_destinations(preparation.effect_destinations, protected)
            except DestinationError as error:
                raise ExecutionFailure(ErrorKind.INPUT, str(error)) from error
            source = preparation.source
            if isinstance(source, SavedSessionSource):
                namespace = self._store.load()["namespace"]
            elif isinstance(source, FreshSource):
                namespace = _fresh_namespace()
            elif isinstance(source, SnapshotSource):
                namespace = self._store.load_snapshot(source.path)
            elif isinstance(source, FixtureSource):
                namespace = _fresh_namespace()
            else:
                raise ExecutionFailure(ErrorKind.INPUT, "invalid render source")
            with self._prepared_operation(namespace) as (interview, status):
                if isinstance(source, FixtureSource):
                    try:
                        __builtins__["exec"](
                            source.path.read_text(encoding="utf-8"), namespace
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
                        raise ExecutionFailure(
                            ErrorKind.EXECUTION,
                            f"fixture failed: {type(error).__name__}: {error}",
                        ) from error
                elif preparation.assemble:
                    outcome = _assemble(interview, namespace, status)
                    if outcome.get("kind") == "error":
                        raise ExecutionFailure(
                            outcome.get("failure_kind", ErrorKind.EXECUTION),
                            outcome.get("message", "assembly failed"),
                            outcome,
                        )
                if preparation.save_snapshot:
                    self._store.save_snapshot(preparation.save_snapshot, namespace)
                return ExecutionOutcome(True, action(namespace))
        except AssemblyLineCompatibilityError as error:
            return _compatibility_failure_outcome(error)
        except ExecutionFailure as error:
            return ExecutionOutcome(
                False, error=ExecutionError(error.kind, str(error), error.details)
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
            logger.exception("render state failed")
            return ExecutionOutcome(
                False,
                error=ExecutionError(
                    ErrorKind.FAULT, f"{type(error).__name__}: {error}"
                ),
            )
        except Exception as error:
            if _is_compile_failure(error):
                return _compile_failure_outcome(error)
            raise

    def _prepare(self, interview, namespace: dict[str, Any]) -> None:
        """Restore server request names, then run authored package adapters."""
        try:
            populate = getattr(interview, "populate_non_pickleable", None)
            if populate is not None:
                populate(namespace)
            else:
                interview.load_util(namespace)
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
            raise ExecutionFailure(
                ErrorKind.EXECUTION,
                f"namespace rehydration failed: {type(error).__name__}: {error}",
            ) from error
        config = self.root / ".config" / "simulator" / "config.py"
        if config.exists():
            try:
                __builtins__["exec"](config.read_text(encoding="utf-8"), namespace)
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
                raise ExecutionFailure(
                    ErrorKind.EXECUTION,
                    f"simulator config {config} failed: {type(error).__name__}: {error}",
                ) from error
        _register_global_roots(namespace)


def _fresh_namespace() -> dict[str, Any]:
    import copy

    from docassemble.base import parse
    from docassemble.base.util import DAObject

    # INITIAL_DICT is the server's contract.  get_initial_dict() is preferred
    # by modern releases because it deep-copies that value, while older test
    # and runtime shims expose only the constant.
    if hasattr(parse, "INITIAL_DICT"):
        initial = copy.deepcopy(parse.INITIAL_DICT)
    else:
        getter = getattr(parse, "get_initial_dict", None)
        if getter is None:
            raise RuntimeError("docassemble parse module has no INITIAL_DICT")
        initial = getter()
    internal = initial["_internal"]
    defaults = {
        "gather": [],
        "modtime": datetime.datetime.now(tz=datetime.UTC),
        "tracker": 0,
        "steps": 1,
        "question_queue": [],
        "step_order": 0,
        "action": None,
        "args": {},
        "answered": set(),
        "event_stack": {},
        "tasks": {},
        "informed": {},
        "dirty": {},
    }
    internal.update({key: value for key, value in defaults.items() if key in internal})
    nav = initial.get("nav") or DAObject(instanceName="nav", sections=None)
    return {
        "_internal": internal,
        # ``user`` is an interview namespace root, not the server's
        # authentication record. The latter lives in InterviewStatus.current_info;
        # pre-seeding this name as a dict prevents ``objects: user`` from creating
        # its declared DAObject.
        "session": DAObject(instanceName="session"),
        "M": DAObject(instanceName="M"),
        "nav": nav,
        "url_args": initial.get("url_args", {}),
    }


def _build_status():
    from docassemble.base.parse import InterviewStatus

    return InterviewStatus(
        current_info={
            "user": {
                "session_uid": "dasimulator",
                "is_authenticated": True,
                "roles": ["user"],
                "id": 1,
                "email": "agent@localhost",
                "device_id": "dasimulator",
                "the_user_id": "1",
                "theid": "1",
            },
            "session": "dasimulator",
            "method": "GET",
            "clientip": "127.0.0.1",
            "headers": {"User-Agent": "Mozilla/5.0 (Simulator; X11; Linux x86_64)"},
        }
    )


@contextmanager
def _interview_context(interview, namespace):
    from docassemble.base.functions import this_thread

    from docassemble_simulator._runtime import runtime_context

    status = _build_status()
    source = getattr(interview, "source", None)
    status.current_info.update(
        {"yaml_filename": getattr(source, "path", None), "url": None}
    )
    with runtime_context(namespace):
        this_thread.current_info = status.current_info
        this_thread.interview = interview
        this_thread.interview_status = status
        if source is not None:
            this_thread.current_package = getattr(source, "package", None)
        this_thread.internal = namespace.get("_internal", {})
        # The simulator-owned foreground background-action fallback uses this
        # explicit marker; docassemble itself keeps the dict in a ContextVar.
        this_thread.current_dict = namespace
        _register_global_roots(namespace)
        try:
            yield status
        finally:
            record_seeking(getattr(status, "seeking", None))
            try:
                del this_thread.current_dict
            except AttributeError:
                pass


def _assemble(interview, namespace, status):
    from docassemble_simulator._runtime import status_field

    try:
        interview.assemble(namespace, interview_status=status)
    except Exception as error:  # noqa: BLE001 - assembly may raise any interview-authored exception
        from docassemble.base.error import DAErrorNoEndpoint

        if isinstance(error, DAErrorNoEndpoint):
            return {"kind": "finished", "message": _first_line(error)}
        unresolved = _unresolved_variable(error)
        result = {
            "kind": "error",
            "error_type": type(error).__name__,
            "message": _user_facing_error(error),
            "traceback_tail": "\n".join(
                traceback.format_exc().strip().splitlines()[-8:]
            ),
        }
        if unresolved is not None:
            result["failure_kind"] = ErrorKind.UNRESOLVED_VARIABLE
            result["sought_variable"] = unresolved
            culprit = _unresolved_culprit(error)
            if culprit is not None:
                result["culprit"] = culprit
        return result
    question = getattr(status, "question", None)
    if question is not None and getattr(question, "question_type", None) == "continue":
        return {
            "kind": "continue",
            "message": status_field(status, "question_text")
            or "continue screen reached",
            "question_name": getattr(question, "name", None),
        }
    if question is not None:
        result = {
            "type": "question",
            "question_text": status_field(status, "question_text"),
            "subquestion_text": status_field(status, "subquestion_text"),
            "continue_label": status_field(status, "continue_label"),
            "sought": status.sought,
            "orig_sought": status.orig_sought,
            "question": question,
            "selectcompute": getattr(status, "selectcompute", {}),
            "extras": getattr(status, "extras", {}),
            "defaults": getattr(status, "defaults", {}),
            "labels": getattr(status, "labels", {}),
        }
        return describe_question_result(result, namespace)
    return {
        "kind": "continue",
        "message": "mandatory code completed without reaching a screen",
    }


def _auto_screen_summary(screen):
    return {
        "question_name": screen.get("question_name"),
        "sought": screen.get("sought"),
        "orig_sought": screen.get("orig_sought"),
        "variables": [
            field.get("variable")
            for field in screen.get("fields") or []
            if field.get("variable")
        ],
        "screen": screen,
    }


def _snapshot_namespace(namespace):
    """Copy an operation-local namespace for one transactional submission.

    Interview namespaces normally contain docassemble objects as well as a
    few runtime helpers.  A deep copy gives validation code the same rollback
    boundary as an ordinary answer, while the per-value fallback keeps an
    unusual uncopyable helper from turning auto-seek into a new failure mode.
    """
    import copy

    try:
        return copy.deepcopy(namespace)
    except _NAMESPACE_COPY_ERRORS:
        snapshot = {}
        for key, value in namespace.items():
            try:
                snapshot[key] = copy.deepcopy(value)
            except _NAMESPACE_COPY_ERRORS:
                snapshot[key] = value
        return snapshot


def _restore_namespace(namespace, snapshot):
    namespace.clear()
    namespace.update(snapshot)
    try:
        from docassemble.base.functions import this_thread

        this_thread.current_dict = namespace
        this_thread.internal = namespace.get("_internal", {})
    except (ImportError, AttributeError):
        # The runtime context is not available in isolated unit-level callers.
        pass


def _auto_submit_screen(
    interview,
    namespace,
    status,
    screen,
    assignments: tuple[tuple[str, str], ...] = (),
):
    """Submit one screen exactly as an untouched browser form would.

    Defaults, browser blanks, and validation code all run inside a transaction.
    A rejected screen is still returned to the caller, but none of those
    attempted mutations become the next auto-seek state.  Replay may provide
    assignments for one step; ordinary automatic seeking passes none and uses
    browser defaults.
    """
    snapshot = _snapshot_namespace(namespace)
    try:
        if assignments:
            dispositions = []
            errors = _apply_assignments(
                namespace,
                screen,
                assignments,
                False,
                allowed=_answerable_fields(screen),
                dispositions=dispositions,
            )
            if errors:
                _restore_namespace(namespace, snapshot)
                return None, {
                    "reason": "validation failed"
                    if any(d["disposition"] == "validation error" for d in dispositions)
                    else "answer failed",
                    "submitted": False,
                    "errors": errors,
                    "assignments": dispositions,
                    "screen": screen,
                }
        submission_screen = _submit_screen_fields(namespace, screen, interview)
        violations = _required_violations(namespace, submission_screen, set())
        if violations:
            _restore_namespace(namespace, snapshot)
            return None, {
                "reason": "required fields missing",
                "submitted": False,
                "missing_required": [variable for variable, _reason in violations],
                "errors": [
                    _required_message(variable, reason)
                    for variable, reason in violations
                ],
                "screen": screen,
            }
        validation = _validate(interview, namespace, screen)
        if validation["errors"]:
            _restore_namespace(namespace, snapshot)
            return None, {
                "reason": "validation failed",
                "submitted": False,
                "errors": validation["errors"],
                "warnings": validation["warnings"],
                "screen": screen,
            }
        _mark_answered(interview, namespace, screen.get("question_name"))
        outcome = _assemble(interview, namespace, status)
        if outcome.get("kind") == "error":
            return outcome, {
                "reason": "flow error",
                "submitted": True,
                "error": outcome,
                "screen": screen,
            }
        if validation["warnings"]:
            outcome["warnings"] = validation["warnings"]
        return outcome, None
    except BaseException:
        _restore_namespace(namespace, snapshot)
        raise


def _variable_is_resolved(namespace, variable):
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
    ):
        return False
    return True


def _auto_trace_error(stop):
    """Turn an automatic-submission stop into a replayable failure payload."""
    if not stop:
        return None
    reason = stop.get("reason")
    if reason == "answer failed":
        return {
            "kind": "answer-input",
            "message": "one or more answers could not be applied",
            "details": {
                "errors": list(stop.get("errors") or []),
                "assignments": list(stop.get("assignments") or []),
                "auto_stop": reason,
            },
        }
    if reason == "required fields missing":
        return {
            "kind": "validation",
            "message": "screen rejected; all answers were discarded",
            "details": {
                "errors": list(stop.get("errors") or []),
                "missing_required": list(stop.get("missing_required") or []),
                "auto_stop": reason,
            },
        }
    if reason == "validation failed":
        return {
            "kind": "validation",
            "message": "screen rejected; all answers were discarded",
            "details": {
                "errors": list(stop.get("errors") or []),
                "warnings": list(stop.get("warnings") or []),
                "auto_stop": reason,
            },
        }
    error = stop.get("error")
    if isinstance(error, dict):
        # ``_assemble`` exposes a bare ``kind: error`` result, while the
        # public execution envelope promotes ``failure_kind`` to the typed
        # error kind. Record the latter so replay compares like with like.
        return {
            "kind": error.get("failure_kind") or error.get("kind", "execution"),
            "message": error.get("message", "automatic submission failed"),
            "details": error,
        }
    return None


def _replay_expectation_verdict(expectation, screen, error, ok, context):
    """Use trace-owned verdict logic without exposing execution state."""
    from docassemble_simulator.trace import compare_replay_expectation

    return compare_replay_expectation(expectation, screen, error, ok, context)


def _replay_trailing_observation(result):
    """Shape a finished replay walk the way its public envelope reports it.

    A failure result promotes its typed ``failure_kind`` exactly as the
    recorded step does, so the trailing verdict compares like with like.
    """
    from docassemble_simulator.trace import is_screen_outcome

    if result.get("kind") == "error":
        return (
            None,
            {
                "kind": result.get("failure_kind") or ErrorKind.EXECUTION,
                "message": result.get("message", "automatic submission failed"),
                "details": result,
            },
            False,
        )
    return (result if is_screen_outcome(result) else None), None, True


def _replay_verdict_payload(verdict):
    """Serialize the trace-owned comparison so the adapter can render it."""
    return {
        "identity_match": verdict.identity_match,
        "field_facts_match": verdict.field_facts_match,
        "flow_match": verdict.flow_match,
        "error_match": verdict.error_match,
        "match": verdict.matched,
    }


def _replay_divergence(phase, seq, screen, error, ok, submitted, verdict):
    """Report a divergent automatic step with its real observation and verdict.

    Execution owns the persistence decision, so the adapter renders the
    recorded observation and the trace-owned verdict rather than rebuilding
    the comparison or inferring a mismatch from the report shape.
    """
    return {
        "phase": phase,
        "seq": seq,
        "observation": {"screen": screen, "error": error, "ok": ok},
        "submitted": dict(submitted),
        "verdict": _replay_verdict_payload(verdict),
    }


def _replay_walk_divergence(expectations, attempted, trailing, result, context):
    """Decide whether a replayed automatic walk may be persisted.

    Only the recorded submissions are attempted, so reaching that boundary is
    not itself a mismatch: the observed outcome is compared with the recorded
    trailing record instead.  A walk that ends before every recorded attempt
    ran is truncated, and the earliest missing expected step is the divergence.
    """
    if attempted < len(expectations):
        expectation = expectations[attempted]
        screen, error, ok = _replay_trailing_observation(result)
        verdict, _identity, _content = _replay_expectation_verdict(
            expectation, screen, error, ok, context
        )
        return _replay_divergence(
            "early-end", expectation.seq, screen, error, ok, {}, verdict
        )
    if trailing is None:
        return None
    screen, error, ok = _replay_trailing_observation(result)
    verdict, _identity, _content = _replay_expectation_verdict(
        trailing, screen, error, ok, context
    )
    if verdict.matched:
        return None
    return _replay_divergence("trailing", trailing.seq, screen, error, ok, {}, verdict)


def _seek_auto(
    interview,
    namespace,
    status,
    variable,
    trace,
    step_submissions: tuple[tuple[tuple[str, str], ...], ...] | None = None,
    expectations: tuple[Any, ...] = (),
    identity_context: tuple[tuple[str, str, str | None], ...] = (),
    trailing: Any = None,
):
    result = _seek(interview, namespace, status, variable, trace)
    submitted = []
    auto_trace = []
    stop = None
    replay_divergence = None
    replay_context = list(identity_context)
    # Replay supplies the recorded submissions, so its recorded group is a hard
    # submission boundary; ordinary auto-seek remains unbounded by expectations.
    replay = step_submissions is not None
    attempted = 0
    for index in range(100):
        if result.get("kind") != "question":
            reason = (
                "sought variable resolved"
                if result.get("kind") == "continue"
                else "seek stopped"
            )
            stop = {"reason": reason}
            break
        if _variable_is_resolved(namespace, variable):
            stop = {"reason": "sought variable resolved"}
            break
        if replay and index >= len(expectations):
            # Never submit a screen the recording did not authorize: the
            # unexpected outcome is observed and the trailing verdict reports it.
            stop = {"reason": "replay boundary"}
            break
        screen = result
        assignments = (
            step_submissions[index]
            if step_submissions is not None and index < len(step_submissions)
            else ()
        )
        expectation = expectations[index] if index < len(expectations) else None
        if expectation is not None:
            pre_error = (
                {"kind": expectation.error_kind}
                if expectation.error_kind is not None
                else None
            )
            pre_verdict, _identity, _content = _replay_expectation_verdict(
                expectation,
                screen,
                pre_error,
                expectation.ok,
                tuple(replay_context),
            )
            if not (pre_verdict.identity_match and pre_verdict.field_facts_match):
                replay_divergence = _replay_divergence(
                    "before-submission",
                    expectation.seq,
                    screen,
                    None,
                    expectation.ok,
                    {},
                    pre_verdict,
                )
                auto_trace.append(
                    {
                        "screen": screen,
                        # A screen that failed the pre-submission check was
                        # only observed; its assignments never reached the
                        # interview, so they are not reported as submitted.
                        "submitted": {},
                        "ok": expectation.ok,
                        "error": None,
                    }
                )
                stop = {
                    "reason": "replay divergence",
                    "submitted": False,
                    "screen": screen,
                }
                result = screen
                break
        result, stop = _auto_submit_screen(
            interview,
            namespace,
            status,
            screen,
            assignments,
        )
        attempted += 1
        actual_error = _auto_trace_error(stop)
        actual_ok = stop is None
        # Automatic records describe the screen that was submitted, rather
        # than only the screen returned after the submission.  A rejected
        # attempt is retained as a failed step so replay cannot silently turn
        # it into a plain seek.
        auto_trace.append(
            {
                "screen": screen,
                "submitted": dict(assignments),
                "ok": actual_ok,
                "error": actual_error,
            }
        )
        if expectation is not None:
            verdict, _identity, _content = _replay_expectation_verdict(
                expectation,
                screen,
                actual_error,
                actual_ok,
                tuple(replay_context),
            )
            if not verdict.matched:
                replay_divergence = _replay_divergence(
                    "after-submission",
                    expectation.seq,
                    screen,
                    actual_error,
                    actual_ok,
                    dict(assignments),
                    verdict,
                )
                stop = {
                    "reason": "replay divergence",
                    "submitted": False,
                    "screen": screen,
                }
                result = screen
            elif _identity is not None:
                replay_context.append(
                    (_identity.key, _identity.rule, expectation.phase)
                )
        if stop is None or stop.get("submitted"):
            submitted.append(_auto_screen_summary(screen))
        if stop is not None:
            if result is None:
                result = stop["screen"]
            break
        if result.get("kind") == "finished":
            stop = {"reason": "interview completed"}
            break
        if _variable_is_resolved(namespace, variable):
            stop = {"reason": "sought variable resolved"}
            break
        result = _seek(interview, namespace, status, variable, trace)
    else:
        stop = {"reason": "auto-advance limit reached", "limit": 100}

    if replay_divergence is None and replay:
        replay_divergence = _replay_walk_divergence(
            expectations,
            attempted,
            trailing,
            result,
            tuple(replay_context),
        )

    result = dict(result)
    result["auto_submitted"] = submitted
    result["auto_stop"] = stop or {"reason": "seek stopped"}
    result["auto_trace"] = auto_trace
    if replay_divergence is not None:
        result["replay_divergence"] = replay_divergence
    return result


def _missing_name(error):
    """Return the undefined name a docassemble-style error carries, if any."""
    match = re.search(
        r"'(.*)' (is not defined|referenced before assignment|is undefined|where it is not)",
        str(error),
    )
    return match.group(1) if match else None


def _define_sought_roots(interview, namespace, status, variable):
    """Let the interview define missing roots the way assembly does.

    Assembly drives recursive seeking from the missing name in a failed
    reference: it extracts the name and seeks it, which can run a code block,
    a question, or an objects declaration.  Seeking an attribute path has to
    do the same; otherwise the generic question for a declared object can
    never be matched, because its root does not exist in the namespace yet.

    Returns a non-continue askfor result when defining a root reached a
    screen, or ``None`` when the full variable can be asked directly.
    """
    tried = set()
    while True:
        try:
            eval(variable, namespace)
            return None
        except Exception as error:  # noqa: BLE001 - any evaluation failure
            missing = _missing_name(error)
            if missing is None:
                return None
        if missing == variable or missing in tried:
            return None
        tried.add(missing)
        try:
            result = interview.askfor(
                missing,
                namespace,
                dict(namespace),
                status,
                seeking=getattr(status, "seeking", []),
                variable_stack=set(),
                questions_tried={},
            )
        except Exception as error:  # noqa: BLE001 - the full ask reports the failure
            logger.debug("could not define sought root %r: %s", missing, error)
            return None
        if result.get("type") not in ("continue", "re_run"):
            return result


def _seek(interview, namespace, status, variable, trace):
    try:
        interview.assemble(namespace, interview_status=status)
    except Exception as exc:  # noqa: BLE001 - pre-seek assembly is best-effort, ignore any failure
        logger.debug("assemble before seek failed for %r: %s", variable, exc)
    result = _define_sought_roots(interview, namespace, status, variable)
    if result is None:
        result = interview.askfor(
            variable,
            namespace,
            dict(namespace),
            status,
            seeking=[],
            variable_stack=set(),
            questions_tried={},
        )
    outcome = {"sought_variable": variable}
    if result.get("type") == "question":
        outcome.update(describe_question_result(result, namespace))
    elif result.get("type") == "continue":
        outcome.update(
            {
                "kind": "continue",
                "message": f"'{variable}' resolved without needing a screen",
            }
        )
    else:
        outcome.update(
            {
                "kind": result.get("type", "unknown"),
                "raw": {
                    key: str(value)[:120]
                    for key, value in result.items()
                    if key != "question"
                },
            }
        )
    if trace:
        outcome["seeking"] = describe_seeking(status.seeking)
    return outcome


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
    types = {
        field.get("variable"): str(field.get("type", "")).lower()
        for field in screen.get("fields") or []
    }
    for variable, target in targets.items():
        if variable in types:
            types.setdefault(target, types[variable])
    return types


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
        if source is not None:
            field["visible"], _note = field_visible(source, namespace)
        fields.append(field)
    return {**screen, "fields": fields}


def _apply_rendered_defaults(namespace, screen):
    """Apply prefilled values before evaluating submission-time visibility."""
    for variable, default, _required, field in _blankable_fields(namespace, screen):
        if default is not None and str(field.get("type", "")).lower() != "signature":
            errors = _apply_assignments(
                namespace, screen, ((variable, default),), False
            )
            if errors:
                logger.debug(
                    "field default %r could not be applied: %s", variable, errors
                )


def _early_rejection_required_guidance(namespace, screen, blank_submissions, interview):
    """Assess valid assignments and defaults without validation or blank synthesis."""
    _apply_rendered_defaults(namespace, screen)
    screen = _submission_screen(interview, namespace, screen)
    violations = _required_violations(namespace, screen, blank_submissions)
    return {
        "missing_required": [variable for variable, _reason in violations],
    }


def _blank_submissions(operation):
    """Variables whose submitted value is intentionally empty.

    A browser cannot submit a blank required input; an API caller can, and
    that explicit clearing must fail the required gate instead of parking an
    empty value in the namespace.
    """
    if operation.code:
        return set()
    blanks = set()
    for variable, raw in operation.assignments:
        if raw.strip() == "" or parse_value(raw) is None:
            blanks.add(variable)
    return blanks


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


def _required_violations(namespace, screen, blank_submissions):
    """Return ``(variable, reason)`` for required visible fields that are not
    answered. Signatures are never required; hidden fields are not shown and
    therefore not assigned or required."""
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
        elif variable in blank_submissions or _is_empty_value(value):
            violations.append((variable, "empty"))
        elif datatype in _GROUP_DATATYPES and _has_no_selected_option(value):
            violations.append((variable, "unselected"))
    return violations


def _blankable_fields(namespace, screen):
    """Yield fields the browser would submit for a shown screen but that are
    not yet defined: ``(variable, default, required)``."""
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
        if not isinstance(default, str) or not default.strip():
            default = None
        yield variable, default, field.get("required") is not False, field


def _submit_screen_fields(namespace, screen, interview):
    """Define every visible field the browser would submit, like a form POST.

    Defaults become the submitted values (the browser prefills them), optional
    blanks become the value the browser would post for the datatype, and
    signature fields become ``DAEmpty()`` so documents can still render them.
    Required fields with no value are left for the required gate.
    """
    _apply_rendered_defaults(namespace, screen)
    screen = _submission_screen(interview, namespace, screen)
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
    data = {
        key: field[key]
        for key in (
            "min",
            "max",
            "minlength",
            "maxlength",
            "step",
            "scale",
            "currency symbol",
            "field metadata",
        )
        if key in field
    }
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
                f"validation code crashed ({type(error).__name__}): {_first_line(error)}"
            )
    return {"errors": errors, "warnings": warnings}


def _mark_answered(interview, namespace, name):
    if not name:
        return
    question = interview.questions_by_name.get(name)
    if question is not None:
        try:
            question.mark_as_answered(namespace)
        except (
            AttributeError,
            TypeError,
            ValueError,
            RuntimeError,
            KeyError,
            IndexError,
            ImportError,
            OSError,
            LookupError,
            NameError,
        ) as exc:
            logger.debug("mark_as_answered failed for %r: %s", name, exc)


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


def _register_global_roots(namespace):
    try:
        from docassemble.base.functions import set_info
        from docassemble.base.util import DAObject

        roots = {
            name: value
            for name, value in namespace.items()
            if name.isidentifier()
            and not name.startswith("_")
            and isinstance(value, DAObject)
        }
        if roots:
            set_info(**roots)
    except (
        ImportError,
        AttributeError,
        TypeError,
        ValueError,
        RuntimeError,
        KeyError,
        OSError,
        LookupError,
    ) as exc:
        logger.debug("register global roots failed: %s", exc)


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


def _picklable_view(namespace):
    kept = {}
    for key, value in namespace.items():
        try:
            pickle.loads(pickle.dumps(value))
        except (
            pickle.PickleError,
            TypeError,
            ValueError,
            AttributeError,
            RuntimeError,
            OSError,
            LookupError,
            ImportError,
        ) as exc:
            logger.debug("pickling %r failed, skipping: %s", key, exc)
            continue
        kept[key] = value
    return kept


def _atomic_pickle(path, payload, *, lock_destination):
    def write_pickle(temporary):
        with temporary.open("wb") as handle:
            pickle.dump(payload, handle)
            handle.flush()

    atomic_replace(
        path,
        write_pickle,
        lock_destination=lock_destination,
    )


def _seek_question_for_variable(seeking: list[Any] | None, variable: str):
    """Find a block bounded by the terminal variable-seek history.

    ``InterviewStatus.question`` is the last successfully populated screen and
    is not evidence for an exhausted seek.  The runtime appends a variable
    stage before the candidate blocks it considers, while nested resolution
    appends another variable stage inside that block.  Keep both sides of the
    terminal stage bounded so a tolerated or later seek cannot become its
    culprit.
    """
    stages = [stage for stage in seeking or () if isinstance(stage, dict)]
    variable = str(variable)
    variable_indexes = [
        index
        for index, stage in enumerate(stages)
        if stage.get("variable") is not None and str(stage["variable"]) == variable
    ]
    if not variable_indexes:
        return None
    terminal_index = variable_indexes[-1]
    next_variable = next(
        (
            index
            for index in range(terminal_index + 1, len(stages))
            if stages[index].get("variable") is not None
        ),
        len(stages),
    )
    question_stages = [
        stage
        for stage in stages[terminal_index + 1 : next_variable]
        if stage.get("question") is not None
    ]
    if question_stages:
        return question_stages[-1]["question"]
    previous_variable = max(
        (
            index
            for index in range(terminal_index - 1, -1, -1)
            if stages[index].get("variable") is not None
        ),
        default=-1,
    )
    question_stages = [
        stage
        for stage in stages[previous_variable + 1 : terminal_index]
        if stage.get("question") is not None
    ]
    return question_stages[-1]["question"] if question_stages else None


def _expression_path(node):
    """Return a lossless-enough path for an AST reference, or ``None``.

    Attribute names, string keys, numeric indexes, and other index expressions
    are kept as distinct path components.  That prevents a reference to
    ``root.other`` or ``items[1]`` from being mistaken for a missing
    ``root.target`` or ``items[0]``.  The path is intentionally conservative:
    dynamic calls, slices, and computed roots provide no reliable provenance.
    """
    if isinstance(node, ast.Name):
        return (("name", node.id),)
    if isinstance(node, ast.Attribute):
        parent = _expression_path(node.value)
        if parent is None:
            return None
        return (*parent, ("member", node.attr))
    if isinstance(node, ast.Subscript):
        parent = _expression_path(node.value)
        if parent is None:
            return None
        index = node.slice
        if isinstance(index, ast.Index):  # pragma: no cover - Python < 3.9
            index = index.value
        if isinstance(index, ast.Constant) and isinstance(index.value, (str, int)):
            component = (
                ("member", index.value)
                if isinstance(index.value, str)
                else ("index", str(index.value))
            )
        elif isinstance(index, ast.Name):
            component = ("index-expression", index.id)
        else:
            try:
                component = ("index-expression", ast.unparse(index))
            except (AttributeError, ValueError):
                return None
        return (*parent, component)
    return None


def _reference_paths(expression: str):
    try:
        tree = ast.parse(expression, mode="exec")
    except (SyntaxError, ValueError, TypeError):
        return ()
    paths = []
    for statement in getattr(tree, "body", ()):
        for node in ast.walk(statement):
            if not isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
                continue
            if not isinstance(getattr(node, "ctx", None), ast.Load):
                continue
            path = _expression_path(node)
            if path is not None:
                paths.append((path, statement))
    return tuple(
        (path, statement)
        for path, statement in paths
        if not any(
            other_statement is statement
            and len(other_path) > len(path)
            and _path_starts_with(other_path, path)
            for other_path, other_statement in paths
        )
    )


def _path_starts_with(path, prefix) -> bool:
    return len(path) >= len(prefix) and path[: len(prefix)] == prefix


def _matching_expression(
    expression: str,
    variable: str,
    *,
    generic: bool = False,
    exact_only: bool = False,
) -> str | None:
    expression = expression.strip()
    if not expression:
        return None
    target_paths = _reference_paths(variable)
    if target_paths:
        target = target_paths[0][0]
        references = _reference_paths(expression)
        for path, statement in references:
            generic_exact = (
                generic
                and path
                and path[0] == ("name", "x")
                and len(target) == len(path)
                and target[1:] == path[1:]
            )
            if path == target or generic_exact:
                segment = ast.get_source_segment(expression, statement)
                return (segment or expression).strip()
        if exact_only:
            if re.search(rf"(?<![\w.]){re.escape(variable)}(?![\w.])", expression):
                return expression
            return None
        for path, statement in references:
            if _path_starts_with(path, target):
                segment = ast.get_source_segment(expression, statement)
                return (segment or expression).strip()
            if generic and path and path[0] == ("name", "x"):
                candidate = path[1:]
                if (
                    candidate
                    and len(target) >= len(candidate)
                    and target[-len(candidate) :] == candidate
                ):
                    segment = ast.get_source_segment(expression, statement)
                    return (segment or expression).strip()
        return None

    # A malformed or templated scalar can still provide exact textual
    # evidence.  Do not fall back to root-only matching: that would fabricate
    # a culprit for a different attribute under the same object.
    if re.search(rf"(?<![\w.]){re.escape(variable)}(?![\w.])", expression):
        return expression
    return None


def _question_expressions(question) -> list[str]:
    expressions: list[str] = []
    for field in getattr(question, "fields", None) or ():
        selections = getattr(field, "selections", None)
        if isinstance(selections, dict):
            sourcecode = selections.get("sourcecode")
            if isinstance(sourcecode, str):
                expressions.append(sourcecode)
        for name in ("showif_code", "sourcecode"):
            sourcecode = getattr(field, name, None)
            if isinstance(sourcecode, str):
                expressions.append(sourcecode)

    for name in ("sourcecode", "code"):
        sourcecode = getattr(question, name, None)
        if isinstance(sourcecode, str):
            expressions.append(sourcecode)

    source = getattr(question, "source_code", None)
    if isinstance(source, str):
        try:
            import yaml

            documents = list(yaml.safe_load_all(source))
        except Exception:  # noqa: BLE001 - provenance must survive malformed source
            documents = []
        keys = {
            "choices",
            "code",
            "if",
            "show if",
            "required",
            "default",
            "validation code",
            "need",
        }

        def collect(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in keys and isinstance(item, str):
                        expressions.append(item)
                    else:
                        collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        for document in documents:
            collect(document)
        if not expressions:
            expressions.append(source)
    return expressions


def _culprit_expression(question, sought_variable: str) -> str | None:
    expressions = _question_expressions(question)
    generic = bool(
        getattr(question, "is_generic", False)
        or getattr(question, "generic_object", None)
        or getattr(question, "generic_list_object", None)
    )
    for expression in expressions:
        matching = _matching_expression(
            expression,
            sought_variable,
            generic=generic,
            exact_only=True,
        )
        if matching is not None:
            return matching[:500]

    # A longer reference can prove that it touched a missing parent (for
    # example ``M.options.target`` while ``M.options`` is missing), but it is
    # not enough to choose between competing attributes.  Keep that useful
    # evidence only when it is unambiguous.
    matches = {
        matching[:500]
        for expression in expressions
        if (
            matching := _matching_expression(
                expression, sought_variable, generic=generic
            )
        )
        is not None
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _unresolved_culprit(error: BaseException) -> dict[str, Any] | None:
    sought_variable = _unresolved_variable(error) or "<unknown>"
    status = getattr(error, "interview_status", None)
    if status is None:
        try:
            from docassemble.base.functions import this_thread

            status = getattr(this_thread, "interview_status", None)
        except (ImportError, AttributeError):
            status = None

    question = _seek_question_for_variable(
        getattr(status, "seeking", None), sought_variable
    )
    if question is None:
        # A successfully displayed screen is not provenance for a later
        # exhausted seek.  Only retained, variable-specific history can name a
        # culprit (ADR-0004).
        return None
    provenance = _provenance(question)
    culprit = {
        "provenance": provenance,
        "expression": _culprit_expression(question, sought_variable),
        "sought_variable": sought_variable,
    }
    record_culprit(sought_variable, culprit)
    return culprit


def _unresolved_variable(error: BaseException) -> str | None:
    """Return the variable only for docassemble's exhausted-seek exception."""
    try:
        from docassemble.base.error import DAErrorMissingVariable
    except ImportError:
        DAErrorMissingVariable = ()
    if not isinstance(error, DAErrorMissingVariable):
        return None
    variable = getattr(error, "variable", None)
    if variable:
        return str(variable)
    match = re.search(r"variable ['\"]([^'\"]+)['\"]", str(error))
    return match.group(1) if match else "<unknown>"


def _is_compile_failure(error: BaseException) -> bool:
    """Whether an escaping error is a definition-load/compile failure.

    Runtime assembly failures are converted into outcome payloads earlier, so
    an escaping docassemble source error belongs to Interview compilation and
    must keep the structured compile vocabulary instead of a generic fault.
    """
    if isinstance(error, CatalogFailure):
        return True
    try:
        from docassemble.base.error import DAError, DANotFoundError
    except ImportError:
        return False
    return isinstance(error, (DAError, DANotFoundError))


def _compile_failure_outcome(error: BaseException) -> ExecutionOutcome:
    message = (
        str(error)
        if isinstance(error, CatalogFailure)
        else f"{type(error).__name__}: {error}"
    )
    return ExecutionOutcome(
        False,
        error=ExecutionError(
            ErrorKind.COMPILE, message, {"error_type": type(error).__name__}
        ),
    )


def _compatibility_failure_outcome(
    error: AssemblyLineCompatibilityError,
) -> ExecutionOutcome:
    return ExecutionOutcome(
        False,
        error=ExecutionError(
            ErrorKind.RUNTIME_COMPATIBILITY, str(error), error.details
        ),
    )


def _first_line(error):
    text = str(error).strip()
    return text.splitlines()[0] if text else type(error).__name__


def _user_facing_error(error):
    return str(error)


def _safe_repr(value):
    try:
        rendered = repr(value)
    except (
        ValueError,
        TypeError,
        RuntimeError,
        AttributeError,
        KeyError,
        OSError,
        LookupError,
        NameError,
        ImportError,
    ) as error:
        return f"<unrepr-able: {error}>"
    return rendered if len(rendered) <= 2000 else rendered[:2000] + "..."


def _jsonable(value):
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return repr(value)


__all__ = [
    "Answer",
    "Evaluate",
    "Execute",
    "ExecutionError",
    "ExecutionOutcome",
    "ExplainResolution",
    "FixtureSource",
    "FreshSource",
    "InterviewExecution",
    "Refresh",
    "RenderSource",
    "ReplayAutomatic",
    "RowAction",
    "SavedSessionSource",
    "SeedState",
    "Seek",
    "SnapshotSource",
    "Start",
    "Status",
    "Variables",
    "parse_value",
]
