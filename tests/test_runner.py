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


def _scope(*, pid=1, interview="docassemble.example:data/questions/main.yml"):
    return {
        "root": "/project",
        "project_root": "/project",
        "interview": interview,
        "config_fingerprint": "",
        "session_root": "/sessions",
        "session": "/sessions/main.pkl",
        "session_id": "main",
        "pid": pid,
    }


def _worker_executable(tmp_path, body):
    executable = tmp_path / "controlled-worker"
    executable.write_text(
        f"#!{sys.executable}\nimport json, os, sys, time\n"
        "request = json.loads(sys.stdin.readline())\n" + body
    )
    executable.chmod(0o755)
    return executable


def _echo_worker_executable(tmp_path, response_id="request"):
    executable = tmp_path / "echo-worker"
    identifier = "request.get('id')" if response_id == "request" else repr(response_id)
    executable.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        "scope = " + repr(_scope()) + "\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    response = {'id': "
        + identifier
        + ", 'ok': True, 'result': {}, 'scope': scope}\n"
        "    print(json.dumps(response), flush=True)\n"
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


def test_runner_correlates_supplied_and_generated_journey_ids(tmp_path):
    executable = _echo_worker_executable(tmp_path)
    runner = SimulatorRunner(_project(tmp_path), python=executable, timeout=1)

    responses = runner.journey(
        [
            {"operation": "start"},
            {"id": "provided", "operation": "status"},
            {"id": 7, "operation": "status"},
            {"operation": "status"},
        ]
    )

    assert [response["id"] for response in responses] == ["1", "provided", 7, "4"]
    assert all(response.ok for response in responses)
    subsequent = runner.status()
    assert subsequent.ok
    assert subsequent["id"] == "5"
    runner.close()


def test_runner_rejects_invalid_supplied_request_ids(tmp_path):
    runner = SimulatorRunner(_project(tmp_path))

    for identifier in (None, True, 1.5, [], {}):
        with pytest.raises(ValueError, match="request id"):
            runner.request("start", id=identifier)

    runner.close()


def test_runner_invalidates_transport_for_mismatched_supplied_id(tmp_path):
    executable = _echo_worker_executable(tmp_path, response_id="wrong")
    runner = SimulatorRunner(_project(tmp_path), python=executable, timeout=1)

    response = runner.request("start", id="provided")

    assert response.error["kind"] == "fault"
    assert "does not match 'provided'" in response.error["details"]["error"]
    with pytest.raises(RunnerError, match="closed"):
        runner.status()


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


def test_runner_rejects_a_response_with_an_incomplete_scope(tmp_path):
    executable = _worker_executable(
        tmp_path,
        'print(json.dumps({"id": "1", "ok": True, "result": {}, "scope": {}}))\n',
    )
    runner = SimulatorRunner(_project(tmp_path), python=executable, timeout=1)

    response = runner.execute("secret = 1")

    assert response.error["kind"] == "fault"
    assert "scope" in response.error["details"]["error"]
    with pytest.raises(RunnerError, match="closed"):
        runner.status()


def test_runner_rejects_a_changed_scope_after_the_first_response(tmp_path):
    first = json.dumps({"id": "1", "ok": True, "result": {}, "scope": _scope()})
    second = json.dumps({"id": "2", "ok": True, "result": {}, "scope": _scope(pid=2)})
    executable = _worker_executable(
        tmp_path,
        f"print({first!r})\n"
        "sys.stdout.flush()\n"
        "json.loads(sys.stdin.readline())\n"
        f"print({second!r})\n",
    )
    runner = SimulatorRunner(_project(tmp_path), python=executable, timeout=1)

    assert runner.execute("secret = 1").ok
    response = runner.status()

    assert response.error["kind"] == "fault"
    assert "scope changed" in response.error["details"]["error"]
    with pytest.raises(RunnerError, match="closed"):
        runner.status()


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


@pytest.mark.parametrize("source_policy", ["copy", "symlink"])
def test_runner_hands_worker_the_original_project_for_config_discovery(
    tmp_path, source_policy
):
    project = _project(tmp_path / "original")
    (tmp_path / "simulator.toml").write_text('timezone = "UTC"\n', encoding="utf-8")
    (project / "simulator.toml").write_text(
        'timezone = "America/Chicago"\n', encoding="utf-8"
    )
    marker = tmp_path / f"{source_policy}-argv.json"
    executable = tmp_path / f"{source_policy}-worker"
    executable.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"open({str(marker)!r}, 'w', encoding='utf-8').write(json.dumps(sys.argv))\n"
        "request = json.loads(sys.stdin.readline())\n"
        f"print(json.dumps({{'id': request.get('id'), 'ok': True, 'result': {{}}, 'scope': {_scope()!r}}}), flush=True)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    with SimulatorRunner(
        project,
        config={"timezone": "America/Los_Angeles"},
        source_policy=source_policy,
        python=executable,
    ) as runner:
        response = runner.request("ping")
        arguments = json.loads(marker.read_text(encoding="utf-8"))
        assert response.ok
        assert runner.project_root == project.resolve()
        assert "--project-root" in arguments
        assert arguments[arguments.index("--project-root") + 1] == str(
            project.resolve()
        )
        assert "--config-layer" in arguments
        generated = Path(arguments[arguments.index("--config-layer") + 1])
        assert generated.is_file()
        assert "America/Los_Angeles" in generated.read_text(encoding="utf-8")


def test_runner_shared_policy_does_not_copy_source(tmp_path):
    project = _project(tmp_path)
    runner = SimulatorRunner(project, isolate=False)

    assert runner.root == project.resolve()
    runner.close()


@pytest.mark.parametrize("value", [None, "0", "1"])
def test_runner_launch_disables_onnx_telemetry_before_child_imports(
    tmp_path, monkeypatch, value
):
    if value is None:
        monkeypatch.delenv("ORT_DISABLE_TELEMETRY", raising=False)
    else:
        monkeypatch.setenv("ORT_DISABLE_TELEMETRY", value)
    executable = _worker_executable(
        tmp_path,
        "response = {'id': request['id'], 'ok': True, 'scope': "
        + repr(_scope())
        + ", 'result': {'telemetry': os.environ.get('ORT_DISABLE_TELEMETRY'), "
        "'unbuffered': os.environ.get('PYTHONUNBUFFERED')}}\n"
        "print(json.dumps(response), flush=True)\n",
    )

    with SimulatorRunner(_project(tmp_path), python=executable) as runner:
        response = runner.request("ping")

    assert response.ok
    assert response.result == {"telemetry": "1", "unbuffered": "1"}
