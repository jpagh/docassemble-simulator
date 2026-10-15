"""Release sequencing tests using command substitutes instead of GitHub or Git."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
RELEASE = PROJECT / "scripts" / "release"

# Allow shell trap dispatch and descendant reaping to lag release exit on both
# macOS and Linux. Each startup, pipe, and process wait is bounded separately.
_GATE_TIMEOUT = 5
_READY = "mise run test:runtime"


_COMMAND = r"""#!/bin/sh
set -eu
name=$(basename "$0")
if [ "$name" != mise ] || [ "${FAKE_MODE:-success}" != interrupt ]; then
  printf '%s %s\n' "$name" "$*" >> "$RELEASE_LOG"
fi
case "$name" in
  mise)
    case "${FAKE_MODE:-success}" in
      fail)
        exit 23
        ;;
      interrupt)
        exec "$RELEASE_TEST_PYTHON" "$RELEASE_TEST_GATE" "$@"
        ;;
      *)
        echo "controlled gate output"
        ;;
    esac
    ;;
  git)
    case "${1:-}" in
      describe)
        if [ "${4:-}" = "v26.9.2^" ]; then
          printf 'v26.9.1\n'
        else
          printf 'v26.9.2\n'
        fi
        ;;
      log)
        printf '%s\n' '- controlled release change'
        ;;
    esac
    ;;
  gh)
    if [ "${1:-}" = repo ]; then
      printf 'example/repository\n'
    fi
    ;;
esac
"""


_INTERRUPT_GATE = """\
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

log = Path(os.environ["RELEASE_LOG"])
startup = os.environ.get("RELEASE_TEST_GATE_STARTUP")
if startup:
    # Separate from the release log: cleanup must work without any logged PID.
    pending = Path(startup + ".pending")
    pending.write_text(str(os.getpid()))
    if os.environ.get("RELEASE_TEST_GATE_DELAY_MARKER"):
        time.sleep(60)
    pending.replace(startup)
    time.sleep(60)


def record(line):
    with log.open("a") as stream:
        stream.write(line + "\\n")


def terminated(signum, frame):
    record("mise-terminated")
    raise SystemExit(143)


record(f"gate-process {os.getpid()}")
signal.signal(signal.SIGINT, terminated)
signal.signal(signal.SIGTERM, terminated)
print("controlled gate output", flush=True)
# Popen returns only after exec: unlike shell `sleep 60 &`, the child cannot
# still be resetting inherited signal handlers when readiness is observed.
descendant = subprocess.Popen(["sleep", "60"])
record(f"gate-descendant {descendant.pid}")
record("mise " + " ".join(sys.argv[1:]))
descendant.wait(timeout=65)
"""


def _fake_path(tmp_path: Path, mode: str) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for command in ("mise", "bump-my-version", "git", "gh"):
        path = bin_dir / command
        path.write_text(_COMMAND)
        path.chmod(0o755)
    gate = bin_dir / "interrupt-gate.py"
    gate.write_text(_INTERRUPT_GATE)
    log = tmp_path / "commands.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RELEASE_LOG": str(log),
        "FAKE_MODE": mode,
        "RELEASE_TEST_PYTHON": sys.executable,
        "RELEASE_TEST_GATE": str(gate),
    }
    return env, log


def _run(
    tmp_path: Path, mode: str = "success"
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    env, log = _fake_path(tmp_path, mode)
    result = subprocess.run(
        [str(RELEASE)],
        cwd=PROJECT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return result, log.read_text().splitlines()


def _gate_descendant(commands: list[str]) -> int | None:
    for line in commands:
        if line.startswith("gate-descendant "):
            return int(line.split()[1])
    return None


def _process_gone(pid: int, deadline: float) -> bool:
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def _cleanup_interrupted_validation(
    process: subprocess.Popen[str],
    read_commands: Callable[[], list[str]],
    *,
    startup: Path | None = None,
):
    # Preserve the original failure even if an orphan still holds a pipe open.
    failed = sys.exc_info()[0] is not None
    try:
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=_GATE_TIMEOUT)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=_GATE_TIMEOUT)
        finally:
            try:
                # Re-read after release's trap: a gate may have published its
                # PID or startup marker only while graceful cleanup ran.
                commands = read_commands()
                if startup is not None and startup.exists():
                    commands.append(f"gate-process {startup.read_text()}")
                for line in commands:
                    name, _, value = line.partition(" ")
                    try:
                        if name == "gate-process":
                            os.killpg(int(value), signal.SIGKILL)
                        elif name == "gate-descendant":
                            os.kill(int(value), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.communicate(timeout=_GATE_TIMEOUT)
            finally:
                try:
                    if process.stdout is not None and not process.stdout.closed:
                        process.stdout.close()
                finally:
                    if process.stderr is not None and not process.stderr.closed:
                        process.stderr.close()
    except Exception:
        if not failed:
            raise


def test_release_validates_before_any_release_mutation(tmp_path: Path):
    result, commands = _run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    names = [line.split(maxsplit=1)[0] for line in commands]
    assert names.index("mise") < names.index("bump-my-version")
    assert names.index("bump-my-version") < names.index("git")
    assert names.index("git") < names.index("gh")
    assert commands[0] == "mise run test:runtime"
    assert "controlled gate output" in result.stdout
    assert "runtime compatibility gate" in result.stdout


def test_failed_validation_prevents_bump_and_publication(tmp_path: Path):
    result, commands = _run(tmp_path, "fail")

    assert result.returncode == 23
    assert commands == ["mise run test:runtime"]
    assert "release files and Git state were not changed" in result.stderr


def _check_interrupted_validation(
    tmp_path: Path, *, immediate: bool = False, readiness_timeout: float = _GATE_TIMEOUT
):
    env, log = _fake_path(tmp_path, "interrupt")
    log_fd = None
    recorded = bytearray()
    if immediate:
        # A FIFO wakes the reader on the readiness write itself, without a
        # polling grace period in which a prematurely ready gate could start.
        os.mkfifo(log)
        log_fd = os.open(log, os.O_RDWR | os.O_NONBLOCK)

    def read_commands() -> list[str]:
        if log_fd is None:
            return log.read_text().splitlines() if log.exists() else []
        while select.select([log_fd], [], [], 0)[0]:
            recorded.extend(os.read(log_fd, 4096))
        return recorded.decode().splitlines()

    process = subprocess.Popen(
        [str(RELEASE)],
        cwd=PROJECT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + readiness_timeout
        while time.monotonic() < deadline:
            commands = read_commands()
            if _READY in commands:
                break
            if log_fd is None:
                time.sleep(0.02)
            else:
                select.select([log_fd], [], [], max(0, deadline - time.monotonic()))
        else:
            pytest.fail("release did not establish the runtime gate")

        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=_GATE_TIMEOUT)

        commands = read_commands()
        # Record order is deterministic even if the reader was descheduled:
        # restoring the old early readiness write must fail this assertion.
        descendant = _gate_descendant(commands)
        assert descendant is not None, commands
        assert commands.index(f"gate-descendant {descendant}") < commands.index(_READY)
        assert process.returncode == 130
        assert "mise-terminated" in commands
        assert not any(line.startswith("bump-my-version") for line in commands)
        assert "no release changes were made" in stderr
        assert "controlled gate output" in stdout

        # The gate's own descendants (pytest workers, target interpreters) must
        # be terminated too, not just the mise process the script signalled.
        assert _process_gone(descendant, time.monotonic() + _GATE_TIMEOUT), (
            f"gate descendant {descendant} survived the interrupt"
        )
    finally:
        try:
            _cleanup_interrupted_validation(
                process,
                read_commands,
                startup=Path(env["RELEASE_TEST_GATE_STARTUP"])
                if env.get("RELEASE_TEST_GATE_STARTUP")
                else None,
            )
        finally:
            if log_fd is not None:
                os.close(log_fd)


def test_interrupted_validation_terminates_gate_without_mutation(tmp_path: Path):
    _check_interrupted_validation(tmp_path)


def test_interrupted_validation_immediately_after_readiness(tmp_path: Path):
    _check_interrupted_validation(tmp_path, immediate=True)


def test_interrupted_validation_failure_cleans_up_gate(tmp_path: Path, monkeypatch):
    send_signal = subprocess.Popen.send_signal
    interrupted = []

    def fail_after_readiness(process, sig):
        if sig == signal.SIGINT:
            interrupted.append(process)
            raise RuntimeError("injected failure after readiness")
        return send_signal(process, sig)

    monkeypatch.setattr(subprocess.Popen, "send_signal", fail_after_readiness)
    with pytest.raises(RuntimeError, match="injected failure after readiness"):
        _check_interrupted_validation(tmp_path)

    commands = (tmp_path / "commands.log").read_text().splitlines()
    descendant = _gate_descendant(commands)
    assert descendant is not None
    assert _process_gone(descendant, time.monotonic() + _GATE_TIMEOUT)
    gate = int(
        next(line.split()[1] for line in commands if line.startswith("gate-process "))
    )
    assert _process_gone(gate, time.monotonic() + _GATE_TIMEOUT)
    with pytest.raises(ProcessLookupError):
        os.killpg(gate, 0)
    assert len(interrupted) == 1
    assert interrupted[0].poll() is not None
    assert interrupted[0].returncode == 143  # release's SIGTERM trap ran
    assert interrupted[0].stdout.closed
    assert interrupted[0].stderr.closed


@pytest.mark.parametrize("wrapper_timeout", [False, True])
def test_interrupted_validation_startup_timeout_cleans_up_unlogged_gate(
    tmp_path: Path, monkeypatch, wrapper_timeout: bool
):
    env, log = _fake_path(tmp_path, "interrupt")
    startup = tmp_path / "gate-startup"
    env["RELEASE_TEST_GATE_STARTUP"] = str(startup)
    pending = startup.with_suffix(".pending")
    if wrapper_timeout:
        env["RELEASE_TEST_GATE_DELAY_MARKER"] = "1"
    monkeypatch.setattr(sys.modules[__name__], "_fake_path", lambda *_: (env, log))
    popen = subprocess.Popen
    started = []

    def start_before_readiness_deadline(*args, **kwargs):
        process = popen(*args, **kwargs)
        started.append(process)
        # Wait for an established gate, not scheduling or interpreter startup.
        # In the wrapper-timeout case the marker genuinely does not exist:
        # only release's trap can find the unlogged gate during cleanup.
        established = pending if wrapper_timeout else startup
        deadline = time.monotonic() + _GATE_TIMEOUT
        while time.monotonic() < deadline:
            if established.exists() and established.read_text():
                break
            time.sleep(0.01)
        else:
            pytest.fail("gate did not start")
        assert startup.exists(), "gate did not enter its startup delay"
        return process

    monkeypatch.setattr(subprocess, "Popen", start_before_readiness_deadline)

    def read_commands() -> list[str]:
        return log.read_text().splitlines() if log.exists() else []

    try:
        if wrapper_timeout:
            with pytest.raises(AssertionError, match="gate did not enter"):
                _check_interrupted_validation(tmp_path, readiness_timeout=0.05)
            assert not startup.exists()
            assert not any(line.startswith("gate-process ") for line in read_commands())
        else:
            with pytest.raises(pytest.fail.Exception, match="did not establish"):
                _check_interrupted_validation(tmp_path, readiness_timeout=0.05)
    finally:
        for process in started:
            _cleanup_interrupted_validation(process, read_commands, startup=startup)

    commands = read_commands()
    assert not any(line.startswith("gate-process ") for line in commands)
    assert _gate_descendant(commands) is None
    gate = int((pending if wrapper_timeout else startup).read_text())
    assert _process_gone(gate, time.monotonic() + _GATE_TIMEOUT)
    # An empty process group also rules out any surviving sleep child.
    with pytest.raises(ProcessLookupError):
        os.killpg(gate, 0)
    assert len(started) == 1
    assert started[0].returncode == 143  # release's SIGTERM trap ran
    assert started[0].stdout.closed
    assert started[0].stderr.closed
