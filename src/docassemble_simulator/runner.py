"""Public persistent runner API built on the simulator worker protocol."""

from __future__ import annotations

import copy
import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from docassemble_simulator._environment import target_interpreter_environment
from docassemble_simulator.config import (
    deep_merge,
    load_config,
    resolve_configuration,
)
from docassemble_simulator.detect import find_package_root


class RunnerError(RuntimeError):
    """The worker could not be started or its protocol became unusable."""


class _ResponseReader:
    """Read frames without a buffered readline that can outlive the deadline."""

    def __init__(self, stream):
        self.stream = stream
        self.buffer = bytearray()
        self.selector = selectors.DefaultSelector()
        os.set_blocking(stream.fileno(), False)
        self.selector.register(stream, selectors.EVENT_READ)

    def read(self, deadline: float) -> bytes:
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError
            try:
                chunk = os.read(self.stream.fileno(), 65536)
            except BlockingIOError:
                continue
            if not chunk:
                raise RunnerError("worker exited before returning a complete response")
            self.buffer.extend(chunk)
        line, _, remainder = self.buffer.partition(b"\n")
        if remainder:
            raise RunnerError("worker returned unsolicited response data")
        self.buffer.clear()
        return bytes(line)

    def close(self):
        self.selector.close()


def _write_request(stream, data: bytes, deadline: float) -> None:
    """Include pipe backpressure in the same operation deadline."""
    os.set_blocking(stream.fileno(), False)
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_WRITE)
        pending = memoryview(data)
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError
            written = os.write(stream.fileno(), pending)
            pending = pending[written:]


_REQUIRED_SCOPE_FIELDS = frozenset(
    {
        "root",
        "project_root",
        "interview",
        "config_fingerprint",
        "session_root",
        "session",
        "session_id",
        "pid",
    }
)


def _validate_request_id(value: Any) -> str | int:
    """Accept the JSON scalar IDs supported by the Python runner contract."""
    if type(value) not in (str, int):
        raise ValueError("request id must be a string or integer")
    return value


class RunnerResponse(dict):
    """Dictionary response with the familiar typed-outcome conveniences."""

    @property
    def ok(self) -> bool:
        return bool(self.get("ok"))

    @property
    def result(self):
        return self.get("result")

    @property
    def error(self):
        return self.get("error")

    @property
    def diagnostics(self):
        return self.get("diagnostics", ())

    @property
    def attachments(self):
        return self.get("attachments", ())

    @property
    def scope(self):
        return self.get("scope")


class SimulatorRunner:
    """Run one isolated project journey through one persistent worker.

    The runner deliberately returns protocol dictionaries rather than raising
    for interview failures.  This preserves the simulator's normal
    ``ok/result`` and ``ok/error`` shapes for pytest fixtures and shell agents.
    Transport and lifecycle failures are returned in that same shape where a
    response is possible; constructor/configuration mistakes raise
    :class:`RunnerError` before a worker exists.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        interview: str | None = None,
        *,
        config: str | Path | dict[str, Any] | None = None,
        config_overrides: dict[str, Any] | None = None,
        session_root: str | Path | None = None,
        timeout: float = 30.0,
        timings: bool = False,
        isolate: bool = True,
        source_policy: str | None = None,
        python: str | Path | None = None,
        offline: bool | None = None,
        background_actions: str | None = None,
        seek_diagnostics: str | None = None,
        stub_defined: bool = False,
    ):
        if timeout <= 0:
            raise ValueError("runner timeout must be greater than zero")
        self.project_root = find_package_root(root)
        self.interview = interview
        self.config = config
        if config_overrides is not None and not isinstance(config_overrides, dict):
            raise ValueError("config_overrides must be a mapping")
        self.config_overrides = config_overrides or {}
        self.timeout = timeout
        self.timings = timings
        self.isolate = isolate
        self.source_policy = source_policy or ("copy" if isolate else "shared")
        if self.source_policy not in {"copy", "symlink", "shared"}:
            raise ValueError("source_policy must be 'copy', 'symlink', or 'shared'")
        if not isolate and self.source_policy != "shared":
            raise ValueError("isolate=False requires source_policy='shared'")
        self.requested_session_root = (
            Path(session_root).expanduser().resolve() if session_root else None
        )
        self.python = str(python or sys.executable)
        self.offline = offline
        self.background_actions = background_actions
        self.seek_diagnostics = seek_diagnostics
        self.stub_defined = stub_defined
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._workspace: Path | None = None
        self._session_root: Path | None = None
        self._config_path: Path | None = None
        self._config_layers: tuple[Path, ...] = ()
        self._config_temporary: tempfile.TemporaryDirectory[str] | None = None
        self._resolved_configuration = None
        self._process: subprocess.Popen[bytes] | None = None
        self._reader: _ResponseReader | None = None
        self._next_id = 0
        self._last_response: dict[str, Any] | None = None
        self._scope: dict[str, Any] | None = None
        self._closed = False

    @property
    def root(self) -> Path:
        """The active worker root (the copied/symlinked workspace when isolated)."""
        if self._workspace is None:
            self._prepare_workspace()
        assert self._workspace is not None
        return self._workspace

    @property
    def session_root(self) -> Path:
        if self._session_root is None:
            self._prepare_workspace()
        assert self._session_root is not None
        return self._session_root

    @property
    def scope(self) -> dict[str, Any] | None:
        return (self._last_response or {}).get("scope")

    def _explicit_config_path(self) -> Path | None:
        if not isinstance(self.config, (str, Path)):
            if self.config is not None and not isinstance(self.config, dict):
                raise RunnerError("config must be a path or mapping")
            return None
        path = Path(self.config).expanduser()
        if not path.is_absolute():
            path = self.project_root / path
        path = path.resolve()
        if not path.is_file():
            raise RunnerError(f"config {path} does not exist")
        return path

    def _mapping_overrides(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        if isinstance(self.config, dict):
            deep_merge(values, copy.deepcopy(self.config))
        deep_merge(values, copy.deepcopy(self.config_overrides))
        return values

    def _command_overrides(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        if self.offline:
            values["offline"] = True
        if self.background_actions:
            values["background_actions"] = self.background_actions
        if self.seek_diagnostics:
            values["seek_diagnostics"] = self.seek_diagnostics
        return values

    def _resolve_original_configuration(self) -> None:
        if self._resolved_configuration is not None:
            return
        override_path = self._explicit_config_path()
        mapping = self._mapping_overrides()
        self._resolved_configuration = resolve_configuration(
            self.project_root,
            override_path=override_path,
            mapping_overrides=mapping or None,
            base=load_config(self.project_root),
            command_overrides=self._command_overrides(),
        )

    def _prepare_workspace(self) -> None:
        if self._workspace is not None:
            return
        # Resolve before copying or symlinking.  The worker receives the
        # original project root separately, so its discovery cannot be fooled
        # by the temporary workspace's ancestors.
        self._resolve_original_configuration()
        if self.source_policy == "shared":
            self._workspace = self.project_root
            self._temporary = None
        else:
            self._temporary = tempfile.TemporaryDirectory(prefix="docassemble-runner-")
            destination = Path(self._temporary.name) / "project"
            destination.mkdir(parents=True, exist_ok=True)
            if self.source_policy == "copy":
                shutil.copytree(
                    self.project_root,
                    destination,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(
                        ".git",
                        ".venv",
                        ".venv-*",
                        "__pycache__",
                        ".pytest_cache",
                        ".ruff_cache",
                        "dist",
                        ".simulator",
                    ),
                )
            else:
                # A symlink workspace keeps imports and package edits visibly
                # tied to the source tree while still isolating generated config
                # and session directories.
                for item in self.project_root.iterdir():
                    target = destination / item.name
                    if item.name == "docassemble":
                        target.symlink_to(item, target_is_directory=True)
                    elif item.name in {
                        ".git",
                        ".simulator",
                        ".venv",
                        ".pytest_cache",
                        ".ruff_cache",
                        "dist",
                        "__pycache__",
                    } or item.name.startswith(".venv-"):
                        continue
                    elif item.is_dir():
                        shutil.copytree(item, target, dirs_exist_ok=True)
                    else:
                        shutil.copy2(item, target)
            self._workspace = destination
        if self.requested_session_root is not None:
            self._session_root = self.requested_session_root
            self._session_root.mkdir(parents=True, exist_ok=True)
        elif self._temporary is not None:
            self._session_root = Path(self._temporary.name) / "sessions"
            self._session_root.mkdir(parents=True, exist_ok=True)
        else:
            self._session_root = self.project_root / ".simulator" / "sessions"
            self._session_root.mkdir(parents=True, exist_ok=True)
        self._prepare_config()

    def _prepare_config(self) -> None:
        config_path = self._explicit_config_path()
        mapping = self._mapping_overrides()
        if mapping:
            if self._temporary is None:
                self._config_temporary = tempfile.TemporaryDirectory(
                    prefix="docassemble-runner-config-"
                )
                directory = Path(self._config_temporary.name)
            else:
                directory = Path(self._temporary.name)
            generated = directory / "runner-config.yml"
            generated.write_text(
                yaml.safe_dump(mapping, sort_keys=False), encoding="utf-8"
            )
            self._config_layers = (generated,)
        self._config_path = config_path

    def _command(self) -> list[str]:
        self._prepare_workspace()
        command = [
            self.python,
            "-m",
            "docassemble_simulator",
            "--root",
            str(self.root),
        ]
        if self.interview:
            command.extend(("--interview", self.interview))
        if self._config_path is not None:
            command.extend(("--config", str(self._config_path)))
        if self.offline:
            command.append("--offline")
        if self.background_actions:
            command.extend(("--background-actions", self.background_actions))
        if self.seek_diagnostics:
            command.extend(("--seek-diagnostics", self.seek_diagnostics))
        if self.stub_defined:
            command.append("--stub-defined")
        command.extend(
            [
                "run",
                "--session-root",
                str(self.session_root),
                "--project-root",
                str(self.project_root),
                "--timeout",
                str(self.timeout),
            ]
        )
        for layer in self._config_layers:
            command.extend(("--config-layer", str(layer)))
        if self.timings:
            command.append("--timings")
        return command

    def open(self) -> SimulatorRunner:
        if self._closed:
            raise RunnerError("runner is already closed")
        if self._process is not None:
            return self
        self._prepare_workspace()
        try:
            self._process = subprocess.Popen(
                self._command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                bufsize=0,
                env=target_interpreter_environment(
                    {**os.environ, "PYTHONUNBUFFERED": "1"}
                ),
            )
        except OSError as error:
            self._cleanup_workspace()
            raise RunnerError(f"could not start simulator worker: {error}") from error
        assert self._process.stdout is not None
        self._reader = _ResponseReader(self._process.stdout)
        return self

    def _transport_failure(
        self, request_id: str, message: str, details=None, *, kind="fault"
    ):
        return RunnerResponse(
            {
                "id": request_id,
                "ok": False,
                "error": {
                    "kind": kind,
                    "message": message,
                    "details": details or {},
                },
            }
        )

    def _invalidate_transport(self) -> None:
        """Close a protocol that can no longer be correlated safely."""
        self.terminate()
        self._closed = True
        self._cleanup_workspace()

    def _validate_response(self, decoded: Any, request_id: str | int) -> RunnerResponse:
        if not isinstance(decoded, dict):
            raise TypeError("worker returned a non-object response")
        response_id = decoded.get("id")
        if type(response_id) is not type(request_id) or response_id != request_id:
            raise ValueError(
                f"worker response id {response_id!r} does not match {request_id!r}"
            )
        if type(decoded.get("ok")) is not bool:
            raise ValueError("worker response field 'ok' must be a boolean")
        scope = decoded.get("scope")
        if not isinstance(scope, dict):
            raise TypeError("worker response field 'scope' must be an object")
        missing_scope_fields = _REQUIRED_SCOPE_FIELDS - scope.keys()
        if missing_scope_fields:
            fields = ", ".join(sorted(missing_scope_fields))
            raise ValueError(f"worker response scope is missing: {fields}")
        string_scope_fields = _REQUIRED_SCOPE_FIELDS - {"pid", "session_id"}
        if any(not isinstance(scope[field], str) for field in string_scope_fields):
            raise ValueError("worker response scope has invalid string fields")
        if scope["session_id"] is not None and not isinstance(scope["session_id"], str):
            raise ValueError("worker response scope has an invalid session id")
        if type(scope["pid"]) is not int:
            raise ValueError("worker response scope has an invalid pid")
        if self._scope is not None and scope != self._scope:
            raise ValueError("worker response scope changed during a journey")
        if decoded["ok"]:
            if "result" not in decoded or "error" in decoded:
                raise ValueError("successful worker response has an invalid shape")
        elif "error" not in decoded or "result" in decoded:
            raise ValueError("failed worker response has an invalid shape")
        if not decoded["ok"]:
            error = decoded["error"]
            if (
                not isinstance(error, dict)
                or not isinstance(error.get("kind"), str)
                or not isinstance(error.get("message"), str)
                or not isinstance(error.get("details"), dict)
            ):
                raise ValueError("worker error response has an invalid shape")
        response = RunnerResponse(decoded)
        if self._scope is None:
            self._scope = dict(scope)
        return response

    def request(self, operation: str, **arguments: Any) -> RunnerResponse:
        """Send one operation and wait at most ``timeout`` seconds for a response.

        An omitted identifier is a monotonically increasing decimal string.  A
        caller-supplied string or integer is sent unchanged and must be echoed
        with the same JSON type by the worker.
        """
        if "id" in arguments:
            request_id = _validate_request_id(arguments["id"])
        else:
            request_id = None
        self.open()
        assert self._process is not None
        assert self._process.stdin is not None
        assert self._reader is not None
        self._next_id += 1
        if request_id is None:
            request_id = str(self._next_id)
        request = {"id": request_id, "operation": operation, **arguments}
        deadline = time.monotonic() + self.timeout
        try:
            _write_request(
                self._process.stdin,
                (json.dumps(request, default=str) + "\n").encode("utf-8"),
                deadline,
            )
            frame = self._reader.read(deadline)
            if not frame:
                code = self._process.poll()
                response = self._transport_failure(
                    request_id,
                    "worker exited before returning a response",
                    {"returncode": code, "operation": operation},
                )
                self._invalidate_transport()
            else:
                try:
                    decoded = json.loads(frame.decode("utf-8"))
                    response = self._validate_response(decoded, request_id)
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    TypeError,
                    ValueError,
                ) as error:
                    response = self._transport_failure(
                        request_id,
                        "worker returned an unusable response",
                        {"error": str(error), "operation": operation},
                    )
                    self._invalidate_transport()
            self._last_response = response
            return response
        except TimeoutError:
            self._invalidate_transport()
            response = self._transport_failure(
                request_id,
                "worker operation timed out before a complete response",
                {"operation": operation, "timeout": self.timeout},
                kind="timeout",
            )
            self._last_response = response
            return response
        except (BrokenPipeError, OSError, RunnerError) as error:
            self._invalidate_transport()
            response = self._transport_failure(
                request_id,
                f"worker transport failed: {error}",
                {"operation": operation},
            )
            self._last_response = response
            return response

    def run(self, operation: str, **arguments: Any):
        """Submit one named worker operation."""
        return self.request(operation, **arguments)

    def journey(self, operations: Iterable[dict[str, Any]]) -> list[RunnerResponse]:
        """Run every request in order, retaining responses after failures."""
        responses = []
        for request in operations:
            if not isinstance(request, dict):
                responses.append(self.request("invalid", request=request))
                continue
            request = dict(request)
            operation = request.pop("operation", request.pop("op", None))
            if operation is None:
                responses.append(self.request("invalid", **request))
            else:
                responses.append(self.request(str(operation), **request))
        return responses

    def start(self):
        return self.request("start")

    def status(self):
        return self.request("status")

    def refresh(self):
        return self.request("refresh")

    def answer(self, assignments=None, **options):
        if assignments is not None:
            options["assignments"] = assignments
        return self.request("answer", **options)

    def action(self, action_id: str):
        """Press one native link discovered on the active saved screen."""
        return self.request("action", action_id=action_id)

    def row_action(self, kind: str, target: str, *, row: int | None = None):
        return self.request("row-action", kind=kind, target=target, row=row)

    def seek(self, variable: str, **options):
        return self.request("seek", variable=variable, **options)

    def evaluate(self, expression: str):
        return self.request("eval", expression=expression)

    eval = evaluate

    def execute(self, code: str, *, assemble: bool = True):
        return self.request("exec", code=code, assemble=assemble)

    exec = execute

    def seed(self, code: str, *, assemble: bool = False):
        return self.request("seed", code=code, assemble=assemble)

    seed_state = seed

    def variables(self, contains: str | None = None):
        return (
            self.request("vars", contains=contains)
            if contains is not None
            else self.request("vars")
        )

    vars = variables

    def reset(self):
        return self.request("reset")

    def terminate(self) -> None:
        process = self._process
        if process is None:
            self._scope = None
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=min(self.timeout, 2.0))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()
        self._process = None
        self._scope = None

    def close(self):
        if self._closed:
            if self._process is not None:
                self.terminate()
            self._cleanup_workspace()
            return self._last_response
        response = None
        if self._process is not None:
            if self._process.poll() is None:
                response = self.request("shutdown")
            if self._process is not None:
                self.terminate()
        self._closed = True
        self._cleanup_workspace()
        return response

    shutdown = close

    def _cleanup_workspace(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        self._process = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
        if self._config_temporary is not None:
            self._config_temporary.cleanup()
            self._config_temporary = None
        self._workspace = None
        self._session_root = None
        self._config_path = None

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


Runner = SimulatorRunner

__all__ = ["Runner", "RunnerError", "RunnerResponse", "SimulatorRunner"]
