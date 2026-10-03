"""Transactional interview execution behind one typed-operation interface."""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import pickle
import re
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
from docassemble_simulator.catalog import CatalogFailure, InterviewCatalog
from docassemble_simulator.compatibility import (
    AssemblyLineCompatibilityError,
    require_assemblyline_compatibility,
)
from docassemble_simulator.describe import (
    describe_question_result,
    describe_seeking,
)

STATE_SCHEMA = 1
_MISSING = object()


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


@dataclass(frozen=True)
class Seek:
    variable: str
    fresh: bool = False
    activate: bool = False
    trace: bool = False


@dataclass(frozen=True)
class Evaluate:
    expression: str


@dataclass(frozen=True)
class Variables:
    contains: str | None = None


@dataclass(frozen=True)
class Execute:
    code: str
    assemble: bool = True


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


Operation = Start | Status | Refresh | Answer | Seek | Evaluate | Variables | Execute


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

    def __init__(self, root: Path, identity: str, config_fingerprint: str = ""):
        self.identity = identity
        self.config_fingerprint = config_fingerprint
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
        self.directory = root / ".simulator" / "sessions"
        self.path = self.directory / f"{self.slug}-{digest}.pkl"
        self.lock_path = self.path.with_suffix(".lock")

    @contextmanager
    def lock(self):
        with flock(self.lock_path):
            yield

    def _other_configuration(self) -> Path | None:
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
                return candidate
        return None

    def load(self, *, namespace: bool = True) -> dict[str, Any]:
        if not self.path.exists():
            if self._other_configuration() is not None:
                raise ExecutionFailure(
                    ErrorKind.STATE,
                    "a saved session for this interview exists under a "
                    "different effective configuration; run `start` to begin "
                    "one for this configuration",
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
            raise ExecutionFailure(
                ErrorKind.STATE,
                "saved state was produced under a different effective "
                "configuration; run `start` again",
            )
        return payload

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


class InterviewExecution:
    """Run complete operations; mutable runtime state never crosses this interface."""

    def __init__(
        self,
        root: str | Path,
        selector: str | None = None,
        *,
        config_fingerprint: str = "",
    ):
        self.root = Path(root).resolve()
        self._catalog = InterviewCatalog(self.root, selector)
        self._identity = self._catalog.identity
        self._store = StateStore(self.root, self._identity, config_fingerprint)

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
                elif isinstance(operation, Seek):
                    outcome = self._seek_operation(operation)
                elif isinstance(operation, Evaluate):
                    outcome = self._evaluate(operation)
                elif isinstance(operation, Variables):
                    outcome = self._variables(operation)
                elif isinstance(operation, Execute):
                    outcome = self._mutate(lambda: self._execute(operation))
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
                    outcome = ExecutionOutcome(
                        False,
                        error=ExecutionError(
                            ErrorKind.UNRESOLVED_VARIABLE,
                            str(error),
                            {"sought_variable": unresolved},
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

    def _answer(self, operation: Answer):
        if not operation.assignments:
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
        with self._prepared_operation(namespace) as (interview, status):
            errors = _apply_assignments(
                namespace,
                screen,
                operation.assignments,
                operation.code,
                allowed=_answerable_fields(screen),
            )
            if errors:
                raise ExecutionFailure(
                    ErrorKind.ANSWER_INPUT,
                    "one or more answers could not be applied",
                    {"errors": errors},
                )
            if not operation.partial:
                # The whole screen is submitted: browser defaults and blanks
                # for optional fields, then the required gate.
                _submit_screen_fields(namespace, screen)
            violations = _required_violations(namespace, screen, blank_submissions)
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
                raise ExecutionFailure(
                    ErrorKind.VALIDATION,
                    "screen rejected; all answers were discarded",
                    validation,
                )
            _mark_answered(interview, namespace, screen.get("question_name"))
            outcome = _assemble(interview, namespace, status)
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
                outcome = _seek(
                    interview, namespace, status, operation.variable, operation.trace
                )
            if operation.activate:
                self._store.save(namespace, outcome, operation.variable)
            return outcome

        if operation.activate:
            return self._mutate(work)
        try:
            return ExecutionOutcome(True, work())
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

    def _execute(self, operation: Execute):
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
        }
        return describe_question_result(result, namespace)
    return {
        "kind": "continue",
        "message": "mandatory code completed without reaching a screen",
    }


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


def _required_message(variable, reason):
    if reason == "unselected":
        return (
            f"required field '{variable}' has no selected option "
            "(the browser would require at least one)"
        )
    state = "empty" if reason == "empty" else "undefined"
    return (
        f"required field '{variable}' is {state} "
        "(the browser would refuse to submit this screen)"
    )


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


def _submit_screen_fields(namespace, screen):
    """Define every visible field the browser would submit, like a form POST.

    Defaults become the submitted values (the browser prefills them), optional
    blanks become the value the browser would post for the datatype, and
    signature fields become ``DAEmpty()`` so documents can still render them.
    Required fields with no value are left for the required gate.
    """
    blanks = []
    targets = _screen_field_targets(screen)
    for variable, default, required, field in _blankable_fields(namespace, screen):
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
        if not signature and default is not None:
            applied = _apply_assignments(
                namespace, screen, ((variable, default),), False
            )
            if not applied:
                continue
            logger.debug("field default %r could not be applied: %s", variable, applied)
        if required and not signature:
            continue
        blank = _browser_blank(field)
        if blank is not _NO_BLANK:
            blanks.append((variable, blank))
    for variable, blank in blanks:
        _set_namespace_value(namespace, targets.get(variable, variable), blank)


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
    if datatype in {"threestate", "object", "object_radio", "file"}:
        return None
    if datatype in _OBJECT_GROUP_DATATYPES:
        # Object selections need runtime initialisation; a fabricated empty
        # object could corrupt interview logic, so leave it undefined.
        return _NO_BLANK
    if datatype in _CHECKBOX_GROUP_DATATYPES:
        values = [
            choice.get("value")
            for choice in field.get("choices") or []
            if "value" in choice
        ]
        if not values:
            return None
        from docassemble.base.util import DADict

        return DADict(elements={str(value): False for value in values})
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


def _apply_assignments(namespace, screen, assignments, use_code, allowed=None):
    errors = []
    targets = _screen_field_targets(screen)
    field_types = _field_types(screen, targets)
    for variable, raw in assignments:
        if allowed is not None and variable not in allowed:
            errors.append(f"{variable}: not a field on the active screen")
            continue
        try:
            if use_code:
                _exec_in_namespace(namespace, f"{variable} = {raw}")
                continue
            value = parse_value(raw)
            datatype = field_types.get(variable, "")
            if datatype == "date":
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
                    continue
            target = targets.get(variable, variable)
            if datatype in {
                "object",
                "object_radio",
                "object_multiselect",
                "object_checkboxes",
            }:
                _apply_object(namespace, variable, target, datatype, value)
                continue
            if datatype == "checkboxes" and isinstance(value, dict):
                from docassemble.base.util import DADict

                value = DADict(elements=value)
            _set_namespace_value(namespace, target, value)
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
    return errors


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
    "FixtureSource",
    "FreshSource",
    "InterviewExecution",
    "Refresh",
    "RenderSource",
    "SavedSessionSource",
    "Seek",
    "SnapshotSource",
    "Start",
    "Status",
    "Variables",
    "parse_value",
]
