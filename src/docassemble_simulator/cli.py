"""Thin CLI adapters for catalog, execution, and rendering modules."""

from __future__ import annotations

import argparse
import json
import logging
import sys
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


class UsageParser(argparse.ArgumentParser):
    def __init__(self, *args, command_name: str = "cli", **kwargs):
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
    outcome = _execution(args, root).run(operation)
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
    _emit(payload, args.json)
    if not outcome.ok:
        return _exit_for_error(outcome.error.kind)
    return 0


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
    add(
        "start",
        help="create and assemble a fresh session for the effective configuration",
    ).set_defaults(func=cmd_execution)
    add("status", help="read the saved outcome without assembly").set_defaults(
        func=cmd_execution
    )
    add("refresh", help="rehydrate and reassemble saved state").set_defaults(
        func=cmd_execution
    )
    answer = add("answer", help="transactionally answer the active screen")
    answer.add_argument("assignments", nargs="+")
    answer.add_argument("--code", action="store_true")
    answer.add_argument("--no-validate", action="store_true")
    answer.add_argument("--strict", action="store_true")
    answer.set_defaults(func=cmd_execution)
    seek = add("seek", help="seek a variable from saved state by default")
    seek.add_argument("variable")
    seek.add_argument("--fresh", action="store_true")
    seek.add_argument("--activate", action="store_true")
    seek.add_argument("--trace", action="store_true")
    seek.set_defaults(func=cmd_execution)
    evaluate = add("eval", help="evaluate an expression without saving")
    evaluate.add_argument("expression")
    evaluate.set_defaults(func=cmd_execution)
    execute = add("exec", help="execute Python and assemble by default")
    execute.add_argument("code", nargs="?", default="")
    execute.add_argument("--file")
    execute.add_argument("--no-assemble", action="store_true")
    execute.set_defaults(func=cmd_execution)
    variables = add("vars", help="inspect saved variables without saving")
    variables.add_argument("--filter")
    variables.set_defaults(func=cmd_execution)
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
            if args.command not in {"info", "status", "config"}:
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
