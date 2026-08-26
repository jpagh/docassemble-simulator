"""Thin CLI adapters for catalog, execution, and rendering modules."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Literal, cast

from docassemble_simulator.bootstrap import (
    bootstrap,
    deep_merge,
    dyld_fallback_value,
    prepare_environment,
)
from docassemble_simulator.config import load_config
from docassemble_simulator.detect import (
    ensure_importable,
    find_package_root,
    list_interviews,
)


def _clean(value):
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


def _envelope(command: str, result: Any = None, error: Any = None):
    if error is None:
        return {"ok": True, "command": command, "result": result}
    if hasattr(error, "kind"):
        error = {
            "kind": error.kind,
            "message": error.message,
            "details": error.details or {},
        }
    return {"ok": False, "command": command, "error": error}


def _emit(payload, as_json):
    if as_json:
        print(json.dumps(_clean(payload), indent=2, default=str))
        return
    if payload["ok"]:
        print(_human(payload.get("result")))
    else:
        error = payload["error"]
        print(f"error: {error['message']}", file=sys.stderr)
        for key, value in (error.get("details") or {}).items():
            print(f"  {key}: {_human(value)}", file=sys.stderr)


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


def _exit_for_error(kind: str):
    if kind in {"input", "state", "workspace", "configuration"}:
        return 1
    if kind == "fault":
        return 3
    return 2


def _execution(args, root):
    from docassemble_simulator.execution import InterviewExecution

    return InterviewExecution(root, args.interview)


def cmd_info(args, root):
    from docassemble_simulator.detect import list_packages

    data = {
        "root": str(root),
        "packages": list_packages(root),
        "interview_count": len(list_interviews(root)),
        "interviews": list_interviews(root),
    }
    try:
        import docassemble.base

        data["docassemble_version"] = getattr(
            docassemble.base, "__version__", "unknown"
        )
    except Exception:
        data["docassemble_version"] = "unknown (docassemble not importable)"
    _emit(_envelope("info", data), args.json)
    return 0


def cmd_catalog(args, root):
    from docassemble_simulator.catalog import InterviewCatalog

    catalog = InterviewCatalog(root, args.interview)
    if args.command == "check":
        outcome = catalog.check()
    elif args.command == "questions":
        outcome = catalog.questions(args.var)
    else:
        outcome = catalog.index(args.var)
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
                "kind": "compile",
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
        _envelope(args.command, outcome.result)
        if outcome.ok
        else _envelope(args.command, error=outcome.error)
    )
    _emit(payload, args.json)
    if not outcome.ok:
        return _exit_for_error(outcome.error.kind)
    return (
        2
        if isinstance(outcome.result, dict) and outcome.result.get("kind") == "error"
        else 0
    )


def cmd_render(args, root):
    from docassemble_simulator.execution import RenderSource
    from docassemble_simulator.render import InterviewRenderer, RenderRequest

    selected = [
        ("fresh", args.fresh),
        ("snapshot", args.snapshot_source),
        ("fixture", args.fixture),
    ]
    choices = [(kind, value) for kind, value in selected if value]
    result: dict[str, Any]
    if len(choices) > 1:
        result = {
            "ok": False,
            "error": {
                "kind": "input",
                "message": "select exactly one of --fresh, --snapshot, or --fixture",
                "details": {},
            },
        }
    else:
        kind: Literal["saved", "fresh", "snapshot", "fixture"]
        value: Any
        raw_kind, value = choices[0] if choices else ("saved", None)
        kind = cast(Literal["saved", "fresh", "snapshot", "fixture"], raw_kind)
        path = (
            Path(str(value)).expanduser().resolve()
            if kind in {"snapshot", "fixture"}
            else None
        )
        source = RenderSource(kind, path)
        assemble = False if kind == "fixture" else not args.no_assemble
        request = RenderRequest(
            args.template,
            source,
            assemble,
            Path(args.save_snapshot).expanduser().resolve()
            if args.save_snapshot
            else None,
            Path(args.output).expanduser().resolve() if args.output else None,
            args.expect_missing,
        )
        result = InterviewRenderer(root, _execution(args, root)).render(request)
    payload = (
        _envelope("render", result.get("result"))
        if result["ok"]
        else _envelope("render", error=result["error"])
    )
    _emit(payload, args.json)
    return 0 if result["ok"] else _exit_for_error(str(result["error"]["kind"]))


def _parse_assignments(pairs):
    result = []
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"error: expected VAR=VALUE, got {pair!r}")
        variable, value = pair.split("=", 1)
        result.append((variable.strip(), value))
    return result


def build_parser():
    parser = argparse.ArgumentParser(
        prog="docassemble-simulator",
        description="Run and render docassemble interviews locally. JSON uses a stable ok/command/result-or-error envelope.",
    )
    parser.add_argument("--root", default=None)
    parser.add_argument("--interview", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--stub-defined", action="store_true")
    parser.add_argument("--config", default=None)
    common = argparse.ArgumentParser(add_help=False)
    for flag in ("root", "interview", "config"):
        common.add_argument(f"--{flag}", default=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    common.add_argument(
        "--stub-defined", action="store_true", default=argparse.SUPPRESS
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, **kwargs):
        return sub.add_parser(name, parents=[common], **kwargs)

    add("info", help="inspect the workspace without runtime bootstrap").set_defaults(
        func=cmd_info
    )
    add("check", help="compile interview definitions").set_defaults(func=cmd_catalog)
    for name in ("questions", "index"):
        item = add(name, help=f"inspect interview {name}")
        item.add_argument("--var")
        item.set_defaults(func=cmd_catalog)
    add("start", help="create and assemble a fresh session").set_defaults(
        func=cmd_execution
    )
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
    render = add("render", help="render a DOCX from one explicit state source")
    render.add_argument("template")
    render.add_argument("--fresh", action="store_true")
    render.add_argument("--snapshot", dest="snapshot_source")
    render.add_argument("--fixture")
    render.add_argument("--no-assemble", action="store_true")
    render.add_argument("--save-snapshot")
    render.add_argument("--output", metavar="PATH")
    render.add_argument("--expect-missing")
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
    args = build_parser().parse_args(argv)
    root = find_package_root(args.root)
    try:
        config = load_config(root)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if getattr(args, "config", None):
        import yaml

        source = Path(args.config).expanduser()
        if not source.exists():
            print(f"error: config {source} does not exist", file=sys.stderr)
            return 1
        loaded = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            print(f"error: config {source} must be a YAML mapping", file=sys.stderr)
            return 1
        deep_merge(config, loaded)
    prepare_environment(
        config_path=root / ".simulator" / "config-effective.yml", extra_config=config
    )
    if args.command not in {"info", "status"}:
        ensure_importable(root)
        bootstrap(stub_define_defined=args.stub_defined)
    try:
        return args.func(args, root)
    except SystemExit:
        raise
    except Exception as error:
        _emit(
            _envelope(
                args.command,
                error={
                    "kind": "fault",
                    "message": f"{type(error).__name__}: {error}",
                    "details": {},
                },
            ),
            args.json,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
