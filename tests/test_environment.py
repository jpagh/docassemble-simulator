"""Pre-import environment policy for the target document interpreter."""

import os
import subprocess
import sys

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


def test_cli_import_does_not_import_docassemble_or_onnxruntime():
    # A fresh process avoids the CLI and docassemble stubs loaded by other tests.
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "def is_runtime(name):\n"
                "    return name.split('.')[0] in ('docassemble', 'onnxruntime')\n"
                "assert not any(is_runtime(name) for name in sys.modules)\n"
                "attempted = []\n"
                "class RejectRuntimeImport:\n"
                "    def find_spec(self, fullname, path=None, target=None):\n"
                "        if is_runtime(fullname):\n"
                "            attempted.append(fullname)\n"
                "            raise AssertionError('premature runtime import: ' + fullname)\n"
                "sys.meta_path.insert(0, RejectRuntimeImport())\n"
                "import docassemble_simulator.cli\n"
                "assert not attempted, attempted\n"
                "assert not any(is_runtime(name) for name in sys.modules)\n"
            ),
        ],
        env=target_interpreter_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


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
