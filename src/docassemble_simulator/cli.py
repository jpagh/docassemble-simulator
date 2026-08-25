"""docassemble-simulator CLI.

Agent-friendly: every command prints either human-readable text or, with
--json, a machine-readable document. Sessions persist under
<package-root>/.simulator/session.pkl so an agent can drive the interview
across many small invocations.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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
    resolve_interview,
)


def _emit(data, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_clean(data), indent=2, default=str))
    else:
        print(_render(data))


def _clean(obj):
    """Drop None values recursively for tidy JSON."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def _render(data, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(data, dict):
        lines = []
        for key, val in data.items():
            if isinstance(val, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.append(_render(val, indent + 1))
            else:
                lines.append(f"{pad}{key}: {val}")
        return "\n".join(lines)
    if isinstance(data, list):
        return "\n".join(_render(item, indent) for item in data)
    return f"{pad}{data}"


def _screen_lines(screen: dict) -> list[str]:
    """Human rendering of one screen description."""
    kind = screen.get("kind", "question")
    out: list[str] = []
    qtype = screen.get("question_type")
    header = kind if not qtype else f"{kind} ({qtype})"
    out.append(f"== Screen: {header}")
    if screen.get("question_name"):
        out.append(f"   name: {screen['question_name']}")
    if screen.get("sought") and screen.get("orig_sought"):
        if screen["sought"] != screen["orig_sought"]:
            out.append(f"   sought: {screen['orig_sought']} (via {screen['sought']})")
        else:
            out.append(f"   sought: {screen['sought']}")
    if screen.get("message"):
        out.append(f"   {screen['message']}")
    if screen.get("error_type"):
        out.append(f"   error: {screen['error_type']}: {screen.get('message', '')}")
    if screen.get("traceback_tail"):
        out.append("   --- traceback tail ---")
        for line in screen["traceback_tail"].splitlines():
            out.append(f"   {line}")
        out.append("   ----------------------")
    text = screen.get("question_text")
    if text:
        out.append("")
        out.append(_strip_html(str(text)))
    sub = screen.get("subquestion_text")
    if sub:
        out.append("")
        out.append(_strip_html(str(sub)))
    fields = screen.get("fields") or []
    if fields:
        out.append("")
        out.append("   fields:")
        for field in fields:
            name = field.get("variable", "?")
            ftype = field.get("type", "?")
            label = field.get("label")
            tags = ""
            if field.get("visible") is False:
                tags += "  [hidden]"
            elif field.get("visible") is None and field.get("visibility_note"):
                tags += f"  [{field['visibility_note']}]"
            if not field.get("required", True):
                tags += "  [optional]"
            out.append(f"     - {name}  ({ftype}){tags}")
            if label:
                out.append(f"       label: {label}")
            choices = field.get("choices")
            if choices:
                rendered = ", ".join(
                    (
                        f"reference to {c['reference_to']}"
                        if c.get("reference_to")
                        else f"{c.get('value')!r}"
                        + (f" ({c.get('label')})" if c.get("label") else "")
                    )
                    for c in choices
                )
                out.append(f"       choices: {rendered}")
            values = field.get("values")
            if values:
                out.append(f"       values: {', '.join(values)}")
            if field.get("default") is not None:
                out.append(f"       default: {field['default']}")
    for warning in screen.get("warnings") or []:
        out.append(f"\n   warning: {warning}")
    if screen.get("continue_label"):
        out.append(f"\n   [continue: {screen['continue_label']}]")
    seeking = screen.get("seeking")
    if seeking:
        out.append("")
        out.append("   seek chain:")
        for stage in seeking:
            var = stage.get("variable", "")
            qname = stage.get("question", "")
            reason = stage.get("reason", "")
            out.append(f"     - {var}{(' <- ' + qname) if qname else ''}  {reason}".rstrip())
    out.append("")
    out.append(
        "answer with: docassemble-simulator set VAR=VALUE ...   "
        "(then the next screen is shown)"
    )
    return out


def _strip_html(text: str) -> str:
    import re

    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    import html

    return html.unescape(text).strip()


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _preflight_warnings(root: Path) -> list[str]:
    """Return advisory setup notes without making `info`/`check` fail."""
    import re
    import zipfile

    warnings: list[str] = []
    attorney_fields = re.compile(r"\.(?:bar_id|email)\b")
    found_attorney_fields = False
    for path in root.glob("docassemble/*/data/templates/**/*"):
        if not path.is_file() or path.suffix.lower() not in {".docx", ".yml", ".yaml", ".txt"}:
            continue
        try:
            if path.suffix.lower() == ".docx":
                with zipfile.ZipFile(path) as archive:
                    contents = b"\n".join(
                        archive.read(name)
                        for name in archive.namelist()
                        if name.endswith(".xml")
                    )
                text = contents.decode("utf-8", errors="ignore")
            else:
                text = path.read_text(encoding="utf-8")
        except (OSError, zipfile.BadZipFile):
            continue
        if attorney_fields.search(text):
            found_attorney_fields = True
            break

    if found_attorney_fields:
        warnings.append(
            "templates reference attorney fields such as .bar_id/.email; "
            "pre-seed those values and stable reference objects in "
            ".config/simulator/config.py (see README 'Pre-seed server-scoped globals')"
        )
    return warnings


def cmd_info(args, root: Path) -> int:
    from docassemble_simulator.detect import list_packages

    sys.path.insert(0, str(root))
    interviews = list_interviews(root)
    data = {
        "root": str(root),
        "packages": list_packages(root),
        "interview_count": len(interviews),
        "interviews": interviews,
        "session_file": str(root / ".simulator" / "session.pkl"),
        "session_exists": (root / ".simulator" / "session.pkl").exists(),
        "warnings": _preflight_warnings(root) or None,
    }
    try:
        import docassemble.base

        data["docassemble_version"] = getattr(docassemble.base, "__version__", "unknown")
    except Exception:
        data["docassemble_version"] = "unknown (docassemble not importable)"
    _emit(data, args.json)
    return 0


def cmd_questions(args, root: Path) -> int:
    ensure_importable(root)
    bootstrap()
    interview_path, _ = resolve_interview(root, args.interview)
    from docassemble_simulator.session import Session

    session = Session(interview_path, root)
    interview = session.load_interview()

    def _dec(s):
        from docassemble_simulator.describe import from_safeid_safe

        try:
            return from_safeid_safe(s)
        except Exception:
            return s

    rows = []
    for idx, q in enumerate(interview.questions_list):
        flds = getattr(q, "fields", None) or []
        first_var = ""
        all_vars = [_dec(getattr(f, "saveas", "") or "") for f in flds]
        if all_vars:
            first_var = all_vars[0]
        if args.var:
            needle = args.var.lower()
            hay = " ".join(all_vars + [getattr(q, "name", "") or ""]).lower()
            if needle not in hay:
                continue
        cond = getattr(q, "condition", "") or ""
        rows.append(
            {
                "index": idx,
                "block_type": getattr(q, "question_type", "?"),
                "mandatory": bool(getattr(q, "is_mandatory", False)),
                "name": getattr(q, "name", None),
                "first_variable": first_var,
                "variables": all_vars or None,
                "condition": cond[:120] or None,
            }
        )
    _emit({"interview": interview_path, "blocks": rows}, args.json)
    return 0


def cmd_index(args, root: Path) -> int:
    ensure_importable(root)
    bootstrap()
    interview_path, _ = resolve_interview(root, args.interview)
    from docassemble_simulator.describe import from_safeid_safe
    from docassemble_simulator.session import Session

    session = Session(interview_path, root)
    interview = session.load_interview()
    mapping = {}
    for var, entry in interview.questions.items():
        if args.var and args.var.lower() not in var.lower():
            continue
        screens = []
        for lang in ("en", "*"):
            lang_entry = entry.get(lang)
            if not lang_entry:
                continue
            for q in lang_entry:
                screens.append({"language": lang, "question_name": getattr(q, "name", None)})
        mapping[var] = screens or None
    if not mapping:
        print(
            "no question index entries"
            + (f" matching '{args.var}'" if args.var else "")
            + "; such variables are defined only by code/objects blocks or are missing",
            file=sys.stderr,
        )
        return 1
    _emit({"interview": interview_path, "index": mapping}, args.json)
    return 0


def cmd_start(args, root: Path) -> int:
    ensure_importable(root)
    bootstrap(stub_define_defined=args.stub_defined)
    interview_path, _ = resolve_interview(root, args.interview)
    from docassemble_simulator.session import Session

    session = Session(interview_path, root)
    session.reset()
    user_dict = session.fresh_user_dict()
    screen, interview = session.current_screen(user_dict)

    if args.set:
        errors = session.apply_assignments(
            interview,
            user_dict,
            _parse_assignments(args.set),
            use_code=args.code,
            screen=screen,
        )
        if errors:
            _emit({"kind": "assignment_errors", "errors": errors}, args.json)
            return 1
        screen, interview = session.current_screen(user_dict)

    session.save(user_dict, screen)
    data = {"interview": interview_path, "session": str(session.state_file)}
    data.update(screen)
    _emit(data, args.json)
    return _screen_exit_code(screen)


def cmd_status(args, root: Path) -> int:
    ensure_importable(root)
    bootstrap(stub_define_defined=args.stub_defined)
    interview_path, _ = resolve_interview(root, args.interview)
    from docassemble_simulator.session import Session, variable_names

    session = Session(interview_path, root)
    user_dict, saved_screen, origin, sought_variable = session.load_state_full()

    if origin == "seek" and sought_variable:
        # Pinned by a previous `seek --save`: re-seek the same variable rather
        # than re-running the mandatory chain (which may stop elsewhere).
        screen, _interview = session.seek(user_dict, sought_variable)
        screen["note"] = f"pinned to seek of '{sought_variable}'"
    else:
        screen, _interview = session.current_screen(user_dict)
        session.save(user_dict, screen)

    data = {"interview": interview_path}
    data.update(screen)
    if args.vars:
        data["defined_variables"] = variable_names(user_dict)
    if args.json:
        _emit(data, True)
    else:
        _render_screen_or_error(screen, saved_screen)
    return _screen_exit_code(screen)


def cmd_seek(args, root: Path) -> int:
    ensure_importable(root)
    bootstrap(stub_define_defined=args.stub_defined)
    interview_path, _ = resolve_interview(root, args.interview)
    from docassemble_simulator.session import Session, _user_facing_error_message

    session = Session(interview_path, root)
    if args.continue_session:
        user_dict, _saved = session.load_state()
    else:
        user_dict = session.fresh_user_dict()
    try:
        screen, interview = session.seek(user_dict, args.variable, trace=args.trace)
    except Exception as err:
        data = {
            "kind": "error",
            "error_type": type(err).__name__,
            "message": _user_facing_error_message(err)[:500],
        }
        _emit(data, args.json)
        return 2
    if args.save:
        # Save the seek result as the session's pinned screen so a following
        # `set` validates against and answers THIS screen.
        session.save(user_dict, screen, origin="seek", sought_variable=args.variable)
    data = {"interview": interview_path}
    data.update(screen)
    _emit(data, args.json)
    return _screen_exit_code(screen)


def cmd_set(args, root: Path) -> int:
    ensure_importable(root)
    bootstrap(stub_define_defined=args.stub_defined)
    interview_path, _ = resolve_interview(root, args.interview)
    from docassemble_simulator.session import Session

    session = Session(interview_path, root)
    user_dict, saved_screen, origin, sought_variable = session.load_state_full()
    assignments = _parse_assignments(args.assignments)
    if not assignments:
        print("error: no VAR=VALUE pairs given", file=sys.stderr)
        return 1

    # Apply without advancing: the values belong to the *current* screen.
    interview_probe = session.load_interview()
    # Authored seed code first so stubbed dependencies are in place for validation.
    session.run_config(user_dict, interview_probe)
    errors = session.apply_assignments(
        interview_probe,
        user_dict,
        assignments,
        use_code=args.code,
        screen=saved_screen,
    )
    if errors:
        _emit({"kind": "assignment_errors", "errors": errors}, args.json)
        return 1

    # Replay the server's submit-time checks against the screen being answered.
    validation: dict = {"errors": [], "warnings": []}
    if not args.no_validate:
        validation = session.validate_screen(interview_probe, user_dict, saved_screen)
        if args.strict:
            validation["errors"] += validation["warnings"]
            validation["warnings"] = []
    if validation["errors"]:
        # Server-faithful: on failed validation the submitted values are
        # discarded and the same screen is re-presented.
        data = {
            "kind": "invalid",
            "message": "screen rejected by submit-time validation; answers discarded",
            "applied": [f"{v}={r}" for v, r in assignments],
            "validation_errors": validation["errors"],
            "warnings": validation["warnings"] or None,
            "re_run_with": "docassemble-simulator set ... --no-validate to bypass",
        }
        _emit(data, args.json)
        if not args.json:
            print("== VALIDATION FAILED (answers discarded)")
            for e in validation["errors"]:
                print(f"   error: {e}")
            for w in validation["warnings"]:
                print(f"   warning: {w}")
        return 2

    question_name = None
    if saved_screen and not args.no_mark_answered:
        question_name = saved_screen.get("question_name")
    session.mark_answered(interview_probe, user_dict, question_name)

    # Advance the flow like a form submission would (this also releases a
    # seek pin: the mandatory chain decides what comes next).
    screen, interview = session.current_screen(user_dict)
    if origin == "seek":
        screen["note"] = f"answered pinned screen '{sought_variable}'; back to flow"
    session.save(user_dict, screen)
    data = {
        "answered_question": question_name,
        "applied": [f"{v}={r}" for v, r in assignments],
        # Warnings refer to the screen just submitted, not the new one.
        "warnings": validation["warnings"] or None,
    }
    data.update(screen)
    if args.json:
        _emit(data, True)
    else:
        for w in validation["warnings"]:
            print(f"warning: {w}")
        _render_screen_or_error(screen, None)
    return _screen_exit_code(screen)


def cmd_get(args, root: Path) -> int:
    ensure_importable(root)
    bootstrap(stub_define_defined=args.stub_defined)
    interview_path, _ = resolve_interview(root, args.interview)
    from docassemble_simulator.session import Session

    session = Session(interview_path, root)
    user_dict, _saved = session.load_state()
    interview = session.load_interview()
    session.run_config(user_dict, interview)
    try:
        value = session.eval_in_session(interview, user_dict, args.expression)
    except Exception as err:
        _emit(
            {"kind": "error", "error_type": type(err).__name__, "message": str(err)[:300]},
            args.json,
        )
        return 2
    if args.json:
        _emit({"expression": args.expression, "value": _safe_repr(value)}, True)
    else:
        print(repr(value))
    return 0


def cmd_exec(args, root: Path) -> int:
    ensure_importable(root)
    bootstrap(stub_define_defined=args.stub_defined)
    interview_path, _ = resolve_interview(root, args.interview)
    from docassemble_simulator.session import Session

    session = Session(interview_path, root)
    user_dict, saved_screen = session.load_state()
    interview = session.load_interview()
    session.run_config(user_dict, interview)
    code = args.code
    if args.file:
        code = Path(args.file).read_text(encoding="utf-8")
    try:
        session.exec_in_session(interview, user_dict, code)
    except Exception as err:
        _emit(
            {"kind": "error", "error_type": type(err).__name__, "message": str(err)[:500]},
            args.json,
        )
        return 2
    screen = None
    if args.show:
        screen, _interview = session.current_screen(user_dict)
        session.save(user_dict, screen)
        data = {"interview": interview_path}
        data.update(screen)
    else:
        session.save(user_dict, None)
        data = {"executed": True, "session": str(session.state_file)}
    _emit(data, args.json)
    return 0


def cmd_vars(args, root: Path) -> int:
    ensure_importable(root)
    bootstrap(stub_define_defined=args.stub_defined)
    interview_path, _ = resolve_interview(root, args.interview)
    from docassemble_simulator.session import Session, variable_names

    session = Session(interview_path, root)
    user_dict, _saved = session.load_state()
    interview = session.load_interview()
    session.run_config(user_dict, interview)
    names = variable_names(user_dict, limit=100000)
    data = {}
    for name in names:
        if args.filter and args.filter.lower() not in name.lower():
            continue
        try:
            value = session.eval_in_session(interview, user_dict, name)
            data[name] = _safe_repr(value)
        except Exception as err:
            data[name] = f"<{type(err).__name__}: {str(err)[:80]}>"
    _emit(data, args.json)
    return 0


def cmd_render(args, root: Path) -> int:
    """Render one template against live interview state or a fixture namespace."""
    ensure_importable(root)
    bootstrap(stub_define_defined=args.stub_defined)
    from docassemble_simulator.render import (
        RenderError,
        RenderExpectationError,
        TemplateNotFoundError,
        find_template,
        missing_error_matches,
        paragraph_count,
        prepare_docx_template,
        render_template,
        write_artifact,
    )
    from docassemble_simulator.session import Session, SessionError

    if args.fresh and (args.no_flow or args.from_snapshot):
        raise SessionError("--fresh cannot be combined with --no-flow or --from-snapshot")
    if args.no_flow and args.from_snapshot:
        raise SessionError("--no-flow and --from-snapshot cannot be used together")
    if args.fixture and args.from_snapshot:
        raise SessionError("--fixture and --from-snapshot cannot be used together")

    try:
        template_path = find_template(root, args.template)
    except TemplateNotFoundError as err:
        raise SessionError(str(err)) from err

    fixture_path = Path(args.fixture) if args.fixture else root / ".config" / "simulator" / "fixture.py"
    if not fixture_path.is_absolute() and args.fixture:
        fixture_path = Path.cwd() / fixture_path
    if args.fixture and not fixture_path.exists():
        raise SessionError(f"fixture script not found: {fixture_path}")
    if not args.fixture and not fixture_path.exists():
        fixture_path = None

    session = Session(resolve_interview(root, args.interview)[0], root)
    interview = session.load_interview()
    if args.from_snapshot:
        user_dict = session.load_snapshot(args.from_snapshot)
    elif fixture_path is not None:
        user_dict = session.fresh_user_dict()
    elif args.no_flow:
        user_dict, _saved_screen = session.load_state()
    elif args.fresh:
        session.reset()
        user_dict = session.fresh_user_dict()
    elif session.state_file.exists():
        user_dict, _saved_screen = session.load_state()
    else:
        user_dict = session.fresh_user_dict()

    result = {"template": args.template, "ok": False}
    try:
        with session._in_interview(interview, user_dict) as status:
            if fixture_path is not None:
                if hasattr(session, "run_config"):
                    session.run_config(user_dict, interview)
                session.exec_in_namespace(
                    fixture_path.read_text(encoding="utf-8"), user_dict, interview
                )
            elif not args.no_flow:
                interview.load_util(user_dict)
                session.run_config(user_dict, interview)
                try:
                    interview.assemble(user_dict, interview_status=status)
                except Exception as err:
                    from docassemble.base.error import DAErrorNoEndpoint

                    if not isinstance(err, DAErrorNoEndpoint):
                        raise RenderError.from_exception(err) from err
            else:
                # --no-flow skips assembly, not the authored seed.  Seeded
                # roots and template built-ins are still needed to resolve
                # references while rendering saved state.
                session.run_config(user_dict, interview)

            if args.snapshot:
                try:
                    session.save_snapshot(args.snapshot, user_dict)
                except OSError as err:
                    raise SessionError(f"could not write snapshot: {err}") from err

            docx_template = prepare_docx_template(template_path)
            try:
                docx_template = render_template(docx_template, user_dict)
            except RenderError as err:
                if args.expect_missing and missing_error_matches(err, args.expect_missing):
                    result = {
                        "template": args.template,
                        "ok": True,
                        "paragraphs": paragraph_count(docx_template),
                    }
                else:
                    raise
            else:
                if args.expect_missing:
                    raise RenderExpectationError(
                        f"expected missing variable {args.expect_missing!r}, but render succeeded"
                    )
                result = {
                    "template": args.template,
                    "ok": True,
                    "paragraphs": paragraph_count(docx_template),
                }
                if args.output:
                    try:
                        artifact = write_artifact(
                            docx_template, args.output, template_path.name
                        )
                    except OSError as err:
                        raise SessionError(
                            f"could not write rendered artifact: {err}"
                        ) from err
                    result["artifact"] = str(artifact)
    except RenderError as err:
        result = {
            "template": args.template,
            "ok": False,
            "error_type": err.error_type,
            "message": str(err),
            "paragraph": err.paragraph,
        }
        _emit(result, args.json)
        return 2
    except RenderExpectationError as err:
        result = {
            "template": args.template,
            "ok": False,
            "error_type": type(err).__name__,
            "message": str(err),
            "paragraph": None,
        }
        _emit(result, args.json)
        return 2
    except SessionError:
        raise
    except Exception as err:
        render_error = RenderError.from_exception(err)
        result = {
            "template": args.template,
            "ok": False,
            "error_type": render_error.error_type,
            "message": str(render_error),
            "paragraph": render_error.paragraph,
        }
        _emit(result, args.json)
        return 2

    _emit(result, args.json)
    return 0


def cmd_check(args, root: Path) -> int:
    ensure_importable(root)
    bootstrap(stub_define_defined=args.stub_defined)
    results = []
    interviews = (
        list_interviews(root)
        if not args.interview
        else [resolve_interview(root, args.interview)[0]]
    )
    from docassemble_simulator.session import Session

    failures = 0
    for path in interviews:
        session = Session(path, root)
        try:
            interview = session.load_interview()
            results.append(
                {
                    "interview": path,
                    "ok": True,
                    "blocks": len(interview.questions_list),
                    "questions": sum(
                        1
                        for q in interview.questions_list
                        if getattr(q, "question_type", "") == "question"
                        or getattr(q, "question_type", "") == "fields"
                    ),
                }
            )
        except SystemExit:
            raise
        except Exception as err:
            failures += 1
            results.append(
                {
                    "interview": path,
                    "ok": False,
                    "error_type": type(err).__name__,
                    "message": str(err)[:300],
                }
            )
    _emit(
        {
            "checked": len(results),
            "failures": failures,
            "results": results,
            "warnings": _preflight_warnings(root) or None,
        },
        args.json,
    )
    return 1 if failures else 0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _parse_assignments(pairs: list[str]) -> list[tuple[str, str]]:
    out = []
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(
                f"error: expected VAR=VALUE, got '{pair}' (quote values containing '=')"
            )
        var, value = pair.split("=", 1)
        out.append((var.strip(), value))
    return out


def _safe_repr(value) -> str:
    try:
        r = repr(value)
    except Exception as err:
        return f"<unrepr-able: {err}>"
    return r if len(r) <= 2000 else r[:2000] + "..."


def _screen_exit_code(screen: dict) -> int:
    """Error screens are exit-distinguishable (2) in every flow command."""
    return 0 if screen.get("kind") != "error" else 2


def _render_screen_or_error(screen: dict, saved_screen: dict | None) -> None:
    if screen.get("kind") == "error":
        print(f"== ERROR: {screen.get('error_type')}: {screen.get('message', '')}")
        if screen.get("traceback_tail"):
            print(screen["traceback_tail"])
        return
    for line in _screen_lines(screen):
        print(line)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docassemble-simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Run a docassemble interview locally, without a server or browser.\n"
            "\n"
            "The interview is loaded through the real docassemble compiler and\n"
            "driven through the real seek/assemble machinery, so variable-resolution\n"
            "failures, broken conditions, and bad screens reproduce exactly.\n"
            "\n"
            "State is a single session file (.simulator/session.pkl inside the\n"
            "package directory): 'start' creates it, 'set' advances it, and\n"
            "'status', 'get', and 'vars' inspect it. Every command accepts --json\n"
            "for machine-readable output. Commands that need a session say so;\n"
            "run 'start' once before using them.\n"
            "\n"
            "Run inside a docassemble package directory (one containing\n"
            "docassemble/<pkg>/); use --root to point elsewhere."
        ),
        epilog=(
            "typical AI-agent loop:\n"
            "\n"
            "  docassemble-simulator info     # what package/interviews were detected?\n"
            "  docassemble-simulator check    # compile every interview; report errors\n"
            "  docassemble-simulator start    # fresh session -> prints the first screen\n"
            "\n"
            "Move through the interview by answering whatever screen 'status' last\n"
            "showed, using the field names it displays:\n"
            "\n"
            "  docassemble-simulator set continue=true                  # yes/no screen\n"
            "  docassemble-simulator set M.parties[0].name.first=Alice  # text field\n"
            "  docassemble-simulator set --code \"M.parties.append_object('Individual')\"\n"
            "\n"
            "Each 'set' writes the values, replays submit-time validation, re-runs\n"
            "the mandatory logic, prints the next screen, and saves the session.\n"
            "\n"
            "Inspection and debugging:\n"
            "\n"
            "  docassemble-simulator status               # re-print the current screen\n"
            "  docassemble-simulator get 'M.parties'      # read a value back\n"
            "  docassemble-simulator vars                 # dump all session variables\n"
            "  docassemble-simulator index --var M.x      # which screen defines M.x?\n"
            "  docassemble-simulator seek M.x --trace     # why does M.x fail to resolve?\n"
            "  docassemble-simulator render template.docx # render against the saved session\n"
        ),
    )
    parser.add_argument(
        "--root", default=None, help="docassemble package directory to operate on (default: current directory)"
    )
    parser.add_argument(
        "--interview",
        default=None,
        help="which interview to load, as docassemble.pkgname:filename.yml (default: auto-detect the package's main.yml)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON instead of human-readable text"
    )
    parser.add_argument(
        "--stub-defined",
        action="store_true",
        help="also stub define()/defined() (needed by a few older packages; on recent docassemble it breaks defined()-branching, so off by default)",
    )
    common = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--config",
        default=None,
        help="extra docassemble server config YAML (merged over discovered simulator TOML config)",
    )
    common.add_argument("--root", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument(
        "--interview", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )
    common.add_argument(
        "--stub-defined",
        dest="stub_defined",
        action="store_true",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_sub(*pargs, **kwargs):
        return sub.add_parser(*pargs, parents=[common], **kwargs)

    p = add_sub(
        "info",
        help="show the detected package, interviews, and versions",
        description="Show what the simulator detected at --root: package name, interview files, and Python/docassemble versions. Needs no session.",
    )
    p.set_defaults(func=cmd_info)

    p = add_sub(
        "check",
        help="compile-parse interviews and report errors without running them",
        description=(
            "Compile-parse the interview(s) through the real docassemble compiler and"
            " report YAML/structure errors. Static analysis only: no session, no logic run."
        ),
    )
    p.set_defaults(func=cmd_check)

    p = add_sub(
        "render",
        help="render a docx template through docassemble's real Jinja pipeline",
        description=(
            "Render TEMPLATE.docx from a package's data/templates directory. By default "
            "the saved session is assembled once before rendering; use --fresh for a new "
            "flow, --no-flow to use saved state without assembling, --from-snapshot to "
            "replay a captured namespace, or --fixture for a Python namespace fixture. "
            "Use --snapshot to capture the namespace after assembly. Missing values are "
            "strict failures and report the template paragraph line. Real "
            "include_docx_template() calls run with docassemble's document context; "
            "templates that depend on attachment assembly (current_context().attachment, "
            "merged attachments, or fillable PDFs) may still need a fixture."
        ),
    )
    p.add_argument("template", metavar="TEMPLATE.docx", help="template filename under data/templates")
    p.add_argument("--fresh", action="store_true", help="discard the saved session and run a fresh flow before rendering")
    p.add_argument("--no-flow", action="store_true", help="skip assembly and render only the saved session state")
    p.add_argument("--fixture", default=None, help="execute this Python script as a render namespace fixture")
    p.add_argument("--expect-missing", default=None, metavar="VAR", help="expect VAR to remain undefined through rendering")
    p.add_argument("--output", default=None, metavar="DIR", help="write the rendered .docx atomically into DIR")
    p.add_argument("--snapshot", default=None, metavar="PATH", help="save the assembled namespace snapshot before rendering")
    p.add_argument("--from-snapshot", default=None, metavar="PATH", help="load a namespace snapshot, assemble once, then render")
    p.set_defaults(func=cmd_render)

    p = add_sub(
        "questions",
        help="list the interview's screens (question blocks) in order",
        description=(
            "List the compiled question blocks ('screens') in source order, with the"
            " variables each one defines. Useful before 'start' to see what the interview"
            " will ask for."
        ),
    )
    p.add_argument("--var", default=None, help="only show screens whose variable or block name contains this substring")
    p.set_defaults(func=cmd_questions)

    p = add_sub(
        "index",
        help="map each variable to the screen that defines it",
        description=(
            "Map variables to the question blocks that define them. Use this to find which"
            " screen asks for a field, or to confirm whether a variable has any defining"
            " screen at all."
        ),
    )
    p.add_argument("--var", default=None, help="only show mappings whose variable name contains this substring")
    p.set_defaults(func=cmd_index)

    p = add_sub(
        "start",
        help="create a fresh session; print the first screen",
        description=(
            "Discard any saved session, create a fresh one, run the mandatory logic until"
            " the interview needs input, and print the first screen with its fields."
            " Run this before 'set', 'status', 'get', 'exec', or 'vars'."
        ),
    )
    p.add_argument(
        "--set",
        nargs="*",
        default=[],
        metavar="VAR=VALUE",
        help=(
            "initial values to apply right after starting (same grammar as the"
            " 'set' command; unlike 'set', submit-time validation is not replayed)"
        ),
    )
    p.add_argument("--code", action="store_true", help="treat --set values as Python expressions evaluated in the session instead of literals")
    p.set_defaults(func=cmd_start)

    p = add_sub(
        "status",
        help="print the current screen of the saved session",
        description=(
            "Re-run the flow on the saved session without changing anything and print the"
            " current screen, including the field names to use with 'set'. Safe to run any"
            " number of times. Requires a session ('start' first)."
        ),
    )
    p.add_argument("--vars", action="store_true", help="also list top-level defined variables")
    p.set_defaults(func=cmd_status)

    p = add_sub(
        "seek",
        help="show which screen asks for VARIABLE, or why it fails to resolve",
        description=(
            "Drive interview.askfor(VARIABLE) directly and report which screen would ask"
            " for it, or the exact failure (e.g. 'could not be looked up in the question"
            " file'). The go-to tool when a variable will not resolve."
        ),
    )
    p.add_argument("variable", help="dotted variable name to resolve, e.g. M.family.parenting_plan.custody_legal_type")
    p.add_argument("--continue", dest="continue_session", action="store_true", help="seek from the saved session instead of fresh state")
    p.add_argument("--save", action="store_true", help="persist the resulting state back to the session")
    p.add_argument("--trace", action="store_true", help="include the full seek chain: every variable/question considered along the way")
    p.set_defaults(func=cmd_seek)

    p = add_sub(
        "set",
        help="answer the current screen, then advance and print the next one",
        description=(
            "Write values into the session under the field names shown by 'status', mark"
            " the current screen answered, replay submit-time validation, re-run the"
            " mandatory logic, print the next screen, and save. Values parse as JSON first"
            " (true/false/null/42/[1,2]/\"text\"), then Python literals, then plain strings."
            " Object-reference fields must be JSON, such as"
            " {\"<safeid-key>\": true}; set receives the current screen automatically."
            " Script drivers using Session.apply_assignments() must pass screen=screen."
            " Requires a session."
        ),
    )
    p.add_argument("assignments", nargs="+", metavar="VAR=VALUE", help="fields to set on the current screen, e.g. M.parties[0].name.first=Alice or continue=true")
    p.add_argument("--code", action="store_true", help="treat values as Python expressions evaluated in the session (e.g. \"M.parties.append_object('Individual')\")")
    p.add_argument("--no-mark-answered", action="store_true", help="do not mark the previous screen's question answered")
    p.add_argument("--no-validate", action="store_true", help="skip the submit-time validation replay entirely")
    p.add_argument("--strict", action="store_true", help="treat unanswered required fields as blocking errors, like a browser would")
    p.set_defaults(func=cmd_set)

    p = add_sub(
        "get",
        help="evaluate an expression in the session and print the result",
        description="Evaluate a Python expression in the interview namespace against the saved session and print its value. Read-only. Requires a session.",
    )
    p.add_argument("expression", help="Python expression over session variables, e.g. M.parties[0].name.first or len(M.parties)")
    p.set_defaults(func=cmd_get)

    p = add_sub(
        "exec",
        help="run arbitrary Python statements in the session (escape hatch)",
        description=(
            "Run arbitrary Python inside the interview namespace of the saved session."
            " Escape hatch for anything 'set' can't express (list appends, object setup);"
            " bypasses the field/validation replay, so prefer 'set' when it fits."
            " Requires a session."
        ),
    )
    p.add_argument("code", nargs="?", default="", help="Python statements to run, e.g. \"M.parties.append_object('Individual')\"")
    p.add_argument("--file", default=None, help="read the code from this file instead of the command line")
    p.add_argument("--show", action="store_true", help="after running, advance the flow and print the current screen")
    p.set_defaults(func=cmd_exec)

    p = add_sub(
        "vars",
        help="dump the saved session's top-level variables with their reprs",
        description="Print every top-level variable currently defined in the saved session, with its repr. Requires a session.",
    )
    p.add_argument("--filter", default=None, help="only show variables whose name contains this substring")
    p.set_defaults(func=cmd_vars)

    return parser


def _reexec_with_dyld_path() -> None:
    """On macOS, dyld reads DYLD_FALLBACK_LIBRARY_PATH at process start.

    Setting it via os.environ after launch has no effect on dlopen, so native
    libs (cairo/weasyprint/zbar) fail to load. Re-exec once with it set.
    """
    import os
    import sys

    value = dyld_fallback_value()
    if value == os.environ.get("DYLD_FALLBACK_LIBRARY_PATH"):
        return
    os.environ["DASIMULATOR_REEXEC"] = "1"
    os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = value
    os.execv(sys.executable, [sys.executable, *sys.argv])


def main(argv: list[str] | None = None) -> int:
    from docassemble_simulator.session import SessionError

    _reexec_with_dyld_path()
    parser = build_parser()
    args = parser.parse_args(argv)
    root = find_package_root(args.root)

    try:
        effective_config_data = load_config(root)
    except ValueError as err:
        raise SystemExit(f"error: {err}") from err

    if getattr(args, "config", None):
        source = Path(args.config).expanduser()
        if not source.exists():
            raise SystemExit(f"error: config {source} does not exist")
        import yaml

        loaded = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise SystemExit(f"error: config {source} must be a YAML mapping")
        deep_merge(effective_config_data, loaded)

    # Root-scoped effective config: merged overrides must never land in the
    # shared home config, where they would leak into unrelated packages.
    effective_config = root / ".simulator" / "config-effective.yml"
    prepare_environment(
        config_path=effective_config,
        extra_config=effective_config_data,
    )
    try:
        return args.func(args, root)
    except SessionError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
