"""Persistent JSON-lines worker for multi-operation simulator journeys.

The worker is intentionally small: it owns one :class:`InterviewExecution`,
keeps the target interpreter alive, and translates protocol messages into the
same typed operations used by the one-shot CLI.  It does not expose Python
objects over the wire and never writes diagnostics to stdout.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import redirect_stdout
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, TextIO

from docassemble_simulator._diagnostics import Diagnostic
from docassemble_simulator._outcomes import ErrorKind, Outcome
from docassemble_simulator._timeouts import OperationTimeout
from docassemble_simulator.execution import (
    Answer,
    Evaluate,
    Execute,
    Refresh,
    SeedState,
    Seek,
    Start,
    Status,
    Variables,
)


class WorkerProtocolError(ValueError):
    """A malformed worker request."""


RequestHandler = Callable[["Worker", Any], Outcome]
"""A command-adapter request served outside the execution operation vocabulary.

The handler owns the protocol-to-operation mapping (``render`` is the only
current one) and returns the same typed :class:`Outcome` an execution
operation returns.  Registering it through ``Worker`` keeps ``worker.py`` an
operation-only server: the worker still owns serialization, the operation
lock, the per-request deadline, and the error envelope, while the command
adapter keeps the request fields and any CLI-flavored concerns.  Handlers are
looked up by canonical :func:`operation_name` and never intercept the
protocol lifecycle operations (``ping``, ``reset``, ``shutdown``).
"""


# Alias spellings the worker accepts, mapped to the one-shot CLI command name.
# The recorder writes this canonical name to the trace, so a journey spelled
# ``evaluate`` records the same operation (and replays the same way) as the
# one-shot ``eval`` command.
_OPERATION_ALIASES = {
    "evaluate": "eval",
    "execute": "exec",
    "variables": "vars",
    "state-seed": "seed",
    "seed-state": "seed",
}


def operation_name(request: Any) -> str:
    """The canonical CLI spelling of a decoded request's operation.

    ``operation``, ``op``, and ``command`` are the accepted keys, in that
    order; case and underscore variants normalize to hyphenated lower case,
    and the aliases above collapse to the one-shot command name. Request-key
    fallback, normalization, and alias mapping live here alone so the worker's
    dispatch and the trace recorder cannot disagree about what an operation is
    called. A request that names no operation returns ``""``.
    """
    if not isinstance(request, dict):
        return ""
    name = request.get("operation", request.get("op", request.get("command")))
    if not isinstance(name, str):
        return ""
    normalized = name.strip().lower().replace("_", "-")
    return _OPERATION_ALIASES.get(normalized, normalized)


def _jsonable(value: Any):
    """Convert public simulator values to JSON without importing the CLI."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _jsonable(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def _text_value(value: Any) -> str:
    """Encode structured assignment values using the CLI's input grammar."""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), default=str)


def _assignment_pairs(raw: Any) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    if isinstance(raw, dict):
        return tuple(
            (str(variable).strip(), _text_value(value))
            for variable, value in raw.items()
        )
    if not isinstance(raw, (list, tuple)):
        raise WorkerProtocolError("answer assignments must be an object or array")
    pairs: list[tuple[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            if "=" not in item:
                raise WorkerProtocolError(
                    f"expected VAR=VALUE assignment, got {item!r}"
                )
            variable, value = item.split("=", 1)
        elif isinstance(item, dict):
            variable = item.get("variable", item.get("name"))
            if variable is None or "value" not in item:
                raise WorkerProtocolError(
                    "assignment objects require variable/name and value"
                )
            value = item["value"]
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            variable, value = item
        else:
            raise WorkerProtocolError(
                "each assignment must be VAR=VALUE, [VAR, VALUE], or an object"
            )
        variable = str(variable).strip()
        if not variable:
            raise WorkerProtocolError("assignment variable cannot be empty")
        pairs.append((variable, _text_value(value)))
    return tuple(pairs)


def operation_from_request(request: dict[str, Any]):
    """Decode one protocol request into an execution operation."""
    name = operation_name(request)
    if not name:
        raise WorkerProtocolError("request requires an operation")
    if name == "start":
        return Start()
    if name == "status":
        return Status()
    if name == "refresh":
        return Refresh()
    if name == "answer":
        return Answer(
            _assignment_pairs(request.get("assignments", request.get("answers"))),
            bool(request.get("code", False)),
            not bool(request.get("no-validate", request.get("no_validate", False))),
            bool(request.get("strict", False)),
            bool(request.get("partial", False)),
            bool(request.get("defaults", False)),
        )
    if name == "seek":
        variable = request.get("variable", request.get("sought"))
        if not isinstance(variable, str) or not variable.strip():
            raise WorkerProtocolError("seek requires a variable")
        return Seek(
            variable,
            bool(request.get("fresh", False)),
            bool(request.get("activate", False)),
            bool(request.get("trace", False)),
            bool(request.get("auto", False)),
        )
    if name == "eval":
        expression = request.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            raise WorkerProtocolError("eval requires an expression")
        return Evaluate(expression)
    if name == "vars":
        contains = request.get("contains", request.get("filter"))
        if contains is not None and not isinstance(contains, str):
            raise WorkerProtocolError("vars filter must be a string")
        return Variables(contains)
    if name == "exec":
        code = request.get("code")
        if not isinstance(code, str):
            raise WorkerProtocolError("exec requires code")
        assemble = request.get(
            "assemble",
            not bool(request.get("no-assemble", request.get("no_assemble", False))),
        )
        return Execute(code, bool(assemble))
    if name == "seed":
        code = request.get("code")
        if not isinstance(code, str):
            raise WorkerProtocolError("state-seed requires code")
        return SeedState(code, bool(request.get("assemble", False)))
    raise WorkerProtocolError(f"unknown operation: {name}")


def _failure(
    kind: ErrorKind | str, message: str, details: dict[str, Any] | None = None
):
    return {"kind": str(ErrorKind(kind)), "message": message, "details": details or {}}


class Worker:
    """One serialized, project/interview-scoped execution worker."""

    def __init__(
        self,
        root: str | Path,
        selector: str | None = None,
        *,
        config_fingerprint: str = "",
        config_context: dict[str, Any] | None = None,
        session_root: str | Path | None = None,
        timeout: float = 30.0,
        timings: bool = False,
        project_root: str | Path | None = None,
        execution=None,
        handlers: Mapping[str, RequestHandler] | None = None,
    ):
        if timeout is not None and timeout <= 0:
            raise ValueError("worker timeout must be greater than zero")
        self.root = Path(root).expanduser().resolve()
        self.project_root = (
            Path(project_root).expanduser().resolve()
            if project_root is not None
            else self.root
        )
        self.timeout = timeout
        self.timings = timings
        self.execution = execution
        self.handlers = dict(handlers or {})
        if self.execution is None:
            from docassemble_simulator.execution import InterviewExecution

            self.execution = InterviewExecution(
                self.root,
                selector,
                config_fingerprint=config_fingerprint,
                config_context=config_context,
                session_directory=(
                    Path(session_root).expanduser().resolve()
                    if session_root is not None
                    else None
                ),
                cache_interview=True,
            )
        self.closed = False
        self._started = time.perf_counter()
        self._lock = threading.Lock()

    @property
    def identity(self) -> str:
        return self.execution.scope.interview

    @property
    def scope(self) -> dict[str, Any]:
        description = self.execution.scope
        return {
            "root": str(self.root),
            "project_root": str(self.project_root),
            "interview": self.identity,
            "config_fingerprint": description.config_fingerprint,
            "session_root": str(description.session_root),
            "session": str(description.session),
            "session_id": description.session_id,
            "pid": os.getpid(),
        }

    def _run_deadline(self, callback, timeout: float | None):
        if timeout is None or threading.current_thread() is not threading.main_thread():
            return callback()
        previous_handler = signal.getsignal(signal.SIGALRM)

        def alarm(_signum, _frame):
            raise OperationTimeout

        previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, alarm)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            return callback()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer[0] > 0:
                signal.setitimer(signal.ITIMER_REAL, *previous_timer)

    def _timing(self, started: float, enabled: bool) -> dict[str, float] | None:
        if not enabled:
            return None
        return {
            "operation_ms": round((time.perf_counter() - started) * 1000, 3),
            "worker_startup_ms": round((started - self._started) * 1000, 3),
        }

    def _request_timeout(self, request: dict[str, Any]) -> float | None:
        timeout = request.get("timeout", self.timeout)
        if timeout is not None:
            try:
                timeout = float(timeout)
            except (TypeError, ValueError) as error:
                raise WorkerProtocolError(
                    "timeout must be a positive number"
                ) from error
            if timeout <= 0:
                raise WorkerProtocolError("timeout must be a positive number")
        return timeout

    def _response(
        self,
        request_id: Any,
        *,
        result: Any = None,
        error: Any = None,
        diagnostics: Any = (),
        attachments: Any = (),
        timing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response: dict[str, Any] = {
            "id": request_id,
            "ok": error is None,
            "scope": _jsonable(self.scope),
        }
        if error is None:
            response["result"] = _jsonable(result)
        else:
            response["error"] = _jsonable(error)
        if diagnostics:
            response["diagnostics"] = _jsonable(diagnostics)
        if attachments:
            response["attachments"] = _jsonable(attachments)
        if timing is not None:
            response["timing"] = timing
        return response

    def handle(self, request: Any) -> dict[str, Any]:
        """Handle one request. Every call returns a JSON-serializable envelope."""
        request_id = request.get("id") if isinstance(request, dict) else None
        started = time.perf_counter()
        timing_enabled = self.timings
        try:
            if not isinstance(request, dict):
                raise WorkerProtocolError("request must be a JSON object")
            timing_enabled = bool(request.get("timings", self.timings))
            normalized = operation_name(request)
            if normalized in {"shutdown", "close", "stop"}:
                self.closed = True
                return self._response(
                    request_id,
                    result={"kind": "shutdown", "scope": self.scope},
                    timing=self._timing(started, timing_enabled),
                )
            if normalized == "ping":
                return self._response(
                    request_id,
                    result={"kind": "ready", "scope": self.scope},
                    timing=self._timing(started, timing_enabled),
                )
            if normalized == "reset":
                timeout = self._request_timeout(request)
                with self._lock:
                    self._run_deadline(self.execution.reset, timeout)
                return self._response(
                    request_id,
                    result={"kind": "reset", "scope": self.scope},
                    timing=self._timing(started, timing_enabled),
                )
            handler = self.handlers.get(normalized)
            if handler is not None:
                timeout = self._request_timeout(request)
                with self._lock:
                    outcome = self._run_deadline(
                        lambda: handler(self, request), timeout
                    )
            else:
                operation = operation_from_request(request)
                timeout = self._request_timeout(request)
                with self._lock:
                    outcome = self._run_deadline(
                        lambda: self.execution.run(operation), timeout
                    )
            if outcome.ok:
                return self._response(
                    request_id,
                    result=outcome.result,
                    diagnostics=outcome.diagnostics,
                    attachments=outcome.attachments,
                    timing=self._timing(started, timing_enabled),
                )
            return self._response(
                request_id,
                error=outcome.error,
                diagnostics=outcome.diagnostics,
                attachments=outcome.attachments,
                timing=self._timing(started, timing_enabled),
            )
        except OperationTimeout:
            return self._response(
                request_id,
                error=_failure(
                    ErrorKind.TIMEOUT,
                    "worker operation timed out",
                    {
                        "operation": (
                            request.get(
                                "operation", request.get("op", request.get("command"))
                            )
                            if isinstance(request, dict)
                            else None
                        ),
                        "timeout": (
                            request.get("timeout", self.timeout)
                            if isinstance(request, dict)
                            else self.timeout
                        ),
                    },
                ),
                timing=self._timing(started, timing_enabled),
            )
        except WorkerProtocolError as error:
            return self._response(
                request_id,
                error=_failure(ErrorKind.INPUT, str(error)),
                timing=self._timing(started, timing_enabled),
            )
        except Exception as error:  # noqa: BLE001 - protocol must not kill the stream
            return self._response(
                request_id,
                error=_failure(ErrorKind.FAULT, f"{type(error).__name__}: {error}"),
                timing=self._timing(started, timing_enabled),
            )


def _recording_failure(
    worker: Worker, request: Any, response: dict[str, Any], error: Exception
) -> dict[str, Any]:
    """The envelope to answer with when the observer could not record.

    Recording is part of the command contract, so an otherwise ``ok``
    response becomes the typed ``input`` failure the one-shot CLI reports. A
    response that already failed keeps its error kind, message, details,
    diagnostics, and timing; its recording failure is attached as a
    ``recording-failure`` diagnostic (whose ``details`` carry
    ``error_kind: input``) because replacing the envelope would erase what the
    operation actually did.
    """
    if not response.get("ok", False):
        diagnostics = list(response.get("diagnostics") or ())
        diagnostics.append(
            _jsonable(
                Diagnostic(
                    "recording-failure",
                    str(error),
                    {"error_kind": str(ErrorKind.INPUT)},
                )
            )
        )
        response["diagnostics"] = diagnostics
        return response
    return worker._response(
        request.get("id") if isinstance(request, dict) else None,
        error=_failure(ErrorKind.INPUT, str(error)),
    )


def serve(
    worker: Worker,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
    observer: Callable[[Any, dict[str, Any]], None] | None = None,
) -> int:
    """Serve newline-delimited requests until EOF or graceful shutdown.

    ``observer``, when given, is called with each decoded request and its
    response envelope after the operation runs.  It exists so a command
    adapter can record execution responses without teaching this module about
    traces; malformed requests never reach it.

    An observer failure is isolated, never fatal: it is logged to stderr and
    the response for that request reflects it. A response that was ``ok``
    becomes the typed ``input`` failure the one-shot CLI reports, because
    recording is part of the command contract. A response that already failed
    keeps its own error and gains a typed ``recording-failure`` diagnostic, so
    a recording failure never erases the operation's result. The stream keeps
    its request/response pairing and keeps answering afterwards.
    """
    failed = False
    # Interview code is allowed to print.  Keep those diagnostics visible on
    # stderr without letting them become protocol frames on stdout.
    with redirect_stdout(sys.stderr):
        for line in input_stream:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as error:
                response = worker._response(
                    None,
                    error=_failure(
                        ErrorKind.INPUT, f"malformed JSON request: {error.msg}"
                    ),
                )
            else:
                response = worker.handle(request)
                if observer is not None:
                    try:
                        observer(request, response)
                    except Exception as error:  # noqa: BLE001 - see docstring
                        print(
                            f"worker observer failed: {type(error).__name__}: {error}",
                            file=sys.stderr,
                        )
                        response = _recording_failure(worker, request, response, error)
            failed = failed or not response.get("ok", False)
            output_stream.write(
                json.dumps(response, separators=(",", ":"), default=str) + "\n"
            )
            output_stream.flush()
            if worker.closed:
                break
    return 2 if failed else 0


__all__ = [
    "OperationTimeout",
    "RequestHandler",
    "Worker",
    "WorkerProtocolError",
    "operation_from_request",
    "operation_name",
    "serve",
]
