"""Verify everyday task selection independently of installed runtimes/fixtures."""

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("task", ["test", "test:fast"])
@pytest.mark.parametrize("equipped", [False, True], ids=["clean", "runtime-equipped"])
def test_everyday_commands_select_only_fast_tests(tmp_path, task, equipped):
    config = tomllib.loads((PROJECT / ".mise.toml").read_text())
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "selection-probe"\nversion = "0.0.0"\n'
        "[tool.pytest.ini_options]\n"
        'markers = ["real_runtime: runtime probe", "corpus: corpus probe"]\n'
    )
    (tmp_path / "test_selection.py").write_text(
        """import os
import pytest


def test_fast():
    pass


@pytest.mark.real_runtime
@pytest.mark.skipif(os.environ['PROBE_EQUIPPED'] != 'yes', reason='no runtime')
def test_runtime():
    pytest.fail('everyday commands must not run real-runtime tests')


@pytest.mark.corpus
@pytest.mark.skipif(os.environ['PROBE_EQUIPPED'] != 'yes', reason='no fixtures')
def test_corpus():
    pytest.fail('everyday commands must not run corpus tests')


@pytest.mark.real_runtime
@pytest.mark.corpus
@pytest.mark.skipif(os.environ['PROBE_EQUIPPED'] != 'yes', reason='no runtime/fixtures')
def test_runtime_corpus():
    pytest.fail('everyday commands must not run tests marked with both slow lanes')
"""
    )
    env = {
        **os.environ,
        # Reuse pytest's interpreter, but do not resolve/install a project or
        # inherit plugins/options that can affect selection in the child suite.
        "UV_PROJECT_ENVIRONMENT": sys.prefix,
        "UV_NO_SYNC": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_ADDOPTS": "",
        "PROBE_EQUIPPED": "yes" if equipped else "no",
    }
    result = subprocess.run(
        config["tasks"][task]["run"],
        shell=True,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed, 3 deselected" in result.stdout, result.stdout


def test_commit_hook_reaches_only_the_fast_test_lane():
    tasks = tomllib.loads((PROJECT / ".mise.toml").read_text())["tasks"]
    pending = ["pre-commit"]
    reached = set()
    pytest_commands = []
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        task = tasks[name]
        pending.extend(task.get("depends", []))
        pending.extend(task.get("depends_post", []))
        runs = task["run"]
        for run in [runs] if isinstance(runs, str) else runs:
            if isinstance(run, dict):
                pending.extend(run["tasks"])
            elif "pytest" in run:
                pytest_commands.append(run)

    assert "test:fast" in reached
    assert reached.isdisjoint({"test:runtime", "test:corpus", "test:all-da"})
    assert pytest_commands == [tasks["test:fast"]["run"]]
