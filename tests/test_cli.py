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
from docassemble_simulator.config import ResolvedConfiguration


def _run_cli(*arguments):
    return subprocess.run(
        [sys.executable, "-m", "docassemble_simulator", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def test_json_array_journey_becomes_one_protocol_frame_per_request(
    monkeypatch, tmp_path
):
    journey = tmp_path / "journey.json"
    journey.write_text(
        '[{"id":"one","operation":"start"},{"id":"two","operation":"status"}]',
        encoding="utf-8",
    )
    seen = {}

    class FakeWorker:
        closed = False

        def __init__(self, *args, **kwargs):
            pass

    def fake_serve(worker, input_stream, output_stream):
        seen["requests"] = [json.loads(line) for line in input_stream]
        return 0

    monkeypatch.setattr("docassemble_simulator.worker.Worker", FakeWorker)
    monkeypatch.setattr("docassemble_simulator.worker.serve", fake_serve)
    args = types.SimpleNamespace(
        session_root=str(tmp_path / "sessions"),
        journey=str(journey),
        interview=None,
        timeout=1.0,
        timings=False,
        project_root=None,
    )

    assert cli.cmd_worker(args, tmp_path) == 0
    assert [request["id"] for request in seen["requests"]] == ["one", "two"]


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
    seek = parser.parse_args(["seek", "M.value", "--fresh", "--activate", "--auto"])
    defaults = parser.parse_args(["answer", "--defaults"])
    why = parser.parse_args(["why", "M.value", "--strict"])

    assert answer.func is cli.cmd_execution
    assert evaluate.command == "eval"
    assert execute.no_assemble is True
    assert seek.fresh and seek.activate and seek.auto
    assert defaults.defaults and defaults.assignments == []
    assert why.variable == "M.value" and why.strict
    for obsolete in (["set", "x=1"], ["get", "x"]):
        try:
            parser.parse_args(obsolete)
        except (SystemExit, cli.UsageFailure):
            pass
        else:
            raise AssertionError(f"obsolete command accepted: {obsolete[0]}")


def test_worker_setup_output_is_not_written_to_protocol(monkeypatch, tmp_path, capsys):
    import docassemble_simulator.worker as worker_module

    protocol = __import__("io").StringIO()

    class PrintingWorker:
        def __init__(self, *args, **kwargs):
            print("bootstrap output")

    def fake_serve(worker, stream, output):
        output.write(json.dumps({"ok": True}) + "\\n")
        return 0

    monkeypatch.setattr(worker_module, "Worker", PrintingWorker)
    monkeypatch.setattr(worker_module, "serve", fake_serve)
    args = SimpleNamespace(
        session_root=str(tmp_path / "sessions"),
        interview=None,
        timeout=1.0,
        timings=False,
        project_root=None,
        journey=None,
        _worker_protocol_stdout=protocol,
    )

    assert cli.cmd_worker(args, tmp_path) == 0
    captured = capsys.readouterr()
    assert protocol.getvalue() == '{"ok": true}\\n'
    assert "bootstrap output" in captured.err


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
        "diagnostics": {
            "count": 1,
            "counts": {"variable-seek": 1},
            "head": [
                {
                    "kind": "variable-seek",
                    "message": "seeking M.value",
                    "details": {"variable": "M.value"},
                }
            ],
            "tail": [
                {
                    "kind": "variable-seek",
                    "message": "seeking M.value",
                    "details": {"variable": "M.value"},
                }
            ],
            "truncated": False,
        },
    }


def test_answer_rejection_guidance_matches_json_and_human_output(
    monkeypatch, tmp_path, capsys
):
    details = {
        "errors": ["typo: not a field on the active screen"],
        "assignments": [
            {"variable": "typo", "input": "value", "disposition": "unknown field"}
        ],
        "active_fields": ["name", "filing_date"],
        "closest_matches": {"typo": "name"},
        "missing_required": ["name"],
    }
    outcome = SimpleNamespace(
        ok=False,
        result=None,
        error=Failure(
            ErrorKind.ANSWER_INPUT,
            "one or more answers could not be applied",
            details,
        ),
        diagnostics=(),
        attachments=(),
    )
    monkeypatch.setattr(
        cli,
        "_execution",
        lambda args, root: SimpleNamespace(run=lambda operation: outcome),
    )

    json_args = cli.build_parser().parse_args(["answer", "typo=value", "--json"])
    assert cli.cmd_execution(json_args, tmp_path) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["details"] == details

    human_args = cli.build_parser().parse_args(["answer", "typo=value"])
    assert cli.cmd_execution(human_args, tmp_path) == 2
    human = capsys.readouterr().err
    assert "active_fields" in human
    assert "closest_matches" in human
    assert "missing_required" in human
    assert "name" in human


def test_unresolved_culprit_matches_json_and_human_failure_output(
    monkeypatch, tmp_path, capsys
):
    culprit = {
        "provenance": {"id": "choices-source", "location": "main.yml:4"},
        "expression": "missing_choices",
        "sought_variable": "missing_choices",
    }
    outcome = SimpleNamespace(
        ok=False,
        result=None,
        error=Failure(
            ErrorKind.UNRESOLVED_VARIABLE,
            "could not define missing_choices",
            {"sought_variable": "missing_choices", "culprit": culprit},
        ),
        diagnostics=(
            Diagnostic(
                "variable-seek",
                "seeking missing_choices",
                {"variable": "missing_choices", "culprit": culprit},
            ),
        ),
        attachments=(),
    )
    monkeypatch.setattr(
        cli,
        "_execution",
        lambda args, root: SimpleNamespace(run=lambda operation: outcome),
    )

    json_args = cli.build_parser().parse_args(["start", "--json"])
    assert cli.cmd_execution(json_args, tmp_path) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["kind"] == "unresolved-variable"
    assert payload["error"]["details"]["culprit"] == culprit

    human_args = cli.build_parser().parse_args(["start"])
    assert cli.cmd_execution(human_args, tmp_path) == 2
    human = capsys.readouterr().err
    assert "choices-source" in human
    assert "missing_choices" in human


def test_why_uses_the_read_only_execution_seam(monkeypatch, tmp_path, capsys):
    from docassemble_simulator.execution import ExplainResolution

    seen = {}

    class FakeExecution:
        def run(self, operation):
            seen["operation"] = operation
            return types.SimpleNamespace(
                ok=True,
                result={
                    "variable": "M.value",
                    "candidates": [],
                    "winner": None,
                    "conflict": False,
                },
                error=None,
                diagnostics=(),
                attachments=(),
            )

    monkeypatch.setattr(cli, "_execution", lambda args, root: FakeExecution())
    args = cli.build_parser().parse_args(["why", "M.value", "--json"])

    assert cli.cmd_catalog(args, tmp_path) == 0
    assert isinstance(seen["operation"], ExplainResolution)
    assert seen["operation"].variable == "M.value"
    assert json.loads(capsys.readouterr().out)["result"]["variable"] == "M.value"


def test_why_conflict_is_warning_by_default_and_strict_failure(
    monkeypatch, tmp_path, capsys
):
    class FakeExecution:
        def run(self, operation):
            return SimpleNamespace(
                ok=True,
                result={
                    "variable": "M.value",
                    "candidates": [{"id": "first"}, {"id": "second"}],
                    "winner": {"id": "first"},
                    "conflict": True,
                },
                error=None,
                diagnostics=(),
                attachments=(),
            )

    monkeypatch.setattr(cli, "_execution", lambda args, root: FakeExecution())

    warning_args = cli.build_parser().parse_args(["why", "M.value", "--json"])
    assert cli.cmd_catalog(warning_args, tmp_path) == 0
    warning = json.loads(capsys.readouterr().out)
    assert warning["ok"] is True
    assert warning["result"]["conflict"] is True

    strict_args = cli.build_parser().parse_args(
        ["why", "M.value", "--strict", "--json"]
    )
    assert cli.cmd_catalog(strict_args, tmp_path) == 2
    failure = json.loads(capsys.readouterr().out)
    assert failure["ok"] is False
    assert failure["error"]["kind"] == "resolution-conflict"
    assert failure["error"]["details"]["conflict"] is True


def test_check_human_output_ends_with_totals(monkeypatch, tmp_path, capsys):
    class FakeCatalog:
        def __init__(self, root, selector):
            pass

        def check(self):
            return SimpleNamespace(
                interview=None,
                result={
                    "checked": 2,
                    "failures": 1,
                    "totals": {"checked": 2, "ok": 1, "failed": 1},
                    "results": [],
                },
            )

    monkeypatch.setattr("docassemble_simulator.catalog.InterviewCatalog", FakeCatalog)
    args = cli.build_parser().parse_args(["check"])

    assert cli.cmd_catalog(args, tmp_path) == 2
    assert (
        capsys.readouterr()
        .out.rstrip()
        .endswith("checked: 2  ok: 1  failures: 1  duplicate ids: 0")
    )


def test_check_strict_fails_on_duplicate_ids(monkeypatch, tmp_path, capsys):
    class FakeCatalog:
        def __init__(self, root, selector):
            pass

        def check(self):
            return SimpleNamespace(
                interview=None,
                result={
                    "checked": 1,
                    "failures": 0,
                    "totals": {"checked": 1, "ok": 1, "failed": 0, "duplicate_ids": 1},
                    "duplicate_ids": [{"kind": "duplicate-id", "id": "shared"}],
                    "results": [],
                },
            )

    monkeypatch.setattr("docassemble_simulator.catalog.InterviewCatalog", FakeCatalog)
    args = cli.build_parser().parse_args(["check", "--strict", "--json"])

    assert cli.cmd_catalog(args, tmp_path) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["kind"] == "duplicate-id"
    assert payload["error"]["details"]["duplicate_ids"][0]["id"] == "shared"


def test_check_compile_failure_precedes_duplicate_warning_in_both_modes(
    monkeypatch, tmp_path, capsys
):
    class FakeCatalog:
        def __init__(self, root, selector):
            pass

        def check(self):
            return SimpleNamespace(
                interview=None,
                result={
                    "checked": 2,
                    "failures": 1,
                    "totals": {
                        "checked": 2,
                        "ok": 1,
                        "failed": 1,
                        "duplicate_ids": 1,
                    },
                    "duplicate_ids": [{"kind": "duplicate-id", "id": "shared"}],
                    "results": [
                        {
                            "interview": "docassemble.broken:data/questions/main.yml",
                            "ok": False,
                            "message": "authored compile failure",
                        },
                        {
                            "interview": "docassemble.duplicate:data/questions/main.yml",
                            "ok": True,
                        },
                    ],
                },
            )

    monkeypatch.setattr("docassemble_simulator.catalog.InterviewCatalog", FakeCatalog)
    expected_totals = {
        "checked": 2,
        "ok": 1,
        "failed": 1,
        "duplicate_ids": 1,
    }

    for strict in (False, True):
        flags = ["check", "--json"]
        if strict:
            flags.insert(1, "--strict")
        args = cli.build_parser().parse_args(flags)

        assert cli.cmd_catalog(args, tmp_path) == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"]["kind"] == "compile"
        assert payload["error"]["message"] == "one or more interviews failed to compile"
        assert payload["error"]["details"]["totals"] == expected_totals

    for strict in (False, True):
        flags = ["check"]
        if strict:
            flags.append("--strict")
        args = cli.build_parser().parse_args(flags)

        assert cli.cmd_catalog(args, tmp_path) == 2
        captured = capsys.readouterr()
        assert "error: one or more interviews failed to compile" in captured.err
        assert captured.out.rstrip().endswith(
            "checked: 2  ok: 1  failures: 1  duplicate ids: 1"
        )


def test_diagnostic_summary_is_bounded_and_counts_kinds():
    events = [
        Diagnostic("variable-seek" if index % 2 else "other", str(index), {})
        for index in range(12)
    ]

    summary = cli._diagnostic_summary(events, limit=3)

    assert summary["count"] == 12
    assert summary["counts"] == {"other": 6, "variable-seek": 6}
    assert [item["message"] for item in summary["head"]] == ["0", "1", "2"]
    assert [item["message"] for item in summary["tail"]] == ["9", "10", "11"]
    assert summary["truncated"] is True


def test_explicit_full_diagnostics_keeps_ordered_events(monkeypatch, tmp_path, capsys):
    outcome = SimpleNamespace(
        ok=True,
        result={"kind": "finished"},
        error=None,
        diagnostics=(
            Diagnostic("variable-seek", "first", {}),
            Diagnostic("variable-seek", "second", {}),
        ),
        attachments=(),
    )
    monkeypatch.setattr(
        cli,
        "_execution",
        lambda args, root: SimpleNamespace(run=lambda operation: outcome),
    )
    args = cli.build_parser().parse_args(["start", "--json", "--full-seek-diagnostics"])

    assert cli.cmd_execution(args, tmp_path) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["message"] for item in payload["diagnostics"]] == [
        "first",
        "second",
    ]


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


def test_config_report_redacts_effective_and_shadowed_secrets_in_json(tmp_path, capsys):
    resolved = ResolvedConfiguration(
        tmp_path,
        {
            "api_key": "SYNTHETIC_EFFECTIVE_API",
            "service": {
                "api_key": "SYNTHETIC_EFFECTIVE_NESTED_API",
                "tokens": ["SYNTHETIC_EFFECTIVE_TOKEN"],
                "label": "visible",
            },
            "safe_label": "visible-safe",
        },
        (),
        layers=(
            (
                "ancestor",
                {
                    "api_key": "SYNTHETIC_SHADOWED_API",
                    "service": {"api_key": "SYNTHETIC_SHADOWED_NESTED_API"},
                },
            ),
            (
                "project",
                {
                    "api_key": "SYNTHETIC_EFFECTIVE_API",
                    "service": {
                        "api_key": "SYNTHETIC_EFFECTIVE_NESTED_API",
                        "tokens": ["SYNTHETIC_EFFECTIVE_TOKEN"],
                        "label": "visible",
                    },
                },
            ),
        ),
    )
    args = SimpleNamespace(
        _resolved_configuration=resolved,
        _simulator_config=resolved.values,
        json=True,
    )

    assert cli.cmd_config(args, tmp_path) == 0
    payload = json.loads(capsys.readouterr().out)
    report = payload["result"]
    assert report["provenance"]["api_key"]["value"] == "<redacted>"
    assert report["provenance"]["api_key"]["shadowed"][-1]["value"] == "<redacted>"
    assert report["provenance"]["service"]["value"] == {
        "api_key": "<redacted>",
        "tokens": "<redacted>",
        "label": "visible",
    }
    serialized = json.dumps(payload)
    assert "SYNTHETIC_" not in serialized
    assert "visible-safe" in serialized


def test_config_report_redacts_secrets_in_human_output(tmp_path, capsys):
    resolved = ResolvedConfiguration(
        tmp_path,
        {
            "password": "SYNTHETIC_EFFECTIVE_PASSWORD",
            "safe_label": "visible-safe",
        },
        (),
        layers=(("ancestor", {"password": "SYNTHETIC_SHADOWED_PASSWORD"}),),
    )
    args = SimpleNamespace(
        _resolved_configuration=resolved,
        _simulator_config=resolved.values,
        json=False,
    )

    assert cli.cmd_config(args, tmp_path) == 0
    human = capsys.readouterr().out
    assert "SYNTHETIC_" not in human
    assert "<redacted>" in human
    assert "visible-safe" in human


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


def test_worker_help_documents_its_isolated_default_session():
    help_result = _run_cli("run", "--help")

    assert help_result.returncode == 0
    assert "temporary isolated session" in help_result.stdout


def test_screen_trace_workflow_is_discoverable_from_help():
    top = _run_cli("--help")
    start = _run_cli("start", "--help")
    compare = _run_cli("trace", "compare", "--help")
    replay = _run_cli("trace", "replay", "--help")

    assert top.returncode == 0
    assert "trace" in top.stdout
    assert "compare screen traces recorded with --record" in top.stdout
    assert start.returncode == 0
    assert "--record PATH" in start.stdout
    assert "trace compare" in start.stdout
    assert compare.returncode == 0
    assert "unordered" in compare.stdout
    assert "phased" in compare.stdout
    assert "--update" in compare.stdout
    assert "golden trace" in compare.stdout
    assert replay.returncode == 0
    assert "--override" in replay.stdout


def test_phase_without_record_is_an_input_error():
    completed = _run_cli("--json", "start", "--phase", "intake")

    assert completed.returncode == 1
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    assert payload["error"]["kind"] == "input"
    assert "--record" in payload["error"]["message"]


def test_phases_without_phased_order_is_an_input_error():
    completed = _run_cli(
        "--json",
        "trace",
        "compare",
        "expected.jsonl",
        "actual.jsonl",
        "--phases",
        "a,b",
    )

    assert completed.returncode == 1
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    assert payload["error"]["kind"] == "input"
    assert "--order phased" in payload["error"]["message"]


def test_answer_screen_submission_flags_are_parsed_and_documented():
    parser = cli.build_parser()
    partial = parser.parse_args(["answer", "x=1", "--partial"])
    default = parser.parse_args(["answer", "x=1"])

    assert partial.partial is True
    assert default.partial is False

    help_result = _run_cli("answer", "--help")
    help_text = " ".join(help_result.stdout.split())
    assert help_result.returncode == 0
    assert "--partial" in help_text
    assert "whole screen" in help_text
    assert "field gate" in help_text
    assert "JSON first" in help_text
    assert "checkbox groups" in help_text
    assert "YYYY-MM-" in help_text
