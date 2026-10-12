"""Release sequencing tests using command substitutes instead of GitHub or Git."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
RELEASE = PROJECT / "scripts" / "release"


_COMMAND = r"""#!/bin/sh
set -eu
name=$(basename "$0")
printf '%s %s\n' "$name" "$*" >> "$RELEASE_LOG"
case "$name" in
  mise)
    case "${FAKE_MODE:-success}" in
      fail)
        exit 23
        ;;
      interrupt)
        trap 'printf "mise-terminated\n" >> "$RELEASE_LOG"; exit 143' INT TERM
        echo "controlled gate output"
        sleep 60 &
        printf 'gate-descendant %s\n' "$!" >> "$RELEASE_LOG"
        while :; do sleep 0.1; done
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


def _fake_path(tmp_path: Path, mode: str) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for command in ("mise", "bump-my-version", "git", "gh"):
        path = bin_dir / command
        path.write_text(_COMMAND)
        path.chmod(0o755)
    log = tmp_path / "commands.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RELEASE_LOG": str(log),
        "FAKE_MODE": mode,
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


def test_interrupted_validation_terminates_gate_without_mutation(tmp_path: Path):
    env, log = _fake_path(tmp_path, "interrupt")
    process = subprocess.Popen(
        [str(RELEASE)],
        cwd=PROJECT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if log.exists() and "mise run test:runtime" in log.read_text():
                break
            time.sleep(0.02)
        else:
            pytest.fail("release did not start the runtime gate")

        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    commands = log.read_text().splitlines()
    assert process.returncode == 130
    assert "mise-terminated" in commands
    assert not any(line.startswith("bump-my-version") for line in commands)
    assert "no release changes were made" in stderr
    assert "controlled gate output" in stdout

    # The gate's own descendants (pytest workers, target interpreters) must be
    # terminated too, not just the mise process the script signalled.
    descendant = _gate_descendant(commands)
    assert descendant is not None, commands
    try:
        assert _process_gone(descendant, time.monotonic() + 5), (
            f"gate descendant {descendant} survived the interrupt"
        )
    finally:
        try:
            os.kill(descendant, signal.SIGKILL)
        except ProcessLookupError:
            pass
