"""Workspace-relative ``run_python_module`` execution with real runtimes (#87).

The upstream function derives its subprocess interpreter from
``docassemble.base.pandoc.__file__``.  A simulator workspace puts that module
under a staged package tree, so the derivation tries to execute ``pandoc.py``
itself.  These tests prove the pre-fix failure and the simulator's interpreter
binding for every supported module form.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from test_real_runtime import (  # noqa: F401 - fixtures registered by import
    _environment,
    _run,
    family_python,
    real_python,
    real_python_19,
)

from docassemble_simulator.demo_corpus import discover_runtime_packages

pytestmark = pytest.mark.real_runtime

INTERVIEW = """\
---
mandatory: True
code: |
  filename_output, filename_code = run_python_module('hello.py')
  dotted_output, dotted_code = run_python_module('docassemble.issue87.hello')
  relative_output, relative_code = run_python_module('.hello')
  arguments_output, arguments_code = run_python_module('tests.py', ['-v', '--fast'])
  failure_output, failure_code = run_python_module('failing.py')
  try:
      run_python_module('hello.py', 'not-a-list')
  except Exception as error:
      argument_error = type(error).__name__ + ': ' + str(error)
  form_codes = f'{filename_code}|{dotted_code}|{relative_code}|{arguments_code}|{failure_code}'
---
mandatory: True
question: |
  ${ form_codes }
subquestion: |
  ${ filename_output }${ dotted_output }${ relative_output }${ arguments_output }
  FAIL: ${ failure_output }
  ARG: ${ argument_error }
"""

PRE_FIX_SCRIPT = """\
from docassemble_simulator._runtime import (
    install_fake_redis,
    prepare_environment,
    runtime_context,
)

install_fake_redis()
prepare_environment()
with runtime_context({'seed': True}):
    from docassemble.base.functions import this_thread

    this_thread.current_package = 'docassemble.issue87'
    from docassemble.base.util import run_python_module

    run_python_module('hello.py')
"""


def _workspace(tmp_path: Path, interpreter: Path) -> Path:
    """A staged package tree whose ``docassemble.base`` distorts derivation."""
    packages = discover_runtime_packages(interpreter)
    workspace = tmp_path / "workspace"
    da_root = workspace / "docassemble"
    da_root.mkdir(parents=True)
    (da_root / "base").symlink_to(packages["base"].path, target_is_directory=True)
    package = da_root / "issue87"
    questions = package / "data" / "questions"
    questions.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "hello.py").write_text('print("hello from workspace")\n')
    (package / "tests.py").write_text(
        "import sys\nprint('args:' + ' '.join(sys.argv[1:]))\n"
    )
    (package / "failing.py").write_text(
        "import sys\n"
        "print('failing stdout', flush=True)\n"
        "print('failing stderr', file=sys.stderr, flush=True)\n"
        "raise SystemExit(7)\n"
    )
    (questions / "main.yml").write_text(INTERVIEW)
    return workspace


def test_upstream_derivation_fails_for_a_workspace_package_tree(
    real_python,  # noqa: F811 - imported fixture
    tmp_path,
):
    workspace = _workspace(tmp_path, real_python)

    completed = subprocess.run(
        [str(real_python), "-c", PRE_FIX_SCRIPT],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
        env=_environment(),
        timeout=120,
    )

    assert completed.returncode != 0, completed.stdout + completed.stderr
    assert "PermissionError" in completed.stderr, completed.stderr
    assert "docassemble/base/pandoc.py" in completed.stderr, completed.stderr


def test_simulator_runner_reaches_a_question_through_every_module_form(
    family_python,  # noqa: F811 - imported fixture
    tmp_path,
):
    workspace = _workspace(tmp_path, family_python.interpreter)

    started = _run(family_python.interpreter, workspace, "start")

    assert started["ok"], started
    assert started["result"]["kind"] == "question", started
    assert started["result"]["question_text"].strip() == "0|0|0|0|7"
    subquestion = started["result"]["subquestion_text"]
    assert subquestion.count("hello from workspace") == 3, subquestion
    assert "args:-v --fast" in subquestion, subquestion
    assert "failing stdout" in subquestion, subquestion
    assert "failing stderr" in subquestion, subquestion
    assert (
        "DAError: run_python_module: the arguments parameter must be in the "
        "form of a list"
    ) in subquestion, subquestion
