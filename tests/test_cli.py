import json
import subprocess
import sys
from types import SimpleNamespace

from docassemble_simulator import cli


def _run_cli(*arguments):
    return subprocess.run(
        [sys.executable, "-m", "docassemble_simulator", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


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
    outcome = SimpleNamespace(ok=True, result={"kind": "finished"}, error=None)
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
    }


def test_render_json_presents_a_typed_outcome(monkeypatch, tmp_path, capsys):
    from docassemble_simulator import render as render_module

    outcome = render_module.RenderOutcome(
        True,
        render_module.RenderResult(
            "form.docx", 4, tmp_path / "artifact.docx"
        ),
    )
    monkeypatch.setattr(cli, "_execution", lambda args, root: object())
    monkeypatch.setattr(
        render_module.InterviewRenderer,
        "render",
        lambda self, request: outcome,
    )
    args = cli.build_parser().parse_args(
        ["render", "form.docx", "--fresh", "--json"]
    )

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
    monkeypatch.setattr(cli, "prepare_environment", lambda **kwargs: None)
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
    monkeypatch.setattr(cli, "prepare_environment", lambda **kwargs: None)
    monkeypatch.setattr(cli, "ensure_importable", lambda root: calls.append("import"))
    monkeypatch.setattr(cli, "bootstrap", lambda **kwargs: calls.append("bootstrap"))
    monkeypatch.setattr(cli, "cmd_execution", lambda args, root: 0)

    assert cli.main(["start"]) == 0
    assert calls == ["import", "bootstrap"]


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
