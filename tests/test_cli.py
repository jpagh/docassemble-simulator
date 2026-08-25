from docassemble_simulator import cli


def test_exec_subcommand_dispatches_to_exec_handler():
    args = cli.build_parser().parse_args(["exec", "M.value = 1"])

    assert args.func is cli.cmd_exec
    assert args.code == "M.value = 1"
