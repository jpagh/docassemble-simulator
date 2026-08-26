import json
from types import SimpleNamespace

from docassemble_simulator import cli


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
        except SystemExit:
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
