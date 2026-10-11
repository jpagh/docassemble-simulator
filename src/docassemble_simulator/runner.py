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
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from docassemble_simulator.detect import find_package_root


class RunnerError(RuntimeError):
    """The worker could not be started or its protocol became unusable."""


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
        self._process: subprocess.Popen[str] | None = None
        self._selector: selectors.BaseSelector | None = None
        self._next_id = 0
        self._last_response: dict[str, Any] | None = None
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

    def _prepare_workspace(self) -> None:
        if self._workspace is not None:
            return
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
        config_path: Path | None = None
        if isinstance(self.config, (str, Path)):
            config_path = Path(self.config).expanduser()
            if not config_path.is_absolute():
                config_path = (self.project_root / config_path).resolve()
            else:
                config_path = config_path.resolve()
        elif self.config is not None and not isinstance(self.config, dict):
            raise RunnerError("config must be a path or mapping")
        if config_path is not None and not config_path.is_file():
            raise RunnerError(f"config {config_path} does not exist")
        if self.config_overrides or isinstance(self.config, dict):
            values: dict[str, Any] = {}
            if config_path is not None:
                if config_path.suffix.lower() == ".toml":
                    import tomllib

                    values = tomllib.loads(config_path.read_text(encoding="utf-8"))
                else:
                    values = (
                        yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
                    )
            elif isinstance(self.config, dict):
                values = copy.deepcopy(self.config)
            _deep_merge(values, self.config_overrides)
            assert self._temporary is not None or self._workspace is not None
            directory = (
                Path(self._temporary.name)
                if self._temporary is not None
                else self._workspace
            )
            assert directory is not None
            config_path = directory / "runner-config.yml"
            config_path.write_text(
                yaml.safe_dump(values, sort_keys=False), encoding="utf-8"
            )
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
        if self.timings:
            command.append("--timings")
        return command

    def open(self) -> SimulatorRunner:
        if self._process is not None:
            return self
        if self._closed:
            raise RunnerError("runner is already closed")
        self._prepare_workspace()
        try:
            self._process = subprocess.Popen(
                self._command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                text=True,
                bufsize=1,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        except OSError as error:
            self._cleanup_workspace()
            raise RunnerError(f"could not start simulator worker: {error}") from error
        assert self._process.stdout is not None
        self._selector = selectors.DefaultSelector()
        self._selector.register(self._process.stdout, selectors.EVENT_READ)
        return self

    def _transport_failure(self, request_id: str, message: str, details=None):
        return RunnerResponse(
            {
                "id": request_id,
                "ok": False,
                "error": {
                    "kind": "fault",
                    "message": message,
                    "details": details or {},
                },
            }
        )

    def request(self, operation: str, **arguments: Any) -> RunnerResponse:
        """Send one operation and wait at most ``timeout`` seconds for a response."""
        self.open()
        assert self._process is not None
        assert self._process.stdin is not None
        assert self._selector is not None
        self._next_id += 1
        request_id = str(self._next_id)
        request = {"id": request_id, "operation": operation, **arguments}
        try:
            self._process.stdin.write(json.dumps(request, default=str) + "\n")
            self._process.stdin.flush()
            ready = self._selector.select(self.timeout)
            if not ready:
                self.terminate()
                response = self._transport_failure(
                    request_id,
                    "worker operation timed out",
                    {"operation": operation, "timeout": self.timeout},
                )
                self._last_response = response
                return response
            line = self._process.stdout.readline() if self._process.stdout else ""
            if not line:
                code = self._process.poll()
                response = self._transport_failure(
                    request_id,
                    "worker exited before returning a response",
                    {"returncode": code, "operation": operation},
                )
                self._last_response = response
                return response
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError as error:
                response = self._transport_failure(
                    request_id,
                    "worker returned malformed JSON",
                    {"error": str(error), "operation": operation},
                )
            else:
                if isinstance(decoded, dict):
                    response = RunnerResponse(decoded)
                else:
                    response = self._transport_failure(
                        request_id, "worker returned a non-object response"
                    )
            self._last_response = response
            return response
        except (BrokenPipeError, OSError) as error:
            response = self._transport_failure(
                request_id, f"worker transport failed: {error}"
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
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=min(self.timeout, 2.0))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if self._selector is not None:
            self._selector.close()
            self._selector = None
        self._process = None

    def close(self):
        if self._closed:
            self._cleanup_workspace()
            return self._last_response
        response = None
        if self._process is not None and self._process.poll() is None:
            response = self.request("shutdown")
            if self._process is not None:
                self.terminate()
        self._closed = True
        self._cleanup_workspace()
        return response

    shutdown = close

    def _cleanup_workspace(self) -> None:
        if self._selector is not None:
            self._selector.close()
            self._selector = None
        self._process = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
        self._workspace = None
        self._session_root = None
        self._config_path = None

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


Runner = SimulatorRunner

__all__ = ["Runner", "RunnerError", "RunnerResponse", "SimulatorRunner"]
