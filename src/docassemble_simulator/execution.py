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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from docassemble_simulator._files import (
    DestinationError,
    ProtectedDirectory,
    ProtectedPath,
    atomic_replace,
    flock,
    validate_destinations,
)
from docassemble_simulator._outcomes import ErrorKind, Failure, Outcome
from docassemble_simulator.catalog import InterviewCatalog
from docassemble_simulator.describe import (
    describe_question_result,
    describe_seeking,
    field_required,
    field_variable,
    field_visible,
)

STATE_SCHEMA = 1
_MISSING = object()


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
    """Versioned, per-interview trusted-local state with atomic replacement."""

    def __init__(self, root: Path, identity: str):
        self.identity = identity
        digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
        slug = (
            re.sub(r"[^A-Za-z0-9_.-]+", "-", identity).strip("-")[-80:] or "interview"
        )
        self.directory = root / ".simulator" / "sessions"
        self.path = self.directory / f"{slug}-{digest}.pkl"
        self.lock_path = self.path.with_suffix(".lock")

    @contextmanager
    def lock(self):
        with flock(self.lock_path):
            yield

    def load(self, *, namespace: bool = True) -> dict[str, Any]:
        if not self.path.exists():
            raise ExecutionFailure(
                ErrorKind.STATE, "no saved session; run `start` first"
            )
        return _read_payload(
            self.path,
            identity=self.identity,
            load_namespace=namespace,
            policy=_SAVED_PAYLOAD,
        )

    def save(
        self,
        namespace: dict[str, Any],
        outcome: dict[str, Any],
        active_seek: str | None = None,
    ) -> None:
        payload = {
            "schema": STATE_SCHEMA,
            "interview": self.identity,
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

    def __init__(self, root: str | Path, selector: str | None = None):
        self.root = Path(root).resolve()
        self._catalog = InterviewCatalog(self.root, selector)
        self._identity = self._catalog.identity
        self._store = StateStore(self.root, self._identity)

    def run(self, operation: Operation) -> ExecutionOutcome:
        try:
            if isinstance(operation, Status):
                return ExecutionOutcome(
                    True, self._store.load(namespace=False)["outcome"]
                )
            if isinstance(operation, Start):
                return self._mutate(lambda: self._start())
            if isinstance(operation, Refresh):
                return self._mutate(lambda: self._refresh())
            if isinstance(operation, Answer):
                return self._mutate(lambda: self._answer(operation))
            if isinstance(operation, Seek):
                return self._seek_operation(operation)
            if isinstance(operation, Evaluate):
                return self._evaluate(operation)
            if isinstance(operation, Variables):
                return self._variables(operation)
            if isinstance(operation, Execute):
                return self._mutate(lambda: self._execute(operation))
            raise TypeError(f"unknown operation: {type(operation).__name__}")
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
            logger.exception("unhandled execution fault")
            return ExecutionOutcome(
                False,
                error=ExecutionError(
                    ErrorKind.FAULT, f"{type(error).__name__}: {error}"
                ),
            )

    def _mutate(self, action: Callable[[], Any]) -> ExecutionOutcome:
        with self._store.lock():
            result = action()
        if isinstance(result, dict) and result.get("kind") == "error":
            return ExecutionOutcome(
                False,
                error=ExecutionError(
                    ErrorKind.EXECUTION,
                    result.get("message", "interview assembly failed"),
                    result,
                ),
            )
        return ExecutionOutcome(True, result)

    def _start(self):
        interview = self._catalog._compile()
        namespace = _fresh_namespace()
        with _interview_context(interview, namespace) as status:
            self._prepare(interview, namespace)
            outcome = _assemble(interview, namespace, status)
        self._store.save(namespace, outcome)
        return outcome

    def _refresh(self):
        payload = self._store.load()
        namespace = payload["namespace"]
        interview = self._catalog._compile()
        with _interview_context(interview, namespace) as status:
            self._prepare(interview, namespace)
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
        interview = self._catalog._compile()
        with _interview_context(interview, namespace) as status:
            self._prepare(interview, namespace)
            errors = _apply_assignments(
                interview, namespace, screen, operation.assignments, operation.code
            )
            if errors:
                raise ExecutionFailure(
                    ErrorKind.ANSWER_INPUT,
                    "one or more answers could not be applied",
                    {"errors": errors},
                )
            validation = (
                _validate(interview, namespace, screen)
                if operation.validate
                else {"errors": [], "warnings": []}
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
            interview = self._catalog._compile()
            with _interview_context(interview, namespace) as status:
                self._prepare(interview, namespace)
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
        interview = self._catalog._compile()
        with _interview_context(interview, namespace):
            self._prepare(interview, namespace)
            try:
                value = eval(operation.expression, namespace)
            except Exception as error:
                raise ExecutionFailure(
                    ErrorKind.EXECUTION, f"{type(error).__name__}: {error}"
                ) from error
        return ExecutionOutcome(
            True, {"expression": operation.expression, "value": _safe_repr(value)}
        )

    def _variables(self, operation: Variables):
        payload = self._store.load()
        namespace = payload["namespace"]
        interview = self._catalog._compile()
        values = {}
        with _interview_context(interview, namespace):
            self._prepare(interview, namespace)
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
        interview = self._catalog._compile()
        with _interview_context(interview, namespace) as status:
            self._prepare(interview, namespace)
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

    def _with_render_state(
        self,
        preparation: _RenderPreparation,
        action: Callable[[dict[str, Any]], Any],
    ) -> ExecutionOutcome:
        """Invoke render's private action while prepared context remains active."""
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
            interview = self._catalog._compile()
            with _interview_context(interview, namespace) as status:
                self._prepare(interview, namespace)
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
                            ErrorKind.EXECUTION,
                            outcome.get("message", "assembly failed"),
                            outcome,
                        )
                if preparation.save_snapshot:
                    self._store.save_snapshot(preparation.save_snapshot, namespace)
                return ExecutionOutcome(True, action(namespace))
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
    from docassemble.base.parse import get_initial_dict
    from docassemble.base.util import DAObject

    initial = get_initial_dict()
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
        "user": {"is_authenticated": True, "roles": ["user"]},
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
            }
        }
    )


@contextmanager
def _interview_context(interview, namespace):
    from docassemble.base.functions import this_thread
    from docassemble.base.thread_context import (
        empty_globals,
        global_context,
        user_dict_context,
    )

    status = _build_status()
    source = getattr(interview, "source", None)
    status.current_info.update(
        {"yaml_filename": getattr(source, "path", None), "url": None}
    )
    with global_context(empty_globals()), user_dict_context(namespace):
        this_thread.current_info = status.current_info
        this_thread.interview = interview
        this_thread.interview_status = status
        if source is not None:
            this_thread.current_package = getattr(source, "package", None)
        this_thread.internal = namespace.get("_internal", {})
        _register_global_roots(namespace)
        yield status


def _assemble(interview, namespace, status):
    try:
        interview.assemble(namespace, interview_status=status)
    except Exception as error:  # noqa: BLE001 - assembly may raise any interview-authored exception
        from docassemble.base.error import DAErrorNoEndpoint

        if isinstance(error, DAErrorNoEndpoint):
            return {"kind": "finished", "message": _first_line(error)}
        return {
            "kind": "error",
            "error_type": type(error).__name__,
            "message": _user_facing_error(error),
            "traceback_tail": "\n".join(
                traceback.format_exc().strip().splitlines()[-8:]
            ),
        }
    question = getattr(status, "question", None)
    if question is not None and getattr(question, "question_type", None) == "continue":
        return {
            "kind": "continue",
            "message": status.question_text or "continue screen reached",
            "question_name": getattr(question, "name", None),
        }
    if question is not None:
        result = {
            "type": "question",
            "question_text": status.question_text,
            "subquestion_text": status.subquestion_text,
            "continue_label": status.continue_label,
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


def _seek(interview, namespace, status, variable, trace):
    try:
        interview.assemble(namespace, interview_status=status)
    except Exception as exc:  # noqa: BLE001 - pre-seek assembly is best-effort, ignore any failure
        logger.debug("assemble before seek failed for %r: %s", variable, exc)
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


def _field_types(interview, screen):
    result = {
        field.get("variable"): str(field.get("type", "")).lower()
        for field in screen.get("fields") or []
    }
    question = (
        interview.questions_by_name.get(screen.get("question_name"))
        if screen.get("question_name")
        else None
    )
    for field in getattr(question, "fields", None) or []:
        name = field_variable(field)
        if name:
            result[name] = str(
                getattr(field, "datatype", "") or result.get(name, "")
            ).lower()
    return result


def _apply_assignments(interview, namespace, screen, assignments, use_code):
    errors = []
    field_types = _field_types(interview, screen)
    for variable, raw in assignments:
        temporary = "__dasimulator_value"
        previous = namespace.get(temporary, _MISSING)
        try:
            if use_code:
                __builtins__["exec"](f"{variable} = {raw}", namespace)
                continue
            value = parse_value(raw)
            datatype = field_types.get(variable, "")
            if datatype == "date":
                value = _coerce_date(raw, value)
            if datatype in {
                "object",
                "object_radio",
                "object_multiselect",
                "object_checkboxes",
            }:
                _apply_object(namespace, variable, datatype, value)
                continue
            if datatype == "checkboxes" and isinstance(value, dict):
                from docassemble.base.util import DADict

                value = DADict(elements=value)
            namespace[temporary] = value
            __builtins__["exec"](f"{variable} = {temporary}", namespace)
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
        finally:
            if previous is _MISSING:
                namespace.pop(temporary, None)
            else:
                namespace[temporary] = previous
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
    fields = getattr(question, "fields", None)
    if fields is None:
        described = [
            f
            for f in screen.get("fields") or []
            if f.get("visible") is not False and f.get("required") is not False
        ]
        checks = [(f.get("variable"), f.get("type")) for f in described]
    else:
        checks = []
        for field in fields:
            if (
                field_visible(field, namespace)[0] is False
                or not field_required(field, namespace)
                or getattr(field, "action", None)
            ):
                continue
            checks.append(
                (
                    field_variable(field),
                    getattr(field, "datatype", ""),
                )
            )
    for variable, datatype in checks:
        if not variable or "signature" in str(datatype).lower():
            continue
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
            logger.debug("required field %r undefined: %s", variable, exc)
            warnings.append(
                f"required field '{variable}' is undefined (the browser would refuse to submit this screen)"
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


def _apply_object(namespace, variable, datatype, value):
    selections = (
        namespace.get("_internal", {}).get("objselections", {}).get(variable, {})
    )
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
        namespace["__dasimulator_object"] = selected
        try:
            __builtins__["exec"](f"{variable} = __dasimulator_object", namespace)
        finally:
            namespace.pop("__dasimulator_object", None)
        return
    keys = value.keys() if isinstance(value, dict) else value
    if keys is None or isinstance(keys, (str, bytes)):
        raise ValueError(
            f"object checkbox answer for {variable} must be a mapping or list"
        )
    selected = [key for key in keys if not isinstance(value, dict) or value[key]]
    unknown = [key for key in selected if key not in selections]
    if unknown:
        raise ValueError(f"unknown object choices for {variable}: {unknown!r}")
    try:
        target = eval(variable, namespace)
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
        target = eval(variable, namespace)
    target.clear()
    for key in selected:
        target.append(selections[key])
    try:
        target.gathered = True
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
