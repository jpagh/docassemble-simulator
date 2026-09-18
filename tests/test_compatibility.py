"""Fast compatibility-contract tests that never import a real runtime."""

from __future__ import annotations

import json
import sys

import pytest

from docassemble_simulator import cli, compatibility
from docassemble_simulator._outcomes import ErrorKind


class DASourceError(Exception):
    """Stand-in for the runtime's parser error in fast tests."""


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    compatibility.reset_compatibility_cache()
    yield
    compatibility.reset_compatibility_cache()


def _environment() -> compatibility.RuntimeEnvironment:
    return compatibility.RuntimeEnvironment(
        family="legacy",
        interpreter="/usr/bin/python3",
        versions={
            "docassemble-base": "1.9.13",
            "docassemble-webapp": "1.9.13",
            "docassemble-assemblyline": "4.8.0",
            "docassemble-altoolbox": "0.19.0",
        },
    )


def _fail_probe(monkeypatch):
    def probe():
        raise DASourceError(
            "Syntax error: field label 'alMonthLabel' overwrites previous label, "
            "'Birthdate'"
        )

    monkeypatch.setattr(compatibility, "_compiler_available", lambda: True)
    monkeypatch.setattr(compatibility, "_compile_birthdate_probe", probe)


def _workspace(tmp_path, content="---\nquestion: Hi\nfields:\n  - Name: user_name\n"):
    questions = tmp_path / "docassemble" / "pkg" / "data" / "questions"
    questions.mkdir(parents=True)
    (tmp_path / "docassemble" / "pkg" / "__init__.py").write_text("")
    (questions / "main.yml").write_text(content)
    return tmp_path


def test_runtime_compatibility_exit_code_matches_compile_class():
    assert cli._exit_for_error(ErrorKind.RUNTIME_COMPATIBILITY) == 2


def test_memoized_probe_failure_is_typed_and_actionable(monkeypatch):
    monkeypatch.setattr(compatibility, "assemblyline_installed", lambda: True)
    monkeypatch.setattr(compatibility, "collect_environment", _environment)
    _fail_probe(monkeypatch)

    with pytest.raises(compatibility.AssemblyLineCompatibilityError) as caught:
        compatibility.require_assemblyline_compatibility()

    message = str(caught.value)
    assert "1.9.13" in message and "4.8.0" in message
    assert "alMonthLabel" in message
    details = caught.value.details
    assert details["runtime_family"] == "legacy (docassemble 1.9.x)"
    assert details["docassemble_base"] == "1.9.13"
    assert details["docassemble_assemblyline"] == "4.8.0"
    assert details["failing_capability"] == compatibility.PROBE_CAPABILITY
    assert "docassemble-assemblyline" in details["recovery"]
    assert list(compatibility.SUPPORTED_MATRIX) == details["tested_matrix"]


def test_probe_is_skipped_without_assemblyline(monkeypatch):
    called = []
    monkeypatch.setattr(compatibility, "assemblyline_installed", lambda: False)
    monkeypatch.setattr(
        compatibility, "_compile_birthdate_probe", lambda: called.append(True)
    )

    compatibility.require_assemblyline_compatibility()

    assert called == []


def test_probe_runs_once_per_process(monkeypatch):
    calls = []
    monkeypatch.setattr(compatibility, "assemblyline_installed", lambda: True)
    monkeypatch.setattr(compatibility, "collect_environment", _environment)
    monkeypatch.setattr(compatibility, "_compiler_available", lambda: True)
    monkeypatch.setattr(
        compatibility, "_compile_birthdate_probe", lambda: calls.append(True)
    )

    compatibility.require_assemblyline_compatibility()
    compatibility.require_assemblyline_compatibility()

    assert calls == [True]


def test_compatibility_report_names_matrix_and_probe(monkeypatch):
    monkeypatch.setattr(compatibility, "collect_environment", _environment)
    monkeypatch.setattr(compatibility, "assemblyline_installed", lambda: True)

    report = compatibility.compatibility_report()

    assert report["runtime_family"] == "legacy (docassemble 1.9.x)"
    assert report["assemblyline_installed"] is True
    assert report["probe"]["capability"] == compatibility.PROBE_CAPABILITY
    assert list(compatibility.SUPPORTED_MATRIX) == report["tested_matrix"]


def test_check_json_reports_runtime_compatibility_without_traceback(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(compatibility, "assemblyline_installed", lambda: True)
    monkeypatch.setattr(compatibility, "collect_environment", _environment)
    _fail_probe(monkeypatch)
    root = _workspace(tmp_path)
    args = cli.build_parser().parse_args(["check", "--json"])

    assert cli.cmd_catalog(args, root) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["command"] == "check"
    assert payload["error"]["kind"] == "runtime-compatibility"
    assert "alMonthLabel" in payload["error"]["details"]["underlying_error"]
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_questions_preserves_compatibility_error_without_traceback(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(compatibility, "assemblyline_installed", lambda: True)
    monkeypatch.setattr(compatibility, "collect_environment", _environment)
    _fail_probe(monkeypatch)
    root = _workspace(tmp_path)
    args = cli.build_parser().parse_args(["questions", "--json"])

    assert cli.cmd_catalog(args, root) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["kind"] == "runtime-compatibility"
    assert payload["error"]["details"]["failing_capability"] == (
        compatibility.PROBE_CAPABILITY
    )


def test_start_reports_compatibility_failure_without_creating_a_session(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(compatibility, "assemblyline_installed", lambda: True)
    monkeypatch.setattr(compatibility, "collect_environment", _environment)
    _fail_probe(monkeypatch)
    root = _workspace(tmp_path)
    args = cli.build_parser().parse_args(["start", "--json"])

    assert cli.cmd_execution(args, root) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["kind"] == "runtime-compatibility"
    assert payload["error"]["details"]["docassemble_assemblyline"] == "4.8.0"
    assert not list((root / ".simulator" / "sessions").glob("*.pkl"))


def test_start_compile_failure_uses_the_compile_kind(
    tmp_path, monkeypatch, capsys, da_stubs
):
    import types

    class DAError(Exception):
        pass

    class DASourceError(DAError):
        pass

    error_module = sys.modules["docassemble.base.error"]
    error_module.DAError = DAError
    error_module.DASourceError = DASourceError
    error_module.DANotFoundError = type("DANotFoundError", (Exception,), {})
    parse = sys.modules["docassemble.base.parse"]
    parse.get_initial_dict = lambda: {
        "_internal": {"tracker": 0},
        "url_args": {},
    }
    util = types.ModuleType("docassemble.base.util")

    class DAObject:
        def __init__(self, instanceName=None, **kwargs):
            self.instanceName = instanceName
            self.__dict__.update(kwargs)

    util.DAObject = DAObject
    monkeypatch.setitem(sys.modules, "docassemble.base.util", util)

    def failing_get_interview(identity):
        raise DASourceError("Syntax error: authored Interview defect")

    interview_cache = types.ModuleType("docassemble.base.interview_cache")
    interview_cache.get_interview = failing_get_interview
    monkeypatch.setitem(
        sys.modules, "docassemble.base.interview_cache", interview_cache
    )
    root = _workspace(tmp_path)
    args = cli.build_parser().parse_args(["start", "--json"])

    assert cli.cmd_execution(args, root) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["kind"] == "compile"
    assert "authored Interview defect" in payload["error"]["message"]
    assert not list((root / ".simulator" / "sessions").glob("*.pkl"))


def test_info_reports_runtime_compatibility(tmp_path, monkeypatch, capsys):
    package = tmp_path / "docassemble" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    monkeypatch.setattr(cli, "_reexec_with_dyld_path", lambda: None)
    monkeypatch.setattr(cli, "find_package_root", lambda root: tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda root: {})
    monkeypatch.setattr(compatibility, "collect_environment", _environment)
    monkeypatch.setattr(compatibility, "assemblyline_installed", lambda: True)

    assert cli.main(["--json", "info"]) == 0
    payload = json.loads(capsys.readouterr().out)
    report = payload["result"]["runtime_compatibility"]
    assert report["docassemble_assemblyline"] == "4.8.0"
    assert report["probe"]["capability"] == compatibility.PROBE_CAPABILITY
