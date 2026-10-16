"""Platform-aware observation of ORT's native telemetry SDK initialization."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from functools import partial
from pathlib import Path

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
        sys.platform not in {"linux", "darwin"},
        reason="telemetry state paths are known on Linux and macOS",
    ),
]


def _telemetry_state_directory(home: Path, cache: Path) -> Path:
    # Upstream posix/device_id.cc ignores XDG_CACHE_HOME on Apple.
    base = (
        home / "Library" / "Application Support" if sys.platform == "darwin" else cache
    )
    return base / "Microsoft" / "DeveloperTools" / ".onnxruntime"


def _assert_process_group_exited(pgid: int, *, timeout: float = 5) -> None:
    # Each launch gets a new session so the group includes inherited descendants,
    # even after the direct child is reaped. Allow bounded orphan-reaping latency.
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        assert time.monotonic() < deadline, f"surviving worker process group {pgid}"
        time.sleep(0.05)


def test_process_group_exit_check_detects_survivor_after_leader_exits():
    command = [sys.executable, "-c", "import time; time.sleep(60)"]
    # Both are our children so we can reap them on Linux as well as macOS.
    # The second process models a descendant's inherited group membership.
    with subprocess.Popen(command, process_group=0) as leader:
        try:
            with subprocess.Popen(command, process_group=leader.pid) as member:
                try:
                    leader.terminate()
                    leader.wait(timeout=5)
                    with pytest.raises(ProcessLookupError):
                        os.kill(leader.pid, 0)
                    with pytest.raises(
                        AssertionError, match="surviving worker process group"
                    ):
                        _assert_process_group_exited(leader.pid, timeout=0)
                finally:
                    member.terminate()
                    member.wait(timeout=5)
            _assert_process_group_exited(leader.pid)
        finally:
            if leader.poll() is None:
                leader.terminate()
                leader.wait(timeout=5)


@pytest.mark.skipif(
    os.environ.get("DASIMULATOR_TELEMETRY_DIAGNOSTICS") != "1",
    reason="opt-in only: enables telemetry and may trigger the native macOS abort",
)
@pytest.mark.parametrize(
    "mode", ["plain-import", "post-import-disable", "pre-import-disable"]
)
def test_onnx_telemetry_state_diagnostic(family_python, tmp_path, mode):  # noqa: F811
    home = tmp_path / "home"
    home.mkdir()
    cache = home / ".cache"
    environment = {
        **_environment(),
        "HOME": str(home),
        "XDG_CACHE_HOME": str(cache),
    }
    environment.pop("ORT_DISABLE_TELEMETRY", None)
    if mode == "pre-import-disable":
        environment["ORT_DISABLE_TELEMETRY"] = "1"
    # Never import an enabled uploader in pytest itself: static destruction can
    # SIGABRT on macOS. Isolate both the state and the potentially crashing import.
    command = [
        str(family_python.interpreter),
        "-c",
        "import importlib.util, sys\n"
        "if importlib.util.find_spec('onnxruntime') is None: sys.exit(77)\n"
        "import onnxruntime\n"
        + (
            "onnxruntime.disable_telemetry_events()\n"
            if mode == "post-import-disable"
            else ""
        )
        + "print(onnxruntime.__version__, flush=True)\n",
    ]
    completed = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode == 77:
        pytest.skip("onnxruntime is not installed in the target interpreter")
    state = _telemetry_state_directory(home, cache)
    evidence = {
        "lane": family_python.label,
        "mode": mode,
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "state_directory": str(state),
        "state_directory_exists": state.is_dir(),
        "deviceid_exists": (state / "deviceid").is_file(),
        "database_exists": (state / "onnxruntime.db").is_file(),
    }
    print(json.dumps(evidence, sort_keys=True))
    if mode == "pre-import-disable":
        assert not state.exists(), evidence
    else:
        assert (state / "deviceid").is_file(), evidence
        assert (state / "onnxruntime.db").is_file(), evidence
    assert completed.returncode == 0, evidence


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
        runner = SimulatorRunner(
            project,
            interview="main.yml",
            python=interpreter,
            offline=True,
            timeout=120,
        )
        # Test-only process isolation; do not change the production runner's
        # launch semantics just to observe its descendants at shutdown.
        with monkeypatch.context() as patch:
            patch.setattr(
                "docassemble_simulator.runner.subprocess.Popen",
                partial(subprocess.Popen, start_new_session=True),
            )
            runner.open()
        with runner:
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
            start_new_session=True,
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

    _assert_process_group_exited(pid)
    assert not _telemetry_state_directory(home, cache).exists()
