"""Pre-import environment policy for the target document interpreter."""

import os

import pytest

from docassemble_simulator import cli
from docassemble_simulator._environment import target_interpreter_environment


@pytest.mark.parametrize("value", [None, "0", "1"])
def test_target_interpreter_environment_forces_onnx_telemetry_off(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ORT_DISABLE_TELEMETRY", raising=False)
    else:
        monkeypatch.setenv("ORT_DISABLE_TELEMETRY", value)
    monkeypatch.setenv("PRESERVED", "original")

    environment = target_interpreter_environment()

    assert environment["ORT_DISABLE_TELEMETRY"] == "1"
    assert environment["PRESERVED"] == "original"
    assert os.environ.get("ORT_DISABLE_TELEMETRY") == value


def test_target_interpreter_environment_preserves_explicit_environment():
    original = {"ORT_DISABLE_TELEMETRY": "0", "HOME": "/isolated"}

    assert target_interpreter_environment(original) == {
        "ORT_DISABLE_TELEMETRY": "1",
        "HOME": "/isolated",
    }
    assert original["ORT_DISABLE_TELEMETRY"] == "0"


def test_cli_main_disables_onnx_telemetry_before_reexec_or_runtime_imports(monkeypatch):
    monkeypatch.setenv("ORT_DISABLE_TELEMETRY", "0")

    class ReexecReached(Exception):
        pass

    def reexec():
        assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"
        raise ReexecReached

    monkeypatch.setattr(cli, "_reexec_with_dyld_path", reexec)
    with pytest.raises(ReexecReached):
        cli.main([])
