"""Exercise the distributed commands in an isolated project interpreter."""

import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def installed_scripts(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("installed-cli")
    dist = workspace / "dist"
    project = Path(__file__).resolve().parents[1]
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist), str(project)],
        check=True,
        capture_output=True,
        text=True,
    )
    environment = workspace / "venv"
    subprocess.run(["uv", "venv", str(environment)], check=True, capture_output=True)
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            str(next(dist.glob("*.whl"))),
        ],
        check=True,
        capture_output=True,
    )
    return scripts


def invoke(scripts, command, *args):
    suffix = ".exe" if os.name == "nt" else ""
    return subprocess.run(
        [str(scripts / (command + suffix)), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_installed_alias_help_needs_no_runtime(installed_scripts):
    for command in ("sim", "docassemble-simulator"):
        result = invoke(installed_scripts, command, "--help")
        assert result.returncode == 0, result.stderr
        assert "--offline" in result.stdout
        assert "start" in result.stdout
    python = "python.exe" if os.name == "nt" else "python"
    probe = subprocess.run(
        [str(installed_scripts / python), "-c", "import docassemble.base"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode != 0
    assert "ModuleNotFoundError" in probe.stderr


def test_installed_alias_preserves_usage_errors(installed_scripts):
    results = [
        invoke(installed_scripts, command, "--json", "not-a-command")
        for command in ("sim", "docassemble-simulator")
    ]
    assert [result.returncode for result in results] == [1, 1]
    payloads = [json.loads(result.stdout) for result in results]
    assert payloads[0] == payloads[1]
    assert payloads[0]["ok"] is False
