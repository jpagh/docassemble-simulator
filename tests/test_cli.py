import json
import subprocess
import sys
import types
from types import SimpleNamespace

from stub_runtime import (
    stub_incomplete_runtime as _stub_incomplete_runtime,
)

from docassemble_simulator import cli
from docassemble_simulator._diagnostics import Diagnostic
from docassemble_simulator._outcomes import ErrorKind, Failure, PublishedAttachment


def _run_cli(*arguments):
    return subprocess.run(
        [sys.executable, "-m", "docassemble_simulator", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def test_error_kinds_keep_exit_codes_and_json_values_stable():
    assert cli._exit_for_error(ErrorKind.INPUT) == 1
    assert cli._exit_for_error(ErrorKind.FAULT) == 3
    assert cli._exit_for_error("future-kind") == 2
    assert cli._envelope("answer", error=Failure(ErrorKind.INPUT, "bad", {})) == {
        "ok": False,
        "command": "answer",
        "error": {"kind": ErrorKind.INPUT, "message": "bad", "details": {}},
    }


def test_json_usage_failure_uses_the_envelope_and_exit_code_one():
    completed = _run_cli("--json", "answer")

    assert completed.returncode == 1
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {
        "ok": False,
        "command": "answer",
        "error": {
            "kind": "input",
            "message": "the following arguments are required: assignments",
            "details": {},
        },
    }


def test_unknown_json_command_uses_cli_usage_envelope():
    completed = _run_cli("--json", "unknown-command")

    assert completed.returncode == 1
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    assert payload["command"] == "cli"
    assert payload["error"]["kind"] == "input"
    assert "invalid choice" in payload["error"]["message"]


def test_non_json_usage_is_concise_and_help_still_succeeds():
    failure = _run_cli("answer")
    help_result = _run_cli("--help")

    assert failure.returncode == 1
    assert failure.stdout == ""
    assert failure.stderr.startswith("error: ")
    assert "assignments" in failure.stderr
    assert help_result.returncode == 0
    assert "usage: docassemble-simulator" in help_result.stdout
    assert help_result.stderr == ""


def test_render_source_flags_are_mutually_exclusive_usage():
    completed = _run_cli(
        "--json", "render", "form.docx", "--fresh", "--fixture", "fixture.py"
    )

    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["command"] == "render"
    assert payload["error"]["kind"] == "input"
    assert "not allowed with argument" in payload["error"]["message"]


def test_command_contract_uses_new_names_and_flags():
    parser = cli.build_parser()
    answer = parser.parse_args(["answer", "M.value=1"])
    evaluate = parser.parse_args(["eval", "M.value"])
    execute = parser.parse_args(["exec", "M.value = 1", "--no-assemble"])
    seek = parser.parse_args(["seek", "M.value", "--fresh", "--activate"])

    assert answer.func is cli.cmd_execution
    assert evaluate.command == "eval"
    assert execute.no_assemble is True
    assert seek.fresh and seek.activate
    for obsolete in (["set", "x=1"], ["get", "x"]):
        try:
            parser.parse_args(obsolete)
        except (SystemExit, cli.UsageFailure):
            pass
        else:
            raise AssertionError(f"obsolete command accepted: {obsolete[0]}")


def test_execution_json_uses_one_envelope(monkeypatch, tmp_path, capsys):
    outcome = SimpleNamespace(
        ok=True,
        result={"kind": "finished"},
        error=None,
        diagnostics=(
            Diagnostic(
                "variable-seek",
                "seeking M.value",
                {"variable": "M.value"},
            ),
        ),
        attachments=(),
    )
    monkeypatch.setattr(
        cli,
        "_execution",
        lambda args, root: SimpleNamespace(run=lambda operation: outcome),
    )
    args = cli.build_parser().parse_args(["start", "--json"])

    assert cli.cmd_execution(args, tmp_path) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "command": "start",
        "result": {"kind": "finished"},
        "diagnostics": [
            {
                "kind": "variable-seek",
                "message": "seeking M.value",
                "details": {"variable": "M.value"},
            }
        ],
    }


def test_human_output_labels_published_attachment_diagnostics(tmp_path, capsys):
    attachment = PublishedAttachment(
        "plan.docx",
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        tmp_path / "plan.docx",
        (tmp_path / "plan.docx").as_uri(),
        (
            Diagnostic(
                "docx-structure",
                "document contains a nested paragraph",
                {"problem": "nested-paragraph"},
            ),
        ),
    )

    cli._emit(
        cli._envelope("start", {"kind": "finished"}, attachments=(attachment,)),
        False,
    )

    output = capsys.readouterr().out
    assert "attachments:" in output
    assert "filename: plan.docx" in output
    assert "docx-structure" in output
    assert "nested-paragraph" in output


def test_config_report_includes_command_line_runtime_overrides(tmp_path):
    args = SimpleNamespace(
        offline=True,
        background_actions="disabled",
        seek_diagnostics="off",
        config=None,
    )

    report = cli._config_report(tmp_path, {}, args)

    assert report["simulator"]["offline"] is True
    assert report["simulator"]["background_actions"] == "disabled"
    assert report["simulator"]["seek_diagnostics"] == "off"
    assert report["command_line_overrides"] == {
        "offline": True,
        "background_actions": "disabled",
        "seek_diagnostics": "off",
    }


def test_seek_diagnostics_flag_is_accepted_on_subcommands():
    parser = cli.build_parser()
    start = parser.parse_args(["start", "--seek-diagnostics", "off"])
    render = parser.parse_args(["render", "form.docx", "--seek-diagnostics", "off"])
    assert start.seek_diagnostics == "off"
    assert render.seek_diagnostics == "off"


def test_render_requires_explicit_fixture_source(monkeypatch, tmp_path, capsys):
    from docassemble_simulator import render as render_module

    (tmp_path / ".config" / "simulator").mkdir(parents=True)
    (tmp_path / ".config" / "simulator" / "fixture.py").write_text(
        "value = 1\n", encoding="utf-8"
    )
    captured = []
    monkeypatch.setattr(cli, "_execution", lambda args, root: object())
    monkeypatch.setattr(
        render_module.InterviewRenderer,
        "render",
        lambda self, request: (
            captured.append(request)
            or render_module.RenderOutcome(
                True, render_module.RenderResult("form.docx", 0)
            )
        ),
    )
    args = cli.build_parser().parse_args(["render", "form.docx"])

    assert cli.cmd_render(args, tmp_path) == 0
    assert isinstance(captured[0].source, render_module.SavedSessionSource)


def test_render_json_presents_a_typed_outcome(monkeypatch, tmp_path, capsys):
    from docassemble_simulator import render as render_module

    outcome = render_module.RenderOutcome(
        True,
        render_module.RenderResult("form.docx", 4, tmp_path / "artifact.docx"),
    )
    monkeypatch.setattr(cli, "_execution", lambda args, root: object())
    monkeypatch.setattr(
        render_module.InterviewRenderer,
        "render",
        lambda self, request: outcome,
    )
    args = cli.build_parser().parse_args(["render", "form.docx", "--fresh", "--json"])

    assert cli.cmd_render(args, tmp_path) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "command": "render",
        "result": {
            "template": "form.docx",
            "paragraphs": 4,
            "artifact": str(tmp_path / "artifact.docx"),
        },
    }


def test_render_human_output_presents_typed_result_fields(
    monkeypatch, tmp_path, capsys
):
    from docassemble_simulator import render as render_module

    monkeypatch.setattr(cli, "_execution", lambda args, root: object())
    monkeypatch.setattr(
        render_module.InterviewRenderer,
        "render",
        lambda self, request: render_module.RenderOutcome(
            True, render_module.RenderResult("form.docx", 4)
        ),
    )
    args = cli.build_parser().parse_args(["render", "form.docx", "--fresh"])

    assert cli.cmd_render(args, tmp_path) == 0
    output = capsys.readouterr().out
    assert output == "template: form.docx\nparagraphs: 4\n"


def test_composition_root_skips_runtime_for_info_and_status(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cli, "_reexec_with_dyld_path", lambda: None)
    monkeypatch.setattr(cli, "find_package_root", lambda root: tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda root: {})
    monkeypatch.setattr(cli, "ensure_importable", lambda root: calls.append("import"))
    monkeypatch.setattr(cli, "bootstrap", lambda **kwargs: calls.append("bootstrap"))
    monkeypatch.setattr(cli, "cmd_info", lambda args, root: 0)
    monkeypatch.setattr(cli, "cmd_execution", lambda args, root: 0)

    assert cli.main(["info"]) == 0
    assert cli.main(["status"]) == 0
    assert calls == []


def test_composition_root_bootstraps_runtime_command_once(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cli, "_reexec_with_dyld_path", lambda: None)
    monkeypatch.setattr(cli, "find_package_root", lambda root: tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda root: {})
    monkeypatch.setattr(cli, "ensure_importable", lambda root: calls.append("import"))
    monkeypatch.setattr(cli, "bootstrap", lambda **kwargs: calls.append("bootstrap"))
    monkeypatch.setattr(cli, "cmd_execution", lambda args, root: 0)

    assert cli.main(["start"]) == 0
    assert calls == ["import", "bootstrap"]


def test_incomplete_runtime_reports_structured_input_error(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(cli, "_reexec_with_dyld_path", lambda: None)
    monkeypatch.setattr(cli, "find_package_root", lambda root: tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda root: {})
    monkeypatch.setattr(cli, "ensure_importable", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)
    _stub_incomplete_runtime(monkeypatch, with_server=False)
    saved_argv = list(sys.argv)
    try:
        assert cli.main(["--json", "start"]) == 1
    finally:
        sys.argv[:] = saved_argv
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert "docassemble.base.functions.server" in payload["error"]["message"]
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_modern_runtime_missing_webapp_reports_structured_input_error(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(cli, "_reexec_with_dyld_path", lambda: None)
    monkeypatch.setattr(cli, "find_package_root", lambda root: tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda root: {})
    monkeypatch.setattr(cli, "ensure_importable", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)
    _stub_incomplete_runtime(monkeypatch, with_server=True, modern=True)
    saved_argv = list(sys.argv)
    try:
        assert cli.main(["--json", "start"]) == 1
    finally:
        sys.argv[:] = saved_argv
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert "webapp" in payload["error"]["message"]
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_missing_runtime_reports_structured_input_error(monkeypatch, tmp_path, capsys):
    from stub_runtime import blocked_docassemble_imports

    monkeypatch.setattr(cli, "_reexec_with_dyld_path", lambda: None)
    monkeypatch.setattr(cli, "find_package_root", lambda root: tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda root: {})
    monkeypatch.setattr(cli, "ensure_importable", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)
    saved_argv = list(sys.argv)
    try:
        with blocked_docassemble_imports(monkeypatch):
            assert cli.main(["--json", "start"]) == 1
    finally:
        sys.argv[:] = saved_argv
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert "No docassemble runtime" in payload["error"]["message"]
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_broken_runtime_context_reports_structured_compile_error(
    monkeypatch, tmp_path, capsys
):

    monkeypatch.setattr(cli, "_reexec_with_dyld_path", lambda: None)
    monkeypatch.setattr(cli, "find_package_root", lambda root: tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda root: {})
    monkeypatch.setattr(cli, "ensure_importable", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)
    # functions has a server but no thread-local state, so bootstrap passes
    # while catalog compilation fails inside runtime_context's validation.
    # The interview_cache stub keeps the import reachable; runtime_context
    # raises before get_interview is ever called.
    _stub_incomplete_runtime(monkeypatch, with_server=True)
    interview_cache = types.ModuleType("docassemble.base.interview_cache")
    interview_cache.get_interview = lambda identity: types.SimpleNamespace(
        questions_list=[]
    )
    monkeypatch.setitem(
        sys.modules, "docassemble.base.interview_cache", interview_cache
    )
    package = tmp_path / "docassemble" / "regression"
    questions = package / "data" / "questions"
    questions.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (questions / "main.yml").write_text(
        "---\nmandatory: True\nquestion: Hi\nfields:\n  - Name: user_name\n"
    )
    saved_argv = list(sys.argv)
    try:
        assert cli.main(["--json", "check"]) == 2
    finally:
        sys.argv[:] = saved_argv
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["error"]["kind"] == "compile"
    assert "functions.this_thread" in json.dumps(payload["error"])
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_render_parser_models_orthogonal_request_concerns():
    args = cli.build_parser().parse_args(
        [
            "render",
            "form.docx",
            "--fresh",
            "--no-assemble",
            "--save-snapshot",
            "state.pkl",
            "--output",
            "artifact.docx",
        ]
    )
    assert args.fresh and args.no_assemble
    assert args.save_snapshot == "state.pkl"
    assert args.output == "artifact.docx"
