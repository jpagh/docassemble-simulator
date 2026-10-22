"""Thin CLI adapters for catalog, execution, and rendering modules."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import textwrap
from collections import Counter
from collections.abc import Callable
from contextlib import nullcontext, redirect_stdout
from dataclasses import dataclass, fields, is_dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from docassemble_simulator._environment import target_interpreter_environment
from docassemble_simulator._outcomes import ErrorKind, Failure, Outcome
from docassemble_simulator._runtime import (
    SimulatorRuntime,
    bootstrap,
    dyld_fallback_value,
)
from docassemble_simulator.catalog import list_interviews
from docassemble_simulator.compatibility import (
    AssemblyLineCompatibilityError,
    compatibility_report,
)
from docassemble_simulator.config import (
    DOCASSEMBLE_DEFAULTS,
    SIMULATOR_DEFAULTS,
    config_fingerprint,
    deep_merge,
    discover_config_files,
    global_config_path,
    load_config,
    pass_through_config,
    redact_config,
    resolve_configuration,
    simulator_settings,
)
from docassemble_simulator.detect import find_package_root
from docassemble_simulator.preflight import ensure_importable


class InputFailure(Exception):
    pass


class UsageFailure(Exception):
    def __init__(self, command: str, message: str):
        super().__init__(message)
        self.command = command


class _MultiLineHelpFormatter(argparse.HelpFormatter):
    """Wrap description and epilog lines independently, keeping examples intact."""

    def _fill_text(self, text, width, indent):
        return "\n".join(
            textwrap.fill(
                line,
                width,
                initial_indent=indent,
                subsequent_indent=indent + "  ",
            )
            for line in text.splitlines()
        )


class UsageParser(argparse.ArgumentParser):
    def __init__(self, *args, command_name: str = "cli", **kwargs):
        kwargs.setdefault("formatter_class", _MultiLineHelpFormatter)
        super().__init__(*args, **kwargs)
        self.command_name = command_name

    def error(self, message):
        raise UsageFailure(self.command_name, message)


def _json_requested(arguments):
    for argument in arguments:
        if argument == "--":
            return False
        if argument == "--json":
            return True
    return False


def _clean(value):
    if is_dataclass(value) and not isinstance(value, type):
        return _clean(
            {field.name: getattr(value, field.name) for field in fields(value)}
        )
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


def _envelope(
    command: str,
    result: Any = None,
    error: Any = None,
    diagnostics: Any = (),
    attachments: Any = (),
):
    if error is None:
        payload = {"ok": True, "command": command, "result": result}
    else:
        payload = {"ok": False, "command": command, "error": _clean(error)}
    if diagnostics:
        payload["diagnostics"] = _clean(diagnostics)
    if attachments:
        payload["attachments"] = _clean(attachments)
    return payload


def _diagnostic_summary(diagnostics, limit=5):
    events = _clean(diagnostics)
    counts = Counter(event.get("kind", "unknown") for event in events)
    return {
        "count": len(events),
        "counts": dict(sorted(counts.items())),
        "head": events[:limit],
        "tail": events[-limit:],
        "truncated": len(events) > limit * 2,
    }


def _simulator_version():
    """The installed simulator distribution version, or a source fallback."""
    from importlib import metadata as importlib_metadata

    try:
        return importlib_metadata.version("docassemble-simulator")
    except importlib_metadata.PackageNotFoundError:
        return "unknown"


def _emit(payload, as_json):
    payload = _clean(payload)
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
        return
    if payload["ok"]:
        print(_human(payload.get("result")))
    else:
        error = payload["error"]
        print(f"error: {error['message']}", file=sys.stderr)
        for key, value in (error.get("details") or {}).items():
            print(f"  {key}: {_human(value)}", file=sys.stderr)
    diagnostics = payload.get("diagnostics") or []
    if diagnostics:
        print("diagnostics:")
        print(_human(diagnostics, 1))
    attachments = payload.get("attachments") or []
    if attachments:
        print("attachments:")
        print(_human(attachments, 1))


def _human(value, indent=0):
    pad = "  " * indent
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.extend([f"{pad}{key}:", _human(item, indent + 1)])
            else:
                lines.append(f"{pad}{key}: {item}")
        return "\n".join(lines)
    if isinstance(value, list):
        return "\n".join(_human(item, indent) for item in value)
    return f"{pad}{value}"


EXIT_CODES = {
    ErrorKind.INPUT: 1,
    ErrorKind.STATE: 1,
    ErrorKind.WORKSPACE: 1,
    ErrorKind.CONFIGURATION: 1,
    ErrorKind.FAULT: 3,
    ErrorKind.RUNTIME_COMPATIBILITY: 2,
    ErrorKind.TRACE_MISMATCH: 2,
    ErrorKind.TIMEOUT: 2,
}


def _exit_for_error(kind: ErrorKind | str):
    try:
        normalized = ErrorKind(kind)
    except ValueError:
        return 2
    return EXIT_CODES.get(normalized, 2)


def _execution(args, root):
    from docassemble_simulator.execution import InterviewExecution

    resolved = getattr(args, "_resolved_configuration", None)
    fingerprint = resolved.fingerprint if resolved is not None else ""
    return InterviewExecution(
        root,
        args.interview,
        config_fingerprint=fingerprint,
        config_context=resolved.session_context() if resolved is not None else None,
    )


def _config_report(root, config, args=None):
    resolved = getattr(args, "_resolved_configuration", None) if args else None
    if resolved is not None:
        report = resolved.report()
        settings = resolved.simulator
    else:
        settings = simulator_settings(config)
        report = {
            "files": [
                *[str(path) for path in discover_config_files(root)],
                *(
                    [str(global_config_path())]
                    if global_config_path().is_file()
                    else []
                ),
            ],
            "effective_config": str(root / ".simulator" / "config-effective.yml"),
            "config_fingerprint": config_fingerprint(config),
            "simulator": redact_config(settings),
            "pass_through_keys": sorted(pass_through_config(config)),
            "defaults": {
                "docassemble": DOCASSEMBLE_DEFAULTS,
                "simulator": SIMULATOR_DEFAULTS,
            },
        }
    command_line = {}
    if args is not None:
        if getattr(args, "offline", False):
            settings["offline"] = True
            command_line["offline"] = True
        if getattr(args, "background_actions", None):
            settings["background_actions"] = args.background_actions
            command_line["background_actions"] = args.background_actions
        if getattr(args, "seek_diagnostics", None):
            settings["seek_diagnostics"] = args.seek_diagnostics
            command_line["seek_diagnostics"] = args.seek_diagnostics
    report["simulator"] = redact_config(settings)
    report["defaults"]["capabilities"] = {
        "docx": "supported",
        "pdf_conversion": (
            "unavailable; DOCX is returned when a generated PDF is requested "
            "for a DOCX document"
        ),
        "background_actions": "foreground by default; no Celery worker",
        "persistent_runner": "JSON-lines worker and Python SimulatorRunner",
        "session_persistence": ("disabled by default; no server database substituted"),
    }
    report["command_line_overrides"] = command_line
    if args is not None and getattr(args, "config", None):
        report["config_override"] = str(Path(args.config).expanduser().resolve())
    return report


def _configured_bindings(config, template, command_bindings):
    settings = simulator_settings(config)
    table = settings.get("render_bindings", {})
    defaults = {}
    specific = {}
    if isinstance(table, dict):
        defaults = {
            key: value for key, value in table.items() if isinstance(value, str)
        }
        candidate = table.get(template, {})
        if isinstance(candidate, dict):
            specific = {
                key: value for key, value in candidate.items() if isinstance(value, str)
            }
    merged = dict(defaults)
    merged.update(specific)
    merged.update(dict(command_bindings or ()))
    return tuple((str(name), str(expression)) for name, expression in merged.items())


def _info_result(root, config, args):
    """The ``sim info --json`` result for a workspace and effective config."""
    from docassemble_simulator.catalog import list_packages

    data = {
        "root": str(root),
        "simulator_version": _simulator_version(),
        "packages": list_packages(root),
        "interview_count": len(list_interviews(root)),
        "interviews": list_interviews(root),
        "config": _config_report(root, config, args),
    }
    try:
        import docassemble.base

        data["docassemble_version"] = getattr(
            docassemble.base, "__version__", "unknown"
        )
    except (ImportError, AttributeError, ModuleNotFoundError) as exc:
        logger.debug("docassemble version lookup failed: %s", exc)
        data["docassemble_version"] = "unknown (docassemble not importable)"
    from docassemble_simulator._runtime import PDF_UNAVAILABLE_MESSAGE

    data["capabilities"] = {
        "docx": "supported",
        "pdf": (
            "unavailable; a generated-PDF request for a DOCX document is "
            "skipped and the DOCX artifact is used"
        ),
        "pdf_message": PDF_UNAVAILABLE_MESSAGE,
        "external_pdf_converter": False,
        "background_worker": False,
        "persistent_runner": "supported through the JSON-lines `run` worker",
        "session_persistence": "disabled (no server database substituted)",
    }
    data["runtime_compatibility"] = compatibility_report()
    return data


def cmd_info(args, root):
    config = getattr(args, "_simulator_config", {})
    _emit(_envelope("info", _info_result(root, config, args)), args.json)
    return 0


def _config_result(root, config, args):
    """The ``sim config --json`` result for a workspace and effective config."""
    report = _config_report(root, config, args)
    effective = dict(DOCASSEMBLE_DEFAULTS)
    deep_merge(effective, pass_through_config(config))
    report["effective"] = redact_config(effective)
    report["overrides"] = redact_config(config)
    report["source"] = "defaults plus discovered/--config overrides"
    return report


def cmd_config(args, root):
    config = getattr(args, "_simulator_config", {})
    _emit(_envelope("config", _config_result(root, config, args)), args.json)
    return 0


def cmd_catalog(args, root):
    from docassemble_simulator.catalog import InterviewCatalog

    built = _catalog_payload(
        _CATALOG_VERBS[args.command].request(
            args.command,
            catalog=lambda: InterviewCatalog(root, args.interview),
            execution=lambda: _execution(args, root),
            variable=getattr(args, "variable", None),
            var=getattr(args, "var", None),
            strict=bool(getattr(args, "strict", False)),
        )
    )
    _emit(built.envelope, args.json)
    if args.command == "check" and not args.json and built.result is not None:
        totals = built.result.get("totals") or {
            "checked": built.result.get("checked", 0),
            "ok": built.result.get("checked", 0) - built.result.get("failures", 0),
            "failed": built.result.get("failures", 0),
            "duplicate_ids": 0,
        }
        print(
            f"checked: {totals['checked']}  "
            f"ok: {totals['ok']}  failures: {totals['failed']}  "
            f"duplicate ids: {totals.get('duplicate_ids', 0)}"
        )
    return built.exit_code


@dataclass(frozen=True)
class _CatalogPayload:
    """A catalog command's ``--json`` envelope, exit code, and result.

    ``result`` is the computed catalog result once the command reached its
    reporting stage, and ``None`` when a raising catalog produced the error,
    so the human ``check`` totals are only printed for a computed result.
    """

    envelope: dict[str, Any]
    exit_code: int
    result: dict[str, Any] | None


@dataclass(frozen=True)
class _CatalogRequest:
    """One catalog command's namespace source and its decoded options.

    ``catalog`` serves the listings and ``check`` (a read-only compile) and
    ``execution`` serves ``why`` (read-only through ``ExplainResolution``);
    exactly one is set. ``variable``/``var``/``strict`` are the command's
    optional request fields. ``cmd_catalog`` and the worker's catalog adapter
    both build one record and hand it to :func:`_catalog_payload`, so the two
    transports cannot disagree about a verb's inputs.
    """

    command: str
    catalog: Any = None
    execution: Any = None
    variable: str | None = None
    var: str | None = None
    strict: bool = False


@dataclass(frozen=True)
class _CatalogVerb:
    """How one catalog verb reads its source and names its request fields.

    ``source`` selects the record's namespace source (``catalog`` for the
    read-only compile verbs, ``execution`` for ``why``); the flags say which
    optional request fields the verb accepts. A new verb whose shape matches an
    existing one only needs a row in :data:`_CATALOG_VERBS`.
    """

    source: str
    variable_field: bool = False
    filter_field: bool = False
    strict_flag: bool = False

    def request(
        self,
        command: str,
        *,
        catalog,
        execution,
        variable=None,
        var=None,
        strict=False,
    ) -> _CatalogRequest:
        """Build one request record from the sources a transport can offer.

        Each source is a callable, so a transport only pays for the one its
        verb reads.
        """
        if self.source == "execution":
            return _CatalogRequest(
                command, execution=execution(), variable=variable, strict=strict
            )
        return _CatalogRequest(command, catalog=catalog(), var=var, strict=strict)


_CATALOG_VERBS = {
    "questions": _CatalogVerb("catalog", filter_field=True),
    "index": _CatalogVerb("catalog", filter_field=True),
    "check": _CatalogVerb("catalog", strict_flag=True),
    "why": _CatalogVerb("execution", variable_field=True, strict_flag=True),
}
"""The read-only catalog verbs, keyed by their one-shot command name."""


def _catalog_result(outcome):
    """The public catalog result with its interview identity when it has one."""
    result = outcome.result
    interview = getattr(outcome, "interview", None)
    if interview:
        result = result | {"interview": interview}
    return result


def _catalog_failure(request: _CatalogRequest, result):
    """The typed failure a catalog result reports, or ``None`` when it passes."""
    if request.command == "check" and result["failures"]:
        return Failure(
            ErrorKind.COMPILE,
            "one or more interviews failed to compile",
            result,
        )
    if (
        request.command == "check"
        and request.strict
        and result.get("totals", {}).get("duplicate_ids", 0)
    ):
        return Failure(
            ErrorKind.DUPLICATE_ID,
            "duplicate explicit question ids found",
            result,
        )
    if request.command == "why" and request.strict and result["conflict"]:
        return Failure(
            ErrorKind.RESOLUTION_CONFLICT,
            "variable resolution conflict found",
            result,
        )
    return None


def _catalog_payload(request: _CatalogRequest):
    from docassemble_simulator._diagnostics import capture_diagnostics, is_collecting

    if request.command == "why" or is_collecting():
        return _catalog_payload_impl(request)
    with capture_diagnostics() as diagnostics:
        built = _catalog_payload_impl(request)
    if diagnostics:
        built.envelope["diagnostics"] = _clean(diagnostics)
    return built


def _catalog_payload_impl(request: _CatalogRequest):
    """Build one read-only catalog command's envelope without emitting it.

    Shared by :func:`cmd_catalog` and the worker's catalog adapter so both
    transports return the same ``result``/``error`` for the same project. The
    request's ``catalog``/``execution`` let the persistent worker reuse its own
    compiled interview and saved namespace: ``execution`` serves ``why``
    (read-only through ``ExplainResolution``) and ``catalog`` serves the
    listings/``check``.
    """
    from docassemble_simulator.catalog import CatalogFailure
    from docassemble_simulator.execution import ExplainResolution

    command = request.command
    if command == "why":
        outcome = request.execution.run(ExplainResolution(request.variable))
        if not outcome.ok:
            return _CatalogPayload(
                _envelope(
                    command,
                    error=outcome.error,
                    diagnostics=outcome.diagnostics,
                ),
                _exit_for_error(outcome.error.kind),
                None,
            )
    else:
        try:
            if command == "check":
                outcome = request.catalog.check()
            elif command == "questions":
                outcome = request.catalog.questions(request.var)
            else:
                outcome = request.catalog.index(request.var)
        except AssemblyLineCompatibilityError as error:
            return _CatalogPayload(
                _envelope(
                    command,
                    error=Failure(
                        ErrorKind.RUNTIME_COMPATIBILITY, str(error), error.details
                    ),
                ),
                _exit_for_error(ErrorKind.RUNTIME_COMPATIBILITY),
                None,
            )
        except CatalogFailure as error:
            return _CatalogPayload(
                _envelope(
                    command,
                    error=Failure(ErrorKind.COMPILE, str(error), {}),
                ),
                2,
                None,
            )
    result = _catalog_result(outcome)
    failure = _catalog_failure(request, result)
    if failure is None:
        return _CatalogPayload(_envelope(command, result), 0, result)
    return _CatalogPayload(
        _envelope(command, error=failure),
        _exit_for_error(failure.kind),
        result,
    )


def cmd_execution(args, root):
    from docassemble_simulator.execution import (
        Answer,
        Evaluate,
        Execute,
        Refresh,
        RowAction,
        SeedState,
        Seek,
        Start,
        Status,
        Variables,
    )
    from docassemble_simulator.trace import TraceError

    if args.command == "start":
        operation = Start()
    elif args.command == "status":
        operation = Status()
    elif args.command == "refresh":
        operation = Refresh()
    elif args.command == "answer":
        operation = Answer(
            tuple(_parse_assignments(args.assignments)),
            args.code,
            not args.no_validate,
            args.strict,
            args.partial,
            args.defaults,
        )
    elif args.command == "row-action":
        operation = RowAction(args.kind, args.target, args.row)
    elif args.command == "seek":
        operation = Seek(
            args.variable, args.fresh, args.activate, args.trace, args.auto
        )
    elif args.command == "eval":
        operation = Evaluate(args.expression)
    elif args.command == "vars":
        operation = Variables(args.filter)
    elif args.command == "seed":
        code = Path(args.file).read_text(encoding="utf-8") if args.file else args.code
        operation = SeedState(code, args.assemble)
    else:
        code = Path(args.file).read_text(encoding="utf-8") if args.file else args.code
        operation = Execute(code, not args.no_assemble)
    execution = _execution(args, root)
    outcome = execution.run(operation)
    diagnostics = outcome.diagnostics
    full_diagnostics = bool(
        getattr(args, "full_seek_diagnostics", False)
        or getattr(args, "seek_diagnostics", None) == "capture"
    )
    if args.json and diagnostics and not full_diagnostics:
        diagnostics = _diagnostic_summary(diagnostics)
    payload = (
        _envelope(
            args.command,
            outcome.result,
            diagnostics=diagnostics,
            attachments=outcome.attachments,
        )
        if outcome.ok
        else _envelope(
            args.command,
            error=outcome.error,
            diagnostics=diagnostics,
            attachments=outcome.attachments,
        )
    )
    if getattr(args, "record", None):
        try:
            _record_execution(args, root, execution, operation, payload)
        except (TraceError, OSError, RuntimeError, ValueError) as error:
            _emit(
                _envelope(args.command, error=Failure(ErrorKind.INPUT, str(error), {})),
                args.json,
            )
            return 1
    _emit(payload, args.json)
    if not outcome.ok:
        return _exit_for_error(outcome.error.kind)
    return 0


def cmd_reset(args, root):
    """Discard every saved session for the selected interview."""
    execution = _execution(args, root)
    removed = execution.reset()
    _emit(
        _envelope(
            "reset",
            {
                "kind": "reset",
                "interview": execution.scope.interview,
                "removed": [str(path) for path in removed],
            },
        ),
        args.json,
    )
    return 0


def cmd_worker(args, root):
    """Run the persistent JSON-lines worker used by agents and test suites."""
    import io

    from docassemble_simulator.worker import Worker, serve

    # Capture the protocol destination before redirecting stdout.  Bootstrap,
    # interview imports, and authored code may print, but those bytes must not
    # become indistinguishable from JSON frames.
    protocol_output = getattr(args, "_worker_protocol_stdout", sys.stdout)
    with redirect_stdout(sys.stderr):
        resolved = getattr(args, "_resolved_configuration", None)
        temporary_session = None
        session_root = args.session_root
        if session_root is None:
            # A worker is a journey boundary. Keep its default state out of the
            # project's ordinary saved-session directory; callers that need to
            # resume across worker processes provide --session-root explicitly.
            worker_directory = root / ".simulator"
            worker_directory.mkdir(parents=True, exist_ok=True)
            temporary_session = tempfile.TemporaryDirectory(
                prefix="worker-session-", dir=worker_directory
            )
            session_root = temporary_session.name
        handle = None
        try:
            worker = Worker(
                root,
                args.interview,
                config_fingerprint=(
                    resolved.fingerprint if resolved is not None else ""
                ),
                config_context=(
                    resolved.session_context() if resolved is not None else None
                ),
                session_root=Path(session_root).expanduser().resolve(),
                timeout=args.timeout,
                timings=args.timings,
                project_root=(
                    Path(args.project_root).expanduser().resolve()
                    if args.project_root
                    else None
                ),
                handlers=_worker_request_handlers(args),
            )
            stream = sys.stdin
            if args.journey:
                handle = Path(args.journey).expanduser().open("r", encoding="utf-8")
                stream = handle
            # A journey file may be either JSONL (the protocol's native format) or
            # one JSON array of requests. JSONL remains the streaming path for
            # stdin and large agent journeys.
            if args.journey:
                contents = stream.read()
                stripped = contents.lstrip()
                if stripped.startswith("["):
                    requests = json.loads(contents)
                    if not isinstance(requests, list):
                        raise ValueError(
                            "journey JSON must contain an array of requests"
                        )
                    stream = io.StringIO(
                        "".join(json.dumps(item) + "\n" for item in requests)
                    )
                else:
                    stream = io.StringIO(contents)
            recorder = _worker_journey_recorder(args, root, worker)
            return serve(worker, stream, protocol_output, observer=recorder)
        finally:
            if handle is not None:
                handle.close()
            if temporary_session is not None:
                temporary_session.cleanup()


def _worker_request_handlers(args):
    """The non-execution requests the worker command serves.

    ``worker.py`` stays an operation-only server (ADR-0017); the command
    adapter owns the ``render`` request, the read-only catalog/project
    requests, and the trace requests, mapping each onto the same payload its
    one-shot command builds.
    """
    return {
        "render": lambda worker, request: _render_worker_request(args, worker, request),
        **_catalog_worker_handlers(args),
        **_trace_worker_handlers(args),
    }


def _catalog_worker_handlers(args):
    """The read-only catalog/project requests the worker command serves.

    Each request returns the ``sim <command> --json`` payload for the worker's
    project, so an agent can switch transports without changing its parsing.
    None of them creates or mutates a Saved session: ``questions``/``index``/
    ``check`` only compile, ``info``/``config`` never touch session state, and
    ``why`` reads the worker's saved namespace through ``ExplainResolution``
    without committing (and stays a static read-only report when there is no
    session).

    The catalog verbs come from :data:`_CATALOG_VERBS`, so adding one there
    registers its request without touching this table.
    """
    return {
        **{
            command: partial(_catalog_worker_request, command=command)
            for command in _CATALOG_VERBS
        },
        "info": lambda worker, request: _project_worker_request(args, worker, "info"),
        "config": lambda worker, request: _project_worker_request(
            args, worker, "config"
        ),
    }


def _worker_catalog_source(worker):
    """The worker's compile authority, shared with its read-only requests.

    The worker's cached catalog owns the current-source watches and the
    compiled identities, and its ``_compile`` revalidates every identity it is
    asked for, so a read-only request compiles through that one instance
    instead of reconstructing a refresh order and a failure handoff.
    """
    return worker.execution.catalog


def _catalog_worker_request(worker, request, *, command):
    """Map one worker catalog request onto the shared catalog payload."""
    verb = _CATALOG_VERBS[command]
    fields = _catalog_worker_fields(verb, command, request)
    from docassemble_simulator._diagnostics import capture_diagnostics

    with capture_diagnostics() as diagnostics:
        built = _catalog_payload(
            verb.request(
                command,
                catalog=lambda: _worker_catalog_source(worker),
                execution=lambda: worker.execution,
                **fields,
            )
        )
    if diagnostics:
        built.envelope["diagnostics"] = _clean(diagnostics)

    return _catalog_worker_outcome(built.envelope)


def _catalog_worker_fields(verb, command, request):
    """Decode a catalog request's optional fields into request-record fields."""
    from docassemble_simulator.worker import WorkerProtocolError

    fields: dict[str, Any] = {}
    if verb.variable_field:
        variable = request.get("variable")
        if not isinstance(variable, str) or not variable.strip():
            raise WorkerProtocolError(f"{command} requires a variable")
        fields["variable"] = variable
    if verb.filter_field:
        var = request.get("var")
        if var is not None and not isinstance(var, str):
            raise WorkerProtocolError(f"{command} var must be a string")
        fields["var"] = var
    if verb.strict_flag:
        strict = request.get("strict", False)
        if not isinstance(strict, bool):
            raise WorkerProtocolError(f"{command} strict must be a boolean")
        fields["strict"] = strict
    return fields


def _project_worker_request(args, worker, command):
    """Map one worker ``info``/``config`` request onto the shared payload."""
    config = getattr(args, "_simulator_config", {})
    result = (
        _info_result(worker.root, config, args)
        if command == "info"
        else _config_result(worker.root, config, args)
    )
    return Outcome(True, _clean(result))


def _catalog_worker_outcome(envelope):
    """Convert a one-shot catalog envelope into the worker's typed Outcome."""
    if envelope["ok"]:
        return Outcome(True, _clean(envelope["result"]))
    error = envelope["error"]
    return Outcome(
        False,
        error=Failure(error["kind"], error["message"], error.get("details") or {}),
        diagnostics=tuple(_clean(envelope.get("diagnostics") or ())),
    )


def _render_worker_request(args, worker, request):
    """Map one worker `render` request and render it with the worker's session.

    The renderer reuses the worker's execution, so a ``saved`` source reads the
    same session the journey produced and ``fresh``/``fixture``/``snapshot``
    stay read-only exactly as they are for ``sim render``.  The result is
    normalized through the CLI's public payload cleaning, so a response's
    ``result`` is the ``sim render --json`` result for the same inputs.
    """
    from docassemble_simulator.render import InterviewRenderer
    from docassemble_simulator.worker import WorkerProtocolError

    try:
        template, source, assemble, save_snapshot, output, expect_missing, bindings = (
            _worker_render_fields(request)
        )
    except InputFailure as error:
        raise WorkerProtocolError(str(error)) from error
    render_request = _render_request(
        template,
        source,
        assemble=assemble,
        save_snapshot=save_snapshot,
        output=output,
        expect_missing=expect_missing,
        bindings=_configured_bindings(
            getattr(args, "_simulator_config", {}), template, bindings
        ),
    )
    outcome = InterviewRenderer(worker.root, worker.execution).render(render_request)
    # ``sim render --json`` drops absent fields (a null artifact when --output
    # is not requested), so the worker response must drop them too.
    return replace(outcome, result=_clean(outcome.result))


def _worker_render_fields(request):
    """Decode a worker `render` request into ``sim render`` inputs."""
    from docassemble_simulator.render import (
        FixtureSource,
        FreshSource,
        SavedSessionSource,
        SnapshotSource,
    )

    template = request.get("template")
    if not isinstance(template, str) or not template.strip():
        raise InputFailure("render requires a template")
    source_name = request.get("source")
    if source_name is None:
        source_name = "saved"
    if not isinstance(source_name, str):
        raise InputFailure("render source must be a string")
    normalized = source_name.strip().lower().replace("_", "-")
    if normalized == "saved":
        source = SavedSessionSource()
    elif normalized == "fresh":
        source = FreshSource()
    elif normalized in {"fixture", "snapshot"}:
        path = _worker_optional_path(request, "path")
        if path is None:
            raise InputFailure(f"render source {normalized} requires a path")
        source = (
            FixtureSource(path) if normalized == "fixture" else SnapshotSource(path)
        )
    else:
        raise InputFailure(f"unknown render source: {normalized}")
    return (
        template,
        source,
        not _worker_flag(request, "no_assemble", "no-assemble"),
        _worker_optional_path(request, "save_snapshot", "save-snapshot"),
        _worker_optional_path(request, "output"),
        _worker_optional_string(request, "expect_missing", "expect-missing"),
        _parse_bindings(_worker_binding_pairs(request.get("bindings"))),
    )


def _worker_field(request, *names):
    """Return the first present request field, accepting both key spellings."""
    for name in names:
        if name in request:
            return request[name]
    return None


def _worker_flag(request, *names) -> bool:
    value = _worker_field(request, *names)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise InputFailure(f"render {names[0]} must be a boolean")
    return value


def _worker_optional_string(request, *names):
    value = _worker_field(request, *names)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InputFailure(f"render {names[0]} must be a string")
    return value


def _worker_optional_path(request, *names):
    value = _worker_field(request, *names)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InputFailure(f"render {names[0]} must be a path string")
    return Path(value).expanduser().resolve()


def _worker_binding_pairs(raw):
    """Normalize a request's `bindings` into NAME=EXPRESSION spellings.

    Only the two shapes the README documents are accepted: an object of
    ``NAME: EXPRESSION``, or an array of ``NAME=EXPRESSION`` strings.
    """
    if raw is None:
        return ()
    if isinstance(raw, dict):
        return tuple(f"{name}={expression}" for name, expression in raw.items())
    if not isinstance(raw, (list, tuple)):
        raise InputFailure("render bindings must be an object or array")
    pairs: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise InputFailure("each render binding must be a NAME=EXPRESSION string")
        pairs.append(item)
    return tuple(pairs)


def _trace_worker_handlers(args):
    """The trace requests the worker command serves.

    Kept in its own helper so registering them stays a one-line addition to
    ``_worker_request_handlers`` and ``worker.py`` stays an operation-only
    server. ``trace-compare`` is read-only over two sidecars; ``trace-replay``
    runs its own isolated execution and never writes the golden.
    """
    return {
        "trace-compare": lambda worker, request: _trace_compare_worker_request(request),
        "trace-replay": lambda worker, request: _trace_replay_worker_request(
            args, worker, request
        ),
    }


def _trace_compare_worker_request(request):
    """Map one worker ``trace-compare`` request onto the compare seam.

    The response ``result`` is the ``sim trace compare --json`` result for the
    same sidecars and policy; a mismatch is the typed ``trace-mismatch`` error
    whose ``details`` are the same report. The request is read-only over two
    sidecar paths, so it needs neither the worker nor an execution: the worker
    never has an ``--update`` path and never writes the golden.
    """
    from docassemble_simulator.trace import TraceError

    policy, expected, actual, exceptions = _trace_compare_worker_fields(request)
    try:
        comparison = _trace_compare_report(expected, actual, policy, exceptions)
    except TraceError as error:
        return _outcome_from_trace_input(error)
    report = _clean(comparison.as_dict())
    if comparison.matched:
        return Outcome(True, result=report)
    return Outcome(
        False,
        error=Failure(
            ErrorKind.TRACE_MISMATCH,
            "screen trace comparison failed",
            report,
        ),
    )


def _trace_compare_worker_fields(request):
    """Decode a ``trace-compare`` request into the compare seam's inputs.

    Decoding failures are ``InputFailure`` at this layer, the same vocabulary
    the render decoders use, and become one typed ``input`` protocol error in
    the handler.
    """
    from docassemble_simulator.worker import WorkerProtocolError

    try:
        _reject_trace_update("compare", request)
        expected = _worker_trace_path(request, "expected")
        actual = _worker_trace_path(request, "actual")
        policy = _worker_trace_policy(request)
        exceptions = _worker_trace_optional_path(request, "exceptions")
    except InputFailure as error:
        raise WorkerProtocolError(str(error)) from error
    return policy, expected, actual, exceptions


def _trace_replay_worker_request(args, worker, request):
    """Map one worker ``trace-replay`` request onto the replay seam.

    The replay runs against its own temporary session directory inside the
    replay seam, so the worker's own Saved session is untouched and the golden
    is only read. That isolated execution compiles through the worker's shared
    catalog, which revalidates the identity it is asked for, so an edit between
    two requests is never replayed as the pre-edit program. The response
    ``result`` is the ``sim trace replay --json`` result for a clean replay; a
    divergence is the typed ``trace-mismatch`` error whose ``details`` are the
    same report.
    """
    from docassemble_simulator.trace import TraceError
    from docassemble_simulator.worker import WorkerProtocolError

    try:
        _reject_trace_update("replay", request)
        golden = _worker_trace_path(request, "golden")
        interview = _worker_trace_interview(request)
        overrides = _worker_trace_overrides(request)
    except InputFailure as error:
        raise WorkerProtocolError(str(error)) from error
    try:
        report = _trace_replay_report(
            worker.root,
            golden,
            interview=interview or worker.identity,
            overrides=overrides,
            resolved_configuration=getattr(args, "_resolved_configuration", None),
            catalog=worker.execution.catalog,
        )
    except TraceError as error:
        return _outcome_from_trace_input(error)
    report = _clean(report)
    if report["clean"]:
        return Outcome(True, result=report)
    return Outcome(
        False,
        error=Failure(
            ErrorKind.TRACE_MISMATCH,
            "screen trace replay diverged",
            report,
        ),
    )


def _outcome_from_trace_input(error):
    """A typed ``input`` outcome for a trace seam's ``TraceError``."""
    return Outcome(False, error=Failure(ErrorKind.INPUT, str(error), {}))


def _reject_trace_update(verb, request):
    """Refuse an ``update`` field: the worker never writes a golden.

    ``--update`` is the CLI's one golden write path, so a worker request that
    asks for it is a typed input error rather than a silently ignored field.
    """
    if _worker_field(request, "update") is not None:
        raise InputFailure(
            f"trace {verb} update is not a worker capability; --update stays CLI-only"
        )


def _worker_trace_path(request, *names):
    """A required trace sidecar path from the request."""
    value = _worker_field(request, *names)
    if not isinstance(value, str) or not value.strip():
        raise InputFailure(f"{names[0]} requires a trace path")
    return value


def _worker_trace_optional_path(request, *names):
    """An optional trace sidecar path from the request."""
    value = _worker_field(request, *names)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InputFailure(f"{names[0]} must be a path string")
    return value


def _worker_trace_interview(request):
    """The optional ``interview`` a replay request names."""
    value = _worker_field(request, "interview")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InputFailure("trace replay interview must be a string")
    return value


def _worker_trace_overrides(request):
    """The ``STEP.FIELD=VALUE`` overrides a replay request carries."""
    raw = _worker_field(request, "overrides", "override")
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if not isinstance(raw, (list, tuple)):
        raise InputFailure("trace replay overrides must be a string or array")
    for item in raw:
        if not isinstance(item, str):
            raise InputFailure(
                "each trace replay override must be a STEP.FIELD=VALUE string"
            )
    return tuple(raw)


def _worker_trace_policy(request):
    """Decode the comparison policy fields of a ``trace-compare`` request."""
    from docassemble_simulator.trace import ComparePolicy

    def choice(name, default):
        value = _worker_field(request, name)
        if value is None:
            return default
        if not isinstance(value, str):
            raise InputFailure(f"trace compare {name} must be a string")
        return value

    full_text = _worker_field(request, "full_text", "full-text")
    if full_text is None:
        full_text = False
    if not isinstance(full_text, bool):
        raise InputFailure("trace compare full_text must be a boolean")
    order = choice("order", "ordered")
    declared = _worker_field(request, "phases")
    phases = _worker_trace_phases(request)
    if declared and order != "phased":
        # The one-shot CLI rejects this pairing (``_validate_trace_arguments``)
        # and treats an empty ``--phases`` as absent; a worker request must not
        # silently ignore a declared phase order.
        raise InputFailure(
            "trace compare phases is only used with order phased; "
            "add order phased or drop phases"
        )
    return ComparePolicy(
        order=order,
        missing=choice("missing", "strict"),
        extra=choice("extra", "strict"),
        full_text=full_text,
        phases=phases,
    )


def _worker_trace_phases(request):
    """The declared phase order a phased comparison request carries."""
    phases = _worker_field(request, "phases")
    if phases is None:
        return None
    if isinstance(phases, str):
        return tuple(item for item in phases.split(",") if item) or None
    if isinstance(phases, (list, tuple)):
        if not all(isinstance(item, str) for item in phases):
            raise InputFailure("trace compare phases must be strings")
        return tuple(phases) or None
    raise InputFailure("trace compare phases must be a string or array")


def _trace_metadata(root, interview, resolved_configuration=None):
    """The trace metadata for one interview and resolved configuration."""
    from docassemble_simulator.catalog import InterviewCatalog
    from docassemble_simulator.compatibility import collect_environment
    from docassemble_simulator.trace import TraceMetadata

    environment = collect_environment()
    simulator = _simulator_version()
    return TraceMetadata(
        interview=InterviewCatalog(root, interview).identity,
        config_fingerprint=(
            resolved_configuration.fingerprint
            if resolved_configuration is not None
            else ""
        ),
        docassemble=environment.versions.get("docassemble-base") or "not installed",
        assemblyline=environment.versions.get("docassemble-assemblyline")
        or "not installed",
        simulator=simulator,
    )


def _command_trace_metadata(root, args):
    """The trace metadata for a one-shot command's interview and configuration."""
    return _trace_metadata(
        root, args.interview, getattr(args, "_resolved_configuration", None)
    )


def _trace_operation_context(operation, command):
    """Return replay data for one public execution operation."""
    from docassemble_simulator.execution import Answer, Execute, Refresh, Seek, Start

    if isinstance(operation, Answer):
        return {
            "intent": "defaults" if operation.defaults else "answer",
            "target": None,
            "fresh": False,
            "activate": False,
            "submitted": dict(operation.assignments),
        }
    if isinstance(operation, Seek):
        return {
            "intent": "seek",
            "target": operation.variable,
            "fresh": operation.fresh,
            "activate": operation.activate,
            "submitted": {},
        }
    if isinstance(operation, Start):
        intent = "start"
    elif isinstance(operation, Refresh):
        intent = "refresh"
    elif isinstance(operation, Execute):
        intent = "exec"
    else:
        intent = command
    return {
        "intent": intent,
        "target": None,
        "fresh": False,
        "activate": False,
        "submitted": {},
    }


def _record_execution(
    args: argparse.Namespace,
    root: str | Path,
    execution: Any,
    operation: Any,
    payload: dict[str, Any],
    *,
    record: str | Path | None = None,
    phase: str | None = None,
    command: str | None = None,
) -> None:
    """Append operation intent and screen outcomes to a trace sidecar.

    The one-shot execution path supplies the trace destination and command on
    ``args``; the worker journey recorder passes the per-request ``record`` and
    ``phase`` and the request's operation name explicitly. Both paths then
    share one recording implementation.
    """
    from docassemble_simulator.execution import Seek, Status
    from docassemble_simulator.trace import append_trace, is_screen_outcome

    if record is None:
        record = args.record
    if phase is None:
        phase = getattr(args, "phase", None)
    if command is None:
        command = getattr(args, "command", None)
    context = _trace_operation_context(operation, command)
    result = payload.get("result") if payload.get("ok") else None
    # Only an automatic seek performed this walk; Status can observe a stored
    # outcome that still carries auto_trace without performing it again.
    auto_trace = (
        result.get("auto_trace")
        if isinstance(operation, Seek) and operation.auto and isinstance(result, dict)
        else None
    )
    if auto_trace:
        metadata = _command_trace_metadata(root, args)
        for index, step in enumerate(auto_trace):
            append_trace(
                record,
                metadata,
                operation=command,
                intent="auto",
                target=context["target"],
                # Preserve the seek's activation policy on every automatic
                # step. Replay also reads it from the trailing post-walk
                # record so schema-4 traces remain compatible.
                activate=context["activate"],
                # A fresh auto-seek has no saved session to answer against.
                # The replay adapter uses this marker to seed its private
                # session once, without changing the user's session.
                fresh=bool(context["fresh"] and index == 0),
                ok=bool(step.get("ok", True)),
                screen=step.get("screen"),
                error=step.get("error"),
                phase=phase,
                submitted=step.get("submitted") or {},
            )

    screen = None
    if payload.get("ok"):
        if is_screen_outcome(payload.get("result")):
            screen = payload["result"]
    else:
        # A failed operation leaves the active screen untouched; the sidecar
        # still records which screen the user was looking at. A stored error
        # outcome is the failure itself, not a screen, so it is left for the
        # error identity below.
        try:
            status = execution.run(Status())
        except Exception:  # noqa: BLE001 - a missing session is not a record failure
            status = None
        if status is not None and status.ok and is_screen_outcome(status.result):
            screen = status.result
    if screen is None and payload.get("ok"):
        return
    # Expanded automatic steps own the fresh marker; the final seek record is
    # the post-walk outcome and must not re-run the walk during replay.
    final_fresh = context["fresh"] and not auto_trace
    append_trace(
        record,
        _command_trace_metadata(root, args),
        operation=command,
        intent=context["intent"],
        target=context["target"],
        fresh=final_fresh,
        activate=context["activate"],
        post_auto=bool(auto_trace),
        ok=bool(payload.get("ok")),
        screen=screen,
        error=payload.get("error"),
        phase=phase,
        submitted=context["submitted"],
    )


def _worker_journey_recorder(
    args: argparse.Namespace, root: str | Path, worker: Any
) -> Callable[[Any, dict[str, Any]], None]:
    """Return a ``serve`` observer that records execution responses.

    A recorded worker response has exactly the one-shot payload shape
    (``ok``/``result``/``error``), so the same scenario recorded through a
    worker journey is equivalent to recording it one process per operation.
    The destination comes from the request's optional ``record``/``phase``
    fields, falling back to the worker command's ``--record PATH --phase
    NAME`` defaults; an absent or ``null`` field means "use the worker
    default", and any other non-string value is an input failure even when the
    journey records nothing. Protocol-only
    requests are skipped, and the worker execution is only touched once a
    request actually asks to record. Recording failures are not caught here:
    ``serve`` isolates them into a typed non-ok response so a truncated trace
    cannot pass unnoticed.
    """
    from docassemble_simulator.worker import (
        WorkerProtocolError,
        operation_from_request,
        operation_name,
    )

    def observe(request: Any, response: dict[str, Any]) -> None:
        if not isinstance(request, dict):
            # ``null``, arrays, and strings are rejected by the protocol
            # handler; there is no request field to record.
            return
        try:
            operation = operation_from_request(request)
        except WorkerProtocolError:
            # ``ping``/``reset``/``shutdown`` and malformed requests are
            # protocol-only: they never produce a recorded screen outcome.
            return
        record = request.get("record") if "record" in request else None
        if record is None:
            record = getattr(args, "record", None)
        # Both request fields are validated before the no-destination return: a
        # non-string ``phase`` is an input error even in a journey that records
        # nothing, exactly as the request/report documentation promises.
        phase = request.get("phase") if "phase" in request else None
        if phase is None:
            phase = getattr(args, "phase", None)
        if phase is not None and not isinstance(phase, str):
            raise InputFailure("a journey phase must be a string or null")
        if record is None:
            return
        if not isinstance(record, str) or not record.strip():
            raise InputFailure("a journey record destination must be a path string")
        # The recorded operation is the canonical one-shot command spelling, so
        # an alias request (``evaluate``) records what ``eval`` records.
        _record_execution(
            args,
            root,
            worker.execution,
            operation,
            response,
            record=record,
            phase=phase,
            command=operation_name(request),
        )

    return observe


def _trace_policy(args):
    from docassemble_simulator.trace import ComparePolicy

    phases = tuple(item for item in (args.phases or "").split(",") if item)
    return ComparePolicy(
        order=args.order,
        missing=args.missing,
        extra=args.extra,
        full_text=args.full_text,
        phases=phases or None,
    )


def _human_trace_report(report, matched):
    counts = report["counts"]
    verdict = "match" if matched else "MISMATCH"
    lines = [
        f"trace compare: {verdict} ({report['order']})",
        (
            f"expected: {counts['expected']}  actual: {counts['actual']}  "
            f"matched: {counts['matched']}  missing: {counts['missing']}  "
            f"extra: {counts['extra']}  duplicates: {counts['duplicates']}"
        ),
    ]
    for phase in report["phases"]:
        lines.append(
            "phase "
            f"{phase['phase']}: expected {phase['expected']} actual {phase['actual']} "
            f"matched {phase['matched']} missing {phase['missing']} extra {phase['extra']}"
        )
    for diff in report["diffs"]:
        label = diff["kind"]
        if diff["excepted"]:
            label += f" (excepted: {diff['excepted']})"
        where = f" #{diff['position']}" if diff.get("position") else ""
        target = f"{diff['identity']} - " if diff["identity"] else ""
        lines.append(f"{label}{where}: {target}{diff['detail']}")
    if not matched:
        lines.append("pass --update to rewrite the golden after review")
    return "\n".join(lines)


def _trace_identity_dict(identity):
    if identity is None:
        return None
    return {
        "key": identity.key,
        "rule": identity.rule,
        "occurrence": identity.occurrence,
    }


def _trace_replay_overrides(raw_overrides, entries):
    by_step = {entry.seq: entry for entry in entries}
    overrides = {}
    for raw in raw_overrides or ():
        if "=" not in raw:
            raise InputFailure(f"expected --override STEP.FIELD=VALUE, got {raw!r}")
        target, value = raw.split("=", 1)
        step_text, separator, field = target.partition(".")
        if not separator or not field:
            raise InputFailure(f"expected --override STEP.FIELD=VALUE, got {raw!r}")
        try:
            step = int(step_text)
        except ValueError as error:
            raise InputFailure(
                f"override step must be an integer: {step_text!r}"
            ) from error
        if step not in by_step:
            raise InputFailure(f"override refers to unknown trace step {step}")
        overrides.setdefault(step, {})[field] = value
    return overrides


def _trace_replay_operation(entry, submitted):
    from docassemble_simulator.execution import Answer, Refresh, Seek, Start, Status
    from docassemble_simulator.trace import TraceError

    intent = entry.intent or entry.operation
    if intent == "start":
        return Start()
    if intent == "status":
        # A recorded status response is a screen observation: replaying it
        # checks the saved screen without submitting anything (#51).
        return Status()
    if intent == "refresh":
        return Refresh()
    if intent == "answer":
        return Answer(tuple(submitted.items()))
    if intent == "defaults":
        return Answer(tuple(submitted.items()), defaults=not submitted)
    if intent == "seek":
        # Pre-intent in-memory fixtures used a seek record's assignments as a
        # follow-up answer. New records carry ``target`` and never use this
        # ambiguity, but retaining it keeps the public test value shape useful.
        if submitted and entry.target is None:
            return Answer(tuple(submitted.items()))
        screen = entry.screen or {}
        variable = entry.target or screen.get("sought") or screen.get("orig_sought")
        if not variable and entry.error:
            variable = (entry.error.get("details") or {}).get("sought_variable")
        if not isinstance(variable, str) or not variable:
            raise TraceError(f"trace step {entry.seq} has no seek variable")
        return Seek(variable, fresh=entry.fresh, activate=entry.activate)
    if intent == "auto":
        # Automatic records are expanded below so the replay can compare the
        # pre-submission screen and then apply the browser-shaped transaction.
        return Answer(tuple(submitted.items()), defaults=not submitted)
    raise TraceError(
        f"trace step {entry.seq} records compare-only operation "
        f"{entry.operation!r}: replay accepts start, status, refresh, answer, "
        "seek, and automatic records"
    )


def _trace_replay_actual(execution, operation):
    from docassemble_simulator.execution import Status
    from docassemble_simulator.trace import is_screen_outcome

    outcome = execution.run(operation)
    actual_error = _clean(outcome.error) if not outcome.ok else None
    screen = (
        outcome.result if outcome.ok and is_screen_outcome(outcome.result) else None
    )
    if screen is None and not outcome.ok:
        status = execution.run(Status())
        if status.ok and is_screen_outcome(status.result):
            screen = status.result
    return outcome.ok, screen, actual_error


def _trace_replay_observation(
    entry,
    submitted,
    actual_ok,
    actual_screen,
    actual_error,
    actual_entries=(),
    actual_submitted=None,
):
    """Build one adapter report from an execution observation.

    ``actual_submitted`` is what execution actually submitted for the step; it
    defaults to the same assignments because a completed observation submits
    the recorded transaction.  A step that stopped before submission was only
    observed, so it passes an empty mapping.
    """
    from docassemble_simulator.trace import (
        TraceEntry,
        assign_identity,
        compare_replay_entries,
    )

    intent = entry.intent or entry.operation
    observed_submission = (
        dict(submitted) if actual_submitted is None else dict(actual_submitted)
    )
    actual_identity = assign_identity(
        actual_screen,
        actual_error,
        tuple(actual_entries),
        entry.phase,
    )
    actual_entry = TraceEntry(
        seq=entry.seq,
        operation=entry.operation,
        phase=entry.phase,
        submitted=observed_submission,
        ok=actual_ok,
        screen=actual_screen,
        identity=actual_identity,
        content=(),
        error=actual_error,
        intent=intent,
        target=entry.target,
        fresh=entry.fresh,
        activate=entry.activate,
        post_auto=entry.post_auto,
    )
    verdict = compare_replay_entries(entry, actual_entry)
    return {
        "_actual_entry": actual_entry,
        "step": entry.seq,
        "operation": entry.operation,
        "intent": intent,
        "submitted": submitted,
        "match": verdict.matched,
        "identity_match": verdict.identity_match,
        "field_facts_match": verdict.field_facts_match,
        "flow_match": verdict.flow_match,
        "error_match": verdict.error_match,
        "expected": {
            "identity": _trace_identity_dict(entry.identity),
            "content": list(verdict.expected_content),
            "ok": entry.ok,
            "error": entry.error,
            "intent": intent,
            "submitted": entry.submitted,
        },
        "actual": {
            "identity": _trace_identity_dict(actual_identity),
            "content": list(verdict.actual_content),
            "ok": actual_ok,
            "error": actual_error,
            "intent": intent,
            "submitted": observed_submission,
        },
    }


def _trace_replay_step(entry, submitted, execution, actual_entries=()):
    """Replay one non-grouped trace entry.

    The legacy automatic branch remains for hand-written schema-4 fixtures
    without a post-walk marker. Recorder-produced automatic walks use the
    grouped operation below so their target context and activation policy stay
    operation-local.
    """
    from docassemble_simulator.trace import is_screen_outcome

    intent = entry.intent or entry.operation
    if intent == "auto":
        from docassemble_simulator.execution import Seek, Status

        status = execution.run(Status())
        if not status.ok and entry.fresh:
            if not entry.target:
                raise InputFailure(f"trace step {entry.seq} has no seek variable")
            status = execution.run(Seek(entry.target, fresh=True, activate=True))
        actual_screen = (
            status.result if status.ok and is_screen_outcome(status.result) else None
        )
        operation = _trace_replay_operation(entry, submitted)
        outcome = execution.run(operation)
        actual_ok = outcome.ok
        actual_error = _clean(outcome.error) if not outcome.ok else None
    elif intent == "seek" and entry.post_auto:
        from docassemble_simulator.execution import Status

        status = execution.run(Status())
        actual_ok = status.ok
        actual_error = _clean(status.error) if not status.ok else None
        actual_screen = (
            status.result if status.ok and is_screen_outcome(status.result) else None
        )
    else:
        operation = _trace_replay_operation(entry, submitted)
        actual_ok, actual_screen, actual_error = _trace_replay_actual(
            execution, operation
        )
    return _trace_replay_observation(
        entry,
        submitted,
        actual_ok,
        actual_screen,
        actual_error,
        actual_entries,
    )


_REPLAY_VERDICT_FLAGS = (
    "identity_match",
    "field_facts_match",
    "flow_match",
    "error_match",
)


def _trace_replay_divergence_report(entry, submitted, divergence, actual_entries):
    """Render the step execution flagged as divergent.

    Execution owns the persistence decision, so the adapter reports its
    observation and copies the trace-owned verdict instead of inferring a
    mismatch from the report shape.
    """
    observation = divergence.get("observation")
    observation = observation if isinstance(observation, dict) else {}
    report = _trace_replay_observation(
        entry,
        submitted,
        bool(observation.get("ok", False)),
        observation.get("screen"),
        observation.get("error"),
        actual_entries,
        divergence.get("submitted") or {},
    )
    actual_entries.append(report.pop("_actual_entry"))
    verdict = divergence.get("verdict")
    verdict = verdict if isinstance(verdict, dict) else {}
    for flag in _REPLAY_VERDICT_FLAGS:
        report[flag] = bool(verdict.get(flag, False))
    report["match"] = all(report[flag] for flag in _REPLAY_VERDICT_FLAGS)
    return report


def _trace_replay_automatic_group(entries, start, overrides, execution, actual_entries):
    """Replay one recorded automatic walk in one operation-local context.

    Execution owns the submission boundary and the trailing verdict; this
    adapter only turns the resulting observations into step reports.
    """
    from docassemble_simulator.execution import ReplayAutomatic
    from docassemble_simulator.trace import (
        is_screen_outcome,
        replay_expectation,
        replay_identity_context,
    )

    first = entries[start]
    end = start
    while (
        end < len(entries) and (entries[end].intent or entries[end].operation) == "auto"
    ):
        end += 1
    post = entries[end] if end < len(entries) and entries[end].post_auto else None
    target = first.target
    if not isinstance(target, str) or not target:
        raise InputFailure(f"trace step {first.seq} has no automatic seek target")
    activate = post.activate if post is not None else first.activate
    group_entries = entries[start:end]
    submissions = tuple(
        tuple(overrides.get(entry.seq, dict(entry.submitted)).items())
        for entry in group_entries
    )
    outcome = execution.run(
        ReplayAutomatic(
            target,
            submissions=submissions,
            fresh=first.fresh,
            activate=activate,
            expectations=tuple(replay_expectation(entry) for entry in group_entries),
            identity_context=replay_identity_context(actual_entries),
            trailing=replay_expectation(post) if post is not None else None,
        )
    )
    result = outcome.result if outcome.ok and isinstance(outcome.result, dict) else {}
    details = (
        outcome.error.details if not outcome.ok and outcome.error is not None else {}
    )
    actual_steps = result.get("auto_trace") or (
        details.get("auto_trace") if isinstance(details, dict) else []
    )
    actual_steps = actual_steps if isinstance(actual_steps, list) else []
    observed = result or details
    divergence = (
        observed.get("replay_divergence") if isinstance(observed, dict) else None
    )
    divergence = divergence if isinstance(divergence, dict) else None
    divergence_seq = divergence.get("seq") if divergence is not None else None
    reports = []
    for offset, entry in enumerate(group_entries):
        if entry.seq == divergence_seq:
            reports.append(
                _trace_replay_divergence_report(
                    entry, dict(submissions[offset]), divergence, actual_entries
                )
            )
            return reports, end + (1 if post is not None else 0)
        actual = actual_steps[offset] if offset < len(actual_steps) else {}
        actual = actual if isinstance(actual, dict) else {}
        report = _trace_replay_observation(
            entry,
            dict(submissions[offset]),
            bool(actual.get("ok", False)),
            actual.get("screen"),
            actual.get("error"),
            actual_entries,
            actual.get("submitted") or {},
        )
        actual_entries.append(report.pop("_actual_entry"))
        reports.append(report)
        if not report["match"]:
            return reports, end + (1 if post is not None else 0)
    if post is not None:
        if post.seq == divergence_seq:
            reports.append(
                _trace_replay_divergence_report(
                    post,
                    dict(overrides.get(post.seq, dict(post.submitted))),
                    divergence,
                    actual_entries,
                )
            )
            return reports, end + 1
        actual_error = _clean(outcome.error) if not outcome.ok else None
        actual_screen = result if outcome.ok and is_screen_outcome(result) else None
        report = _trace_replay_observation(
            post,
            dict(overrides.get(post.seq, dict(post.submitted))),
            outcome.ok,
            actual_screen,
            actual_error,
            actual_entries,
        )
        actual_entries.append(report.pop("_actual_entry"))
        reports.append(report)
    return reports, end + (1 if post is not None else 0)


def _trace_replay_report(
    root,
    golden,
    *,
    interview=None,
    overrides=(),
    resolved_configuration=None,
    catalog=None,
):
    """Run ``trace replay`` and return its report without emitting or exiting.

    Shared by ``cmd_trace_replay`` and the worker's ``trace-replay`` handler so
    both compute the same ``sim trace replay --json`` report (``golden``,
    ``metadata``, ``clean``, ``steps``, ``first_divergence``, ``counts``) and
    raise the same ``TraceError`` for an unreadable golden, an interview or
    config mismatch, a malformed override, or a step that records an operation
    replay cannot reconstruct (``eval``, ``vars``, ``seed``, or ``exec``).
    ``interview`` is the requested interview (``None`` accepts the golden's
    own) and ``resolved_configuration`` the caller's already-resolved effective
    configuration, so neither caller has to fabricate a command namespace.
    ``catalog`` is the worker's shared compile authority: the replay's isolated
    execution compiles through it, so it observes the current source without
    the worker refreshing first. The replay always runs against its own
    temporary session directory, so it never touches a caller's Saved session
    and never writes the golden.
    """
    from docassemble_simulator.execution import InterviewExecution
    from docassemble_simulator.trace import TraceError, load_trace

    golden_path = Path(golden).expanduser()
    recorded = load_trace(golden_path)
    if interview and interview != recorded.metadata.interview:
        raise TraceError(
            "trace interview mismatch: "
            f"{interview!r} does not match {recorded.metadata.interview!r}"
        )
    current = _trace_metadata(root, recorded.metadata.interview, resolved_configuration)
    for label, expected, actual in (
        ("interview definition", recorded.metadata.interview, current.interview),
        (
            "config fingerprint",
            recorded.metadata.config_fingerprint,
            current.config_fingerprint,
        ),
    ):
        if expected != actual:
            raise TraceError(
                f"trace {label} mismatch: {expected!r} does not match {actual!r}"
            )
    try:
        overrides = _trace_replay_overrides(overrides, recorded.entries)
    except InputFailure as error:
        raise TraceError(str(error)) from error

    scope_parent = root / ".simulator"
    scope_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="trace-replay-", dir=scope_parent) as scope:
        execution = InterviewExecution(
            root,
            recorded.metadata.interview,
            config_fingerprint=(
                resolved_configuration.fingerprint
                if resolved_configuration is not None
                else ""
            ),
            config_context=(
                resolved_configuration.session_context()
                if resolved_configuration is not None
                else None
            ),
            catalog=catalog,
            session_directory=Path(scope) / "sessions",
        )
        steps = []
        actual_entries = []
        first_divergence = None
        index = 0
        while index < len(recorded.entries):
            entry = recorded.entries[index]
            if (entry.intent or entry.operation) == "auto":
                # Recorder-produced walks have one post-walk marker after all
                # consecutive automatic screens. Find that trailing marker
                # before choosing the legacy per-entry compatibility path.
                post_index = index + 1
                while (
                    post_index < len(recorded.entries)
                    and (
                        recorded.entries[post_index].intent
                        or recorded.entries[post_index].operation
                    )
                    == "auto"
                ):
                    post_index += 1
                if (
                    post_index < len(recorded.entries)
                    and recorded.entries[post_index].post_auto
                ):
                    grouped, index = _trace_replay_automatic_group(
                        recorded.entries, index, overrides, execution, actual_entries
                    )
                    steps.extend(grouped)
                    divergence = next(
                        (step for step in grouped if not step["match"]), None
                    )
                    if divergence is not None:
                        first_divergence = divergence
                        break
                    continue
            submitted = dict(entry.submitted)
            submitted.update(overrides.get(entry.seq, {}))
            step = _trace_replay_step(entry, submitted, execution, actual_entries)
            actual_entries.append(step.pop("_actual_entry"))
            steps.append(step)
            index += 1
            if not step["match"]:
                first_divergence = step
                break

    return {
        "golden": str(golden_path),
        "metadata": recorded.metadata.as_dict(),
        "clean": first_divergence is None and len(steps) == len(recorded.entries),
        "steps": steps,
        "first_divergence": first_divergence,
        "counts": {
            "expected": len(recorded.entries),
            "replayed": len(steps),
            "matched": sum(step["match"] for step in steps),
        },
    }


def cmd_trace_replay(args, root):
    from docassemble_simulator.trace import TraceError

    try:
        report = _trace_replay_report(
            root,
            args.golden,
            interview=args.interview,
            overrides=args.overrides,
            resolved_configuration=getattr(args, "_resolved_configuration", None),
        )
    except TraceError as error:
        _emit(
            _envelope("trace", error=Failure(ErrorKind.INPUT, str(error), {})),
            args.json,
        )
        return _exit_for_error(ErrorKind.INPUT)

    if report["clean"]:
        if args.json:
            _emit(_envelope("trace", report), True)
        else:
            print(
                f"trace replay: match ({report['counts']['matched']}/"
                f"{report['counts']['expected']} steps)"
            )
        return 0
    if args.json:
        _emit(
            _envelope(
                "trace",
                error=Failure(
                    ErrorKind.TRACE_MISMATCH,
                    "screen trace replay diverged",
                    report,
                ),
            ),
            True,
        )
    else:
        step = report["first_divergence"] or {}
        print(
            f"trace replay: DIVERGENCE at step {step.get('step', '?')} "
            f"({step.get('operation', 'unknown')})"
        )
        print(_human(step))
    return _exit_for_error(ErrorKind.TRACE_MISMATCH)


def _trace_compare_report(expected, actual, policy, exceptions=None):
    """Compare two recorded traces without emitting or writing the golden.

    Shared by ``cmd_trace`` and the worker's ``trace-compare`` handler so both
    return the same ``trace compare --json`` report and the same typed input
    errors. A malformed trace or exception file raises ``TraceError``; a
    mismatch is a normal return value whose ``matched`` is ``False``.
    """
    return _trace_compare(expected, actual, policy, exceptions)[0]


def _trace_compare(expected, actual, policy, exceptions=None):
    """Load both sidecars, compare them, and return the compared actual trace.

    The comparison and the trace it compared travel together so ``--update``
    writes exactly the snapshot the report describes instead of re-reading a
    sidecar another process may have replaced in between.
    """
    from docassemble_simulator.trace import (
        compare_traces,
        load_exceptions,
        load_trace,
    )

    expected_trace = load_trace(expected)
    actual_trace = load_trace(actual)
    exceptions_loaded = load_exceptions(exceptions) if exceptions else ()
    comparison = compare_traces(expected_trace, actual_trace, policy, exceptions_loaded)
    return comparison, actual_trace


def cmd_trace(args, root):
    from docassemble_simulator.trace import TraceError, write_trace

    try:
        comparison, actual_trace = _trace_compare(
            args.expected, args.actual, _trace_policy(args), args.exceptions
        )
    except TraceError as error:
        _emit(
            _envelope("trace", error=Failure(ErrorKind.INPUT, str(error), {})),
            args.json,
        )
        return 1
    report = comparison.as_dict()
    if args.update:
        try:
            write_trace(args.expected, actual_trace)
        except OSError as error:
            _emit(
                _envelope(
                    "trace",
                    error=Failure(
                        ErrorKind.INPUT, f"could not update golden: {error}", {}
                    ),
                ),
                args.json,
            )
            return 1
        report = {**report, "updated": str(Path(args.expected).expanduser())}
        if args.json:
            _emit(_envelope("trace", report), True)
        else:
            print(
                "trace compare: golden updated "
                f"{Path(args.expected).expanduser()} "
                f"({'match' if comparison.matched else 'MISMATCH'} before update)"
            )
        return 0
    if comparison.matched:
        if args.json:
            _emit(_envelope("trace", report), True)
        else:
            print(_human_trace_report(report, True))
        return 0
    if args.json:
        _emit(
            _envelope(
                "trace",
                error=Failure(
                    ErrorKind.TRACE_MISMATCH,
                    "screen trace comparison failed",
                    report,
                ),
            ),
            True,
        )
    else:
        print(_human_trace_report(report, False))
    return 2


def _render_request(
    template,
    source,
    *,
    assemble,
    save_snapshot=None,
    output=None,
    expect_missing=None,
    bindings=(),
):
    """Build one render request; a fixture source never assembles.

    Shared by ``sim render`` and the worker's ``render`` handler so the two
    cannot drift on the seven request fields or the fixture rule.
    """
    from docassemble_simulator.render import FixtureSource, RenderRequest

    return RenderRequest(
        template,
        source,
        False if isinstance(source, FixtureSource) else assemble,
        save_snapshot,
        output,
        expect_missing,
        tuple(bindings),
    )


def cmd_render(args, root):
    from docassemble_simulator.render import (
        FixtureSource,
        FreshSource,
        InterviewRenderer,
        SavedSessionSource,
        SnapshotSource,
    )

    if args.fixture:
        source = FixtureSource(Path(args.fixture).expanduser().resolve())
    elif args.snapshot_source:
        source = SnapshotSource(Path(args.snapshot_source).expanduser().resolve())
    elif args.fresh:
        source = FreshSource()
    else:
        source = SavedSessionSource()
    request = _render_request(
        args.template,
        source,
        assemble=not args.no_assemble,
        save_snapshot=Path(args.save_snapshot).expanduser().resolve()
        if args.save_snapshot
        else None,
        output=Path(args.output).expanduser().resolve() if args.output else None,
        expect_missing=args.expect_missing,
        bindings=_configured_bindings(
            getattr(args, "_simulator_config", {}),
            args.template,
            _parse_bindings(args.bind),
        ),
    )
    outcome = InterviewRenderer(root, _execution(args, root)).render(request)
    payload = (
        _envelope(
            "render",
            outcome.result,
            diagnostics=outcome.diagnostics,
            attachments=outcome.attachments,
        )
        if outcome.ok
        else _envelope(
            "render",
            error=outcome.error,
            diagnostics=outcome.diagnostics,
            attachments=outcome.attachments,
        )
    )
    _emit(payload, args.json)
    if outcome.ok:
        return 0
    return _exit_for_error(outcome.error.kind)


def _parse_bindings(pairs):
    result = []
    for pair in pairs:
        if "=" not in pair:
            raise InputFailure(f"expected NAME=EXPRESSION, got {pair!r}")
        name, expression = pair.split("=", 1)
        name = name.strip()
        if not name.isidentifier():
            raise InputFailure(
                f"render binding name must be a simple Python name: {name!r}"
            )
        if not expression.strip():
            raise InputFailure(f"render binding {name!r} has an empty expression")
        result.append((name, expression))
    return result


def _parse_assignments(pairs):
    result = []
    for pair in pairs:
        if "=" not in pair:
            raise InputFailure(f"expected VAR=VALUE, got {pair!r}")
        variable, value = pair.split("=", 1)
        result.append((variable.strip(), value))
    return result


def build_parser():
    parser = UsageParser(
        prog="docassemble-simulator",
        description=(
            "Run and render docassemble interviews locally. Config is discovered "
            "from global and walking-up project TOML files; defaults provide "
            "fake Redis, local storage, and foreground background actions, with "
            "no server database substituted. "
            "DOCX is supported, but PDF conversion/downloads are deployment-only. "
            "JSON uses a stable ok/command/result-or-error envelope."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_simulator_version()}",
    )
    parser.add_argument("--root", default=None)
    parser.add_argument("--interview", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--stub-defined", action="store_true")
    parser.add_argument(
        "--config",
        default=None,
        help="override config file (YAML or TOML); sessions are scoped by the effective configuration",
    )
    parser.add_argument(
        "--offline", action="store_true", help="do not acquire missing runtime packages"
    )
    parser.add_argument(
        "--background-actions",
        choices=("foreground", "disabled"),
        help="background_action mode (default: foreground; no Celery worker)",
    )
    parser.add_argument(
        "--seek-diagnostics",
        choices=("capture", "off"),
        help="capture the variable-seeking trace (default output is summarized; off suppresses it)",
    )
    parser.add_argument(
        "--full-seek-diagnostics",
        action="store_true",
        help="return the complete ordered seek trace instead of the bounded JSON summary",
    )
    common = argparse.ArgumentParser(add_help=False)
    for flag in ("root", "interview"):
        common.add_argument(f"--{flag}", default=argparse.SUPPRESS)
    common.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        help="override config file (YAML or TOML); sessions are scoped by the effective configuration",
    )
    common.add_argument(
        "--config-layer",
        action="append",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    common.add_argument(
        "--stub-defined", action="store_true", default=argparse.SUPPRESS
    )
    common.add_argument("--offline", action="store_true", default=argparse.SUPPRESS)
    common.add_argument(
        "--background-actions",
        choices=("foreground", "disabled"),
        default=argparse.SUPPRESS,
    )
    common.add_argument(
        "--seek-diagnostics",
        choices=("capture", "off"),
        default=argparse.SUPPRESS,
        help="capture the seek trace (JSON summarizes it unless capture/full is explicit)",
    )
    common.add_argument(
        "--full-seek-diagnostics",
        action="store_true",
        default=argparse.SUPPRESS,
        help="return the complete ordered seek trace in JSON",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, **kwargs):
        return sub.add_parser(name, parents=[common], command_name=name, **kwargs)

    add(
        "info",
        help="inspect workspace, config defaults, and DOCX/PDF capabilities",
        description="Show config discovery, local service defaults, and capability boundaries. No runtime bootstrap is required.",
    ).set_defaults(func=cmd_info)
    add(
        "config",
        help="inspect effective simulator configuration (secrets redacted)",
        description="Show discovered config, simulator settings, redacted pass-through values, and the config_fingerprint that scopes sessions.",
    ).set_defaults(func=cmd_config)
    check = add("check", help="compile interview definitions")
    check.add_argument(
        "--strict",
        action="store_true",
        help="treat accidental duplicate explicit ids as a failure",
    )
    check.set_defaults(func=cmd_catalog)
    for name in ("questions", "index"):
        item = add(name, help=f"inspect interview {name}")
        item.add_argument("--var")
        item.set_defaults(func=cmd_catalog)
    why = add(
        "why",
        help="explain which blocks can resolve a variable",
        description=(
            "List variable-resolution candidates in the real runtime's order, "
            "with provenance and guard text. A saved session evaluates guards "
            "through prepared read-only execution; without one, the report is "
            "static and does not create state."
        ),
    )
    why.add_argument("variable")
    why.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when the candidate set has a resolution conflict",
    )
    why.set_defaults(func=cmd_catalog)

    recording_epilog = (
        "Example:\n"
        "  docassemble-simulator start --record run.jsonl --phase intake\n"
        "  docassemble-simulator answer user_name=Alice --record run.jsonl --phase intake\n"
        "  docassemble-simulator trace compare golden.jsonl run.jsonl"
    )

    def add_recording(parser_):
        parser_.add_argument(
            "--record",
            metavar="PATH",
            help=(
                "append this operation's screen outcome to a JSONL trace; "
                "compare traces with `docassemble-simulator trace compare`"
            ),
        )
        parser_.add_argument(
            "--phase",
            metavar="NAME",
            help=(
                "tag recorded entries with a scenario phase (requires --record; "
                "used by `trace compare --order phased`)"
            ),
        )
        return parser_

    add_recording(
        add(
            "start",
            help="create and assemble a fresh session for the effective configuration",
            epilog=recording_epilog,
        )
    ).set_defaults(func=cmd_execution)
    add("status", help="read the saved outcome without assembly").set_defaults(
        func=cmd_execution
    )
    add_recording(
        add(
            "refresh",
            help="rehydrate and reassemble saved state",
            epilog=recording_epilog,
        )
    ).set_defaults(func=cmd_execution)
    add(
        "reset",
        help="discard every saved session for the selected interview",
        description=(
            "Remove saved state for the selected interview, including any "
            "session stored under a different effective configuration. Use it "
            "as the one-shot escape when a command reports a saved session "
            "under a different effective configuration."
        ),
    ).set_defaults(func=cmd_reset)
    row_action = add(
        "row-action", help="press an add, delete, or edit control on the saved screen"
    )
    row_action.add_argument("kind", choices=("add", "delete", "edit"))
    row_action.add_argument("target")
    row_action.add_argument("--row", type=int)
    row_action.set_defaults(func=cmd_execution)
    answer = add_recording(
        add(
            "answer",
            help="transactionally answer the active screen",
            description=(
                "Submit assignments to the active screen. Values are parsed in "
                "JSON, then Python literal, then plain-text order. Blank values "
                "use browser semantics; checkbox and object groups use the forms "
                "shown in the active screen description."
            ),
            epilog=(
                recording_epilog + "\n\n"
                "Value grammar:\n"
                "  JSON first, then Python literals, then plain text. Use "
                'field= for a browser blank, field={"choice":true} for '
                'checkbox groups, field=["choice"] for object multiselects, '
                'field="choice" for an object choice, and field=YYYY-MM-DD '
                "for dates."
            ),
        )
    )
    answer.add_argument("assignments", nargs="*")
    answer.add_argument(
        "--defaults",
        action="store_true",
        help="submit visible defaults and browser-blank values without assignments",
    )
    answer.add_argument("--code", action="store_true")
    answer.add_argument(
        "--no-validate",
        action="store_true",
        help=(
            "skip the interview's validation code; the required-field gate "
            "still applies unless --partial is also passed"
        ),
    )
    answer.add_argument(
        "--strict",
        action="store_true",
        help="treat the permissive mode's warnings as errors",
    )
    answer.add_argument(
        "--partial",
        action="store_true",
        help=(
            "submit only the given fields instead of the whole screen: missing "
            "required fields become warnings and visible optional fields are not "
            "filled in (pre-screen-submission behavior)"
        ),
    )
    answer.set_defaults(func=cmd_execution)
    seek = add_recording(
        add(
            "seek",
            help="seek a variable from saved state by default",
            epilog=recording_epilog,
        )
    )
    seek.add_argument("variable")
    seek.add_argument("--fresh", action="store_true")
    seek.add_argument("--activate", action="store_true")
    seek.add_argument("--trace", action="store_true")
    seek.add_argument(
        "--auto",
        action="store_true",
        help="auto-submit defaultable screens until the variable resolves or input is needed",
    )
    seek.set_defaults(func=cmd_execution)
    evaluate = add("eval", help="evaluate an expression without saving")
    evaluate.add_argument("expression")
    evaluate.set_defaults(func=cmd_execution)
    execute = add_recording(
        add(
            "exec",
            help="execute Python and assemble by default",
            epilog=recording_epilog,
        )
    )
    execute.add_argument("code", nargs="?", default="")
    execute.add_argument("--file")
    execute.add_argument("--no-assemble", action="store_true")
    execute.set_defaults(func=cmd_execution)
    seed = add(
        "seed",
        help="explicitly execute code to seed a local test namespace",
        description=(
            "Run local Python in the saved interview namespace as an explicit "
            "test escape hatch. This is not a browser submission."
        ),
    )
    seed.add_argument("code", nargs="?", default="")
    seed.add_argument("--file")
    seed.add_argument(
        "--assemble",
        action="store_true",
        help="assemble immediately after seeding instead of requiring refresh",
    )
    seed.set_defaults(func=cmd_execution)
    worker_help = "serve a persistent JSON-lines worker for one project and interview"
    for worker_name in ("worker", "run", "journey"):
        worker = add(
            worker_name,
            help=worker_help,
            description=(
                "Read one JSON request per line and return one structured response "
                "per operation. The process keeps runtime bootstrap and the "
                "session scope alive until EOF or shutdown."
            ),
        )
        worker.add_argument(
            "--journey", help="read requests from a JSONL or JSON-array file"
        )
        worker.add_argument(
            "--session-root",
            help=(
                "directory for this worker's saved session "
                "(default: temporary isolated session)"
            ),
        )
        worker.add_argument(
            "--project-root",
            help="original project root to report separately from an isolated workspace",
        )
        worker.add_argument(
            "--timeout",
            type=float,
            default=30.0,
            help="per-operation timeout in seconds (default: 30)",
        )
        worker.add_argument(
            "--timings",
            action="store_true",
            help="include operation and worker-startup timings in responses",
        )
        worker.add_argument(
            "--record",
            metavar="PATH",
            help=(
                "append every execution response's screen outcome to a JSONL "
                "trace; a request may override this with its own `record` field"
            ),
        )
        worker.add_argument(
            "--phase",
            metavar="NAME",
            help=(
                "default phase tag for recorded journey entries; a request may "
                "override this with its own `phase` field"
            ),
        )
        worker.set_defaults(func=cmd_worker)
    variables = add("vars", help="inspect saved variables without saving")
    variables.add_argument("--filter")
    variables.set_defaults(func=cmd_execution)
    trace = add(
        "trace",
        help="compare screen traces recorded with --record",
        description=(
            "Record a screen trace with --record PATH on an execution command, "
            "then compare a later run against it. Ordered, unordered, and phased "
            "matching all report coverage counts."
        ),
        epilog=recording_epilog,
    )
    trace_sub = trace.add_subparsers(dest="trace_command", required=True)
    trace_replay = trace_sub.add_parser(
        "replay",
        parents=[common],
        command_name="trace",
        help="replay a golden screen trace from fresh state",
        description=(
            "Replay a golden screen trace in an isolated session. The first "
            "identity, field-fact, flow, or error mismatch stops the run."
        ),
    )
    trace_replay.add_argument("golden", help="golden trace to replay without modifying")
    trace_replay.add_argument(
        "--override",
        dest="overrides",
        action="append",
        default=[],
        metavar="STEP.FIELD=VALUE",
        help="override one recorded assignment; may be repeated",
    )
    trace_replay.set_defaults(func=cmd_trace_replay)
    trace_compare = trace_sub.add_parser(
        "compare",
        parents=[common],
        command_name="trace",
        help="compare a golden trace with a run's trace",
        description=(
            "Compare a golden screen trace with the trace of the run under test. "
            "Exit 0 on match; 2 on mismatch with the full report; 1 on unreadable, "
            "malformed, or mismatched traces."
        ),
        epilog=(
            "Examples:\n"
            "  docassemble-simulator trace compare golden.jsonl run.jsonl\n"
            "  docassemble-simulator trace compare golden.jsonl run.jsonl --order phased --phases intake,documents\n"
            "  docassemble-simulator trace compare golden.jsonl run.jsonl --update"
        ),
    )
    trace_compare.add_argument(
        "expected", help="golden trace recorded from a known-good run"
    )
    trace_compare.add_argument("actual", help="trace recorded from the run under test")
    trace_compare.add_argument(
        "--order",
        choices=("ordered", "unordered", "phased"),
        default="ordered",
        help=(
            "ordered pins the exact sequence (default); unordered compares as a "
            "multiset; phased orders declared phases but not entries within them"
        ),
    )
    trace_compare.add_argument(
        "--missing",
        choices=("strict", "allow"),
        default="strict",
        help="strict fails on a missing expected screen (default); allow reports it",
    )
    trace_compare.add_argument(
        "--extra",
        choices=("strict", "allow"),
        default="strict",
        help="strict fails on an unexpected screen (default); allow reports it",
    )
    trace_compare.add_argument(
        "--full-text",
        action="store_true",
        help="also compare normalized question and subquestion text",
    )
    trace_compare.add_argument(
        "--phases",
        metavar="P1,P2,...",
        help=(
            "declared phase order for --order phased, e.g. "
            "intake,documents,download; without it the golden's recorded order "
            "is used"
        ),
    )
    trace_compare.add_argument(
        "--exceptions",
        metavar="FILE",
        help=(
            "TOML of reviewed identity tolerances; order violations and "
            "simulator faults cannot be excused"
        ),
    )
    trace_compare.add_argument(
        "--update",
        action="store_true",
        help="rewrite EXPECTED from ACTUAL; the only golden write path",
    )
    trace_compare.set_defaults(func=cmd_trace)
    render = add(
        "render",
        help="render a DOCX from one explicit state source",
        description=(
            "Render DOCX only. PDF conversion is unavailable and no external "
            "converter is invoked. Use --bind NAME=EXPRESSION for explicit "
            "ephemeral template bindings; configured defaults come from "
            "[simulator.render_bindings]. See README § Rendering."
        ),
        epilog=(
            "The persistent worker accepts the same request as "
            '{"operation":"render", ...}: template, source, path, bindings, '
            "no_assemble, output, save_snapshot, and expect_missing. See README "
            "§ Persistent journeys and the standard runner."
        ),
    )
    render.add_argument("template")
    render_source = render.add_mutually_exclusive_group()
    render_source.add_argument("--fresh", action="store_true")
    render_source.add_argument("--snapshot", dest="snapshot_source")
    render_source.add_argument(
        "--fixture",
        metavar="PATH",
        help=(
            "exec a Python file in a fresh render namespace; never assembles "
            "(see README § Rendering)"
        ),
    )
    render.add_argument("--no-assemble", action="store_true")
    render.add_argument("--save-snapshot")
    render.add_argument("--output", metavar="PATH")
    render.add_argument("--expect-missing")
    render.add_argument(
        "--bind",
        action="append",
        default=[],
        metavar="NAME=EXPRESSION",
        help=(
            "bind a template variable in the ephemeral render namespace "
            "(repeatable; defaults come from [simulator.render_bindings])"
        ),
    )
    render.set_defaults(func=cmd_render)
    return parser


def _reexec_with_dyld_path():
    import os

    value = dyld_fallback_value()
    if value == os.environ.get("DYLD_FALLBACK_LIBRARY_PATH"):
        return
    os.environ["DASIMULATOR_REEXEC"] = "1"
    os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = value
    os.execv(sys.executable, [sys.executable, *sys.argv])


def _validate_trace_arguments(args):
    """Fail fast on flags that would otherwise be silently ignored."""
    if (
        getattr(args, "command", None) == "answer"
        and not args.assignments
        and not args.defaults
    ):
        raise InputFailure("the following arguments are required: assignments")
    if getattr(args, "phase", None) and not getattr(args, "record", None):
        raise InputFailure(
            "--phase requires --record PATH; phases are only meaningful in a trace"
        )
    if (
        getattr(args, "command", None) == "trace"
        and getattr(args, "phases", None)
        and getattr(args, "order", "ordered") != "phased"
    ):
        raise InputFailure(
            "--phases is only used with --order phased; add --order phased "
            "or drop --phases"
        )


def _emit_worker_startup_error(args, error, kind):
    """Keep worker startup failures on the JSON protocol stream."""
    if getattr(args, "command", None) not in {"worker", "run", "journey"}:
        return False
    payload = {
        "id": None,
        "ok": False,
        "error": _clean(
            error if isinstance(error, Failure) else Failure(kind, str(error), {})
        ),
    }
    print(json.dumps(payload, default=str))
    return True


def main(argv=None):
    os.environ.update(target_interpreter_environment())
    _reexec_with_dyld_path()
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(arguments)
    except UsageFailure as error:
        _emit(
            _envelope(
                error.command,
                error=Failure(ErrorKind.INPUT, str(error), {}),
            ),
            _json_requested(arguments),
        )
        return 1
    try:
        _validate_trace_arguments(args)
        if args.command == "trace" and not args.root:
            # Trace comparison reads sidecars and never touches an Interview
            # package, so it does not require one to be present.
            root = Path.cwd().resolve()
        else:
            root = find_package_root(args.root)
        command_overrides = {}
        if getattr(args, "offline", False):
            command_overrides["offline"] = True
        if getattr(args, "background_actions", None):
            command_overrides["background_actions"] = args.background_actions
        if getattr(args, "seek_diagnostics", None):
            command_overrides["seek_diagnostics"] = args.seek_diagnostics
        config_root = root
        if getattr(args, "project_root", None):
            config_root = Path(args.project_root).expanduser().resolve()
        resolved = resolve_configuration(
            root,
            discovery_root=config_root,
            override_path=getattr(args, "config", None),
            additional_override_paths=tuple(getattr(args, "config_layer", None) or ()),
            base=load_config(config_root),
            command_overrides=command_overrides,
        )
        config = resolved.values
        args._resolved_configuration = resolved
        args._simulator_config = config
        settings = resolved.simulator
        mode = (
            getattr(args, "background_actions", None) or settings["background_actions"]
        )
        install_missing = settings["missing_runtime"] == "install"
        worker_command = args.command in {"worker", "run", "journey"}
        if worker_command:
            # Keep the real stdout handle for protocol frames while redirecting
            # bootstrap and interview-authored prints to stderr below.
            args._worker_protocol_stdout = sys.stdout
        with SimulatorRuntime().activate(
            root,
            config_path=resolved.effective_path,
            extra_config=config,
            background_action_mode=mode,
            seek_diagnostics=settings["seek_diagnostics"],
        ):
            needs_interview_bootstrap = args.command not in {
                "info",
                "status",
                "reset",
                "config",
                "trace",
            } or (args.command == "trace" and args.trace_command == "replay")
            with redirect_stdout(sys.stderr) if worker_command else nullcontext():
                if needs_interview_bootstrap:
                    if not config and not getattr(args, "offline", False):
                        # Preserve the small composition seam used by embedders that
                        # provide the legacy one-argument preflight callable.
                        ensure_importable(root)
                    else:
                        ensure_importable(
                            root,
                            install_missing=install_missing,
                            offline=getattr(args, "offline", False)
                            or settings["offline"],
                        )
                    bootstrap(
                        stub_define_defined=args.stub_defined,
                        background_action_mode=mode,
                        extra_config=config,
                    )
                return args.func(args, root)
    except (InputFailure, SystemExit, ValueError) as error:
        message = str(error)
        message = message.removeprefix("error: ")
        failure = Failure(ErrorKind.INPUT, message, {})
        if _emit_worker_startup_error(args, failure, ErrorKind.INPUT):
            return 1
        _emit(
            _envelope(
                args.command,
                error=failure,
            ),
            args.json,
        )
        return 1
    except (
        RuntimeError,
        TypeError,
        AttributeError,
        KeyError,
        IndexError,
        ImportError,
        OSError,
        LookupError,
        NameError,
        SyntaxError,
    ) as error:
        logger.exception("unhandled CLI fault")
        failure = Failure(ErrorKind.FAULT, f"{type(error).__name__}: {error}", {})
        if _emit_worker_startup_error(args, failure, ErrorKind.FAULT):
            return 3
        _emit(
            _envelope(
                args.command,
                error=failure,
            ),
            args.json,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
