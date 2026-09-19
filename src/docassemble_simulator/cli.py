"""Thin CLI adapters for catalog, execution, and rendering modules."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import textwrap
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from docassemble_simulator._outcomes import ErrorKind, Failure
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
    return InterviewExecution(root, args.interview, config_fingerprint=fingerprint)


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


def cmd_info(args, root):
    from docassemble_simulator.catalog import list_packages

    config = getattr(args, "_simulator_config", {})
    data = {
        "root": str(root),
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
        "session_persistence": "disabled (no server database substituted)",
    }
    data["runtime_compatibility"] = compatibility_report()
    _emit(_envelope("info", data), args.json)
    return 0


def cmd_config(args, root):
    config = getattr(args, "_simulator_config", {})
    report = _config_report(root, config, args)
    effective = dict(DOCASSEMBLE_DEFAULTS)
    deep_merge(effective, pass_through_config(config))
    report["effective"] = redact_config(effective)
    report["overrides"] = redact_config(config)
    report["source"] = "defaults plus discovered/--config overrides"
    _emit(_envelope("config", report), args.json)
    return 0


def cmd_catalog(args, root):
    from docassemble_simulator.catalog import CatalogFailure, InterviewCatalog

    catalog = InterviewCatalog(root, args.interview)
    try:
        if args.command == "check":
            outcome = catalog.check()
        elif args.command == "questions":
            outcome = catalog.questions(args.var)
        else:
            outcome = catalog.index(args.var)
    except AssemblyLineCompatibilityError as error:
        _emit(
            _envelope(
                args.command,
                error=Failure(
                    ErrorKind.RUNTIME_COMPATIBILITY, str(error), error.details
                ),
            ),
            args.json,
        )
        return _exit_for_error(ErrorKind.RUNTIME_COMPATIBILITY)
    except CatalogFailure as error:
        _emit(
            _envelope(
                args.command,
                error=Failure(ErrorKind.COMPILE, str(error), {}),
            ),
            args.json,
        )
        return 2
    result = outcome.result | (
        {"interview": outcome.interview} if outcome.interview else {}
    )
    ok = not (args.command == "check" and result["failures"])
    payload = (
        _envelope(args.command, result)
        if ok
        else _envelope(
            args.command,
            error={
                "kind": ErrorKind.COMPILE,
                "message": "one or more interviews failed to compile",
                "details": result,
            },
        )
    )
    _emit(payload, args.json)
    return 0 if ok else 2


def cmd_execution(args, root):
    from docassemble_simulator.execution import (
        Answer,
        Evaluate,
        Execute,
        Refresh,
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
        )
    elif args.command == "seek":
        operation = Seek(args.variable, args.fresh, args.activate, args.trace)
    elif args.command == "eval":
        operation = Evaluate(args.expression)
    elif args.command == "vars":
        operation = Variables(args.filter)
    else:
        code = Path(args.file).read_text(encoding="utf-8") if args.file else args.code
        operation = Execute(code, not args.no_assemble)
    execution = _execution(args, root)
    outcome = execution.run(operation)
    payload = (
        _envelope(
            args.command,
            outcome.result,
            diagnostics=outcome.diagnostics,
            attachments=outcome.attachments,
        )
        if outcome.ok
        else _envelope(
            args.command,
            error=outcome.error,
            diagnostics=outcome.diagnostics,
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


def _trace_metadata(args, root):
    from importlib import metadata as importlib_metadata

    from docassemble_simulator.catalog import InterviewCatalog
    from docassemble_simulator.compatibility import collect_environment
    from docassemble_simulator.trace import TraceMetadata

    resolved = getattr(args, "_resolved_configuration", None)
    environment = collect_environment()
    try:
        simulator = importlib_metadata.version("docassemble-simulator")
    except importlib_metadata.PackageNotFoundError:
        simulator = "unknown"
    return TraceMetadata(
        interview=InterviewCatalog(root, args.interview).identity,
        config_fingerprint=resolved.fingerprint if resolved is not None else "",
        docassemble=environment.versions.get("docassemble-base") or "not installed",
        assemblyline=environment.versions.get("docassemble-assemblyline")
        or "not installed",
        simulator=simulator,
    )


def _record_execution(args, root, execution, operation, payload):
    """Append the operation's screen outcome to the requested trace sidecar."""
    from docassemble_simulator.execution import Status
    from docassemble_simulator.trace import append_trace

    screen = None
    if payload.get("ok"):
        result = payload.get("result")
        if isinstance(result, dict) and "kind" in result:
            screen = result
    else:
        # A failed operation leaves the active screen untouched; the sidecar
        # still records which screen the user was looking at.
        try:
            status = execution.run(Status())
        except Exception:  # noqa: BLE001 - a missing session is not a record failure
            status = None
        if (
            status is not None
            and status.ok
            and isinstance(status.result, dict)
            and "kind" in status.result
        ):
            screen = status.result
    if screen is None and payload.get("ok"):
        return
    append_trace(
        args.record,
        _trace_metadata(args, root),
        operation=args.command,
        ok=bool(payload.get("ok")),
        screen=screen,
        error=payload.get("error"),
        phase=getattr(args, "phase", None),
        submitted=dict(getattr(operation, "assignments", ()) or ()),
    )


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


def cmd_trace(args, root):
    from docassemble_simulator.trace import (
        TraceError,
        compare_traces,
        load_exceptions,
        load_trace,
        write_trace,
    )

    try:
        expected = load_trace(args.expected)
        actual = load_trace(args.actual)
        exceptions = load_exceptions(args.exceptions) if args.exceptions else ()
        comparison = compare_traces(expected, actual, _trace_policy(args), exceptions)
    except TraceError as error:
        _emit(
            _envelope("trace", error=Failure(ErrorKind.INPUT, str(error), {})),
            args.json,
        )
        return 1
    report = comparison.as_dict()
    if args.update:
        try:
            write_trace(args.expected, actual)
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


def cmd_render(args, root):
    from docassemble_simulator.render import (
        FixtureSource,
        FreshSource,
        InterviewRenderer,
        RenderRequest,
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
    request = RenderRequest(
        args.template,
        source,
        False if isinstance(source, FixtureSource) else not args.no_assemble,
        Path(args.save_snapshot).expanduser().resolve() if args.save_snapshot else None,
        Path(args.output).expanduser().resolve() if args.output else None,
        args.expect_missing,
        _configured_bindings(
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
        help="capture the variable-seeking trace (default: capture; off suppresses the trace)",
    )
    common = argparse.ArgumentParser(add_help=False)
    for flag in ("root", "interview"):
        common.add_argument(f"--{flag}", default=argparse.SUPPRESS)
    common.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        help="override config file (YAML or TOML); sessions are scoped by the effective configuration",
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
    add("check", help="compile interview definitions").set_defaults(func=cmd_catalog)
    for name in ("questions", "index"):
        item = add(name, help=f"inspect interview {name}")
        item.add_argument("--var")
        item.set_defaults(func=cmd_catalog)

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
    answer = add_recording(
        add(
            "answer",
            help="transactionally answer the active screen",
            epilog=recording_epilog,
        )
    )
    answer.add_argument("assignments", nargs="+")
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
            "declared phase order for --order phased, e.g. intake,documents,download"
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
            "ephemeral template bindings."
        ),
    )
    render.add_argument("template")
    render_source = render.add_mutually_exclusive_group()
    render_source.add_argument("--fresh", action="store_true")
    render_source.add_argument("--snapshot", dest="snapshot_source")
    render_source.add_argument("--fixture")
    render.add_argument("--no-assemble", action="store_true")
    render.add_argument("--save-snapshot")
    render.add_argument("--output", metavar="PATH")
    render.add_argument("--expect-missing")
    render.add_argument(
        "--bind",
        action="append",
        default=[],
        metavar="NAME=EXPRESSION",
        help="bind a template variable in the ephemeral render namespace (repeatable)",
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
    """Fail fast on trace flags that would otherwise be silently ignored."""
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


def main(argv=None):
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
        resolved = resolve_configuration(
            root,
            override_path=getattr(args, "config", None),
            base=load_config(root),
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
        with SimulatorRuntime().activate(
            root,
            config_path=resolved.effective_path,
            extra_config=config,
            background_action_mode=mode,
            seek_diagnostics=settings["seek_diagnostics"],
        ):
            if args.command not in {"info", "status", "config", "trace"}:
                if not config and not getattr(args, "offline", False):
                    # Preserve the small composition seam used by embedders that
                    # provide the legacy one-argument preflight callable.
                    ensure_importable(root)
                else:
                    ensure_importable(
                        root,
                        install_missing=install_missing,
                        offline=getattr(args, "offline", False) or settings["offline"],
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
        _emit(
            _envelope(
                args.command,
                error=Failure(ErrorKind.INPUT, message, {}),
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
        _emit(
            _envelope(
                args.command,
                error={
                    "kind": ErrorKind.FAULT,
                    "message": f"{type(error).__name__}: {error}",
                    "details": {},
                },
            ),
            args.json,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
