from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from docassemble_simulator.runner import RunnerError, SimulatorRunner


def _project(tmp_path: Path) -> Path:
    package = tmp_path / "docassemble" / "example"
    (package / "data" / "questions").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "data" / "questions" / "main.yml").write_text("---\n", encoding="utf-8")
    return tmp_path


def test_runner_discovers_project_and_copies_source_for_isolation(tmp_path):
    project = _project(tmp_path)
    runner = SimulatorRunner(project, config={"simulator": {"offline": True}})

    assert runner.project_root == project.resolve()
    isolated = runner.root
    assert isolated != runner.project_root
    assert (
        isolated / "docassemble" / "example" / "data" / "questions" / "main.yml"
    ).is_file()
    assert runner.session_root != project / ".simulator" / "sessions"

    runner.close()
    assert not isolated.exists()
    assert (
        project / "docassemble" / "example" / "data" / "questions" / "main.yml"
    ).is_file()


def _worker_executable(tmp_path, body):
    executable = tmp_path / "controlled-worker"
    executable.write_text(
        f"#!{sys.executable}\nimport json, os, sys, time\n"
        "request = json.loads(sys.stdin.readline())\n" + body
    )
    executable.chmod(0o755)
    return executable


@pytest.mark.parametrize(
    "body",
    [
        "sys.stdout.write('{\"id\":'); sys.stdout.flush(); time.sleep(1)\n",
        "time.sleep(1)\n",
    ],
)
def test_runner_deadline_covers_a_complete_response(tmp_path, body):
    executable = _worker_executable(tmp_path, body)
    with SimulatorRunner(_project(tmp_path), python=executable, timeout=0.15) as runner:
        started = time.monotonic()
        response = runner.execute("secret = 1")
        assert time.monotonic() - started < 0.8
        assert response["id"] == "1"
        assert response.error["kind"] == "timeout"
        assert response.error["details"]["operation"] == "exec"
        assert "secret" not in json.dumps(response)
        with pytest.raises(RunnerError, match="closed"):
            runner.status()


def test_runner_closes_unusable_transport_after_unexpected_response(tmp_path):
    executable = _worker_executable(
        tmp_path,
        'sys.stdout.write(json.dumps({"id": "other", "ok": True, "result": {}}) + "\\n")\n',
    )
    runner = SimulatorRunner(_project(tmp_path), python=executable, timeout=0.5)
    response = runner.execute("secret = 1")

    assert response.error["kind"] == "fault"
    assert response.error["details"]["operation"] == "exec"
    with pytest.raises(RunnerError, match="closed"):
        runner.status()
    runner.close()


@pytest.mark.parametrize(
    "body",
    [
        'print(json.dumps({"id": "wrong", "ok": True, "result": {}, "scope": {}}))\n',
        'print(json.dumps({"id": "1", "result": {}, "scope": {}}))\n',
        'print(json.dumps({"id": "1", "ok": True, "error": {}, "scope": {}}))\n',
    ],
)
def test_runner_closes_unusable_response_transports(tmp_path, body):
    executable = _worker_executable(tmp_path, body)
    runner = SimulatorRunner(_project(tmp_path), python=executable, timeout=1)

    response = runner.execute("secret = 1")

    assert response["id"] == "1"
    assert response.error["kind"] == "fault"
    with pytest.raises(RunnerError, match="closed"):
        runner.status()


def test_runner_reports_a_crashed_worker_and_closes_it(tmp_path):
    executable = _worker_executable(tmp_path, "os._exit(7)\n")
    runner = SimulatorRunner(_project(tmp_path), python=executable, timeout=1)

    response = runner.execute("secret = 1")

    assert response["id"] == "1"
    assert response.error["kind"] == "fault"
    assert response.error["details"]["operation"] == "exec"
    with pytest.raises(RunnerError, match="closed"):
        runner.status()
    runner.close()


def test_runner_rejects_unsolicited_data_after_a_response(tmp_path):
    executable = _worker_executable(
        tmp_path,
        'sys.stdout.write(json.dumps({"id": "1", "ok": True, "result": {}, "scope": {}}) + "\\nnoise\\n")\n'
        "sys.stdout.flush()\n",
    )
    runner = SimulatorRunner(_project(tmp_path), python=executable, timeout=1)

    response = runner.execute("secret = 1")

    assert response.error["kind"] == "fault"
    assert "unsolicited response data" in response.error["message"]
    with pytest.raises(RunnerError, match="closed"):
        runner.status()


def test_runner_shared_policy_does_not_copy_source(tmp_path):
    project = _project(tmp_path)
    runner = SimulatorRunner(project, isolate=False)

    assert runner.root == project.resolve()
    runner.close()
