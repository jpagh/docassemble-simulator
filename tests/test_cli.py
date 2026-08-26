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
