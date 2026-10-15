"""Linux-observable prevention of ORT's native telemetry initialization."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from test_real_runtime import (  # noqa: F401 - fixtures registered by import
    _environment,
    family_python,
    real_python,
    real_python_19,
)

from docassemble_simulator.runner import SimulatorRunner

pytestmark = [
    pytest.mark.real_runtime,
    pytest.mark.skipif(
        sys.platform != "linux", reason="Linux telemetry cache observable"
    ),
]


@pytest.mark.parametrize("launch", ["runner", "worker", "one-shot"])
def test_target_interpreter_launch_prevents_onnx_telemetry_state(
    family_python,  # noqa: F811
    tmp_path,
    monkeypatch,
    launch,
    capfd,
):
    interpreter = family_python.interpreter
    available = subprocess.run(
        [
            str(interpreter),
            "-c",
            (
                "import importlib.util, pathlib; "
                "assert importlib.util.find_spec('onnxruntime'); "
                "import nltk; "
                "print(next((p for p in nltk.data.path if all("
                "(pathlib.Path(p) / 'corpora' / (name + '.zip')).is_file() "
                "for name in ('omw-1.4', 'wordnet', 'wordnet_ic', 'sentiwordnet'))), ''))"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if available.returncode:
        pytest.skip("onnxruntime or nltk is not installed in the target interpreter")
    nltk_data = available.stdout.strip()
    if not nltk_data:
        pytest.skip(
            "target interpreter needs pre-provisioned NLTK corpora; no downloads"
        )
    monkeypatch.setenv("NLTK_DATA", nltk_data)
    home = tmp_path / "home"
    home.mkdir()
    cache = home / ".cache"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    # An explicit opt-in must not defeat the process-safety mitigation.
    monkeypatch.setenv("ORT_DISABLE_TELEMETRY", "0")
    project = tmp_path / "project"
    package = project / "docassemble" / "telemetryprobe"
    questions = package / "data" / "questions"
    questions.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "helpers.py").write_text(
        "import os\n"
        "assert os.environ['ORT_DISABLE_TELEMETRY'] == '1'\n"
        "import onnxruntime\n"
        "def telemetry_probe_loaded():\n"
        "    return onnxruntime.__version__\n"
    )
    (questions / "main.yml").write_text(
        "---\nmodules:\n  - docassemble.telemetryprobe.helpers\n"
        "---\nmandatory: True\nquestion: Telemetry probe\n"
        "subquestion: ${ telemetry_probe_loaded() }\n"
        "fields:\n  - Name: name\n"
    )
    if launch == "runner":
        with SimulatorRunner(
            project,
            interview="main.yml",
            python=interpreter,
            offline=True,
            timeout=120,
        ) as runner:
            assert runner.start().ok
            process = runner._process
            assert runner.request("shutdown").ok
            assert process is not None
            assert process.wait(timeout=30) == 0
            pid = process.pid
        assert capfd.readouterr().err == ""
    else:
        command = [
            str(interpreter),
            "-m",
            "docassemble_simulator",
            "--root",
            str(project),
            "--interview",
            "main.yml",
            "--offline",
            "--json",
        ]
        if launch == "worker":
            command.append("worker")
            requests = [
                {"id": "start", "operation": "start"},
                {"id": "shutdown", "operation": "shutdown"},
            ]
            input_text = "".join(json.dumps(request) + "\n" for request in requests)
        else:
            command.append("start")
            input_text = None
        with subprocess.Popen(
            command,
            env=_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            try:
                stdout, stderr = process.communicate(input_text, timeout=180)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
            assert process.returncode == 0, (stdout, stderr)
            assert stderr == ""
            frames = (
                [json.loads(line) for line in stdout.splitlines()]
                if launch == "worker"
                else [json.loads(stdout)]
            )
            assert all(frame["ok"] for frame in frames), frames
            assert len(frames) == (2 if launch == "worker" else 1)
            if launch == "worker":
                assert frames[-1]["result"]["kind"] == "shutdown"
            pid = process.pid

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not (cache / "Microsoft" / "DeveloperTools" / ".onnxruntime").exists()
