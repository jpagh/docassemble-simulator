"""Interpreter binding for docassemble's ``run_python_module`` seam (#87)."""

from __future__ import annotations

import subprocess
import sys
import types

import pytest

from docassemble_simulator import _runtime as runtime_module
from docassemble_simulator._runtime import (
    SimulatorRuntime,
    install_python_module_runner,
)


class _DAError(Exception):
    pass


def _install_stub_runtime(monkeypatch):
    da = types.ModuleType("docassemble")
    da.__path__ = []
    base = types.ModuleType("docassemble.base")
    base.__path__ = []
    functions = types.ModuleType("docassemble.base.functions")
    functions.this_thread = types.SimpleNamespace(current_package="docassemble.demo")
    error = types.ModuleType("docassemble.base.error")
    error.DAError = _DAError
    util = types.ModuleType("docassemble.base.util")

    def upstream_run_python_module(module, arguments=None):
        raise AssertionError("the upstream interpreter derivation must not run")

    util.run_python_module = upstream_run_python_module
    legal = types.ModuleType("docassemble.base.legal")
    legal.run_python_module = upstream_run_python_module
    da.base = base
    base.functions = functions
    base.error = error
    base.util = util
    base.legal = legal
    for name, module in (
        ("docassemble", da),
        ("docassemble.base", base),
        ("docassemble.base.functions", functions),
        ("docassemble.base.error", error),
        ("docassemble.base.util", util),
        ("docassemble.base.legal", legal),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return util, legal, upstream_run_python_module


def test_runner_uses_the_active_interpreter_for_every_module_form(monkeypatch):
    util, legal, _ = _install_stub_runtime(monkeypatch)
    calls = []

    def fake_check_output(command, **kwargs):
        calls.append((list(command), kwargs))
        return b"combined output\n"

    monkeypatch.setattr(runtime_module.subprocess, "check_output", fake_check_output)
    install_python_module_runner()

    assert util.run_python_module("docassemble.demo.hello") == (
        "combined output\n",
        0,
    )
    assert util.run_python_module("hello.py", ["-v"]) == ("combined output\n", 0)
    assert util.run_python_module(".hello") == ("combined output\n", 0)

    assert calls == [
        (
            [sys.executable, "-m", "docassemble.demo.hello"],
            {"stderr": subprocess.STDOUT, "cwd": None},
        ),
        (
            [sys.executable, "-m", "docassemble.demo.hello", "-v"],
            {"stderr": subprocess.STDOUT, "cwd": None},
        ),
        (
            [sys.executable, "-m", "docassemble.demo.hello"],
            {"stderr": subprocess.STDOUT, "cwd": None},
        ),
    ]
    assert legal.run_python_module is util.run_python_module


def test_runner_preserves_argument_validation_before_execution(monkeypatch):
    util, _, _ = _install_stub_runtime(monkeypatch)
    calls = []
    monkeypatch.setattr(
        runtime_module.subprocess,
        "check_output",
        lambda *args, **kwargs: calls.append(args) or b"",
    )
    install_python_module_runner()

    with pytest.raises(
        _DAError,
        match="the arguments parameter must be in the form of a list",
    ):
        util.run_python_module("hello.py", "not-a-list")

    assert calls == []


def test_runner_returns_combined_output_and_the_child_return_code(monkeypatch):
    util, _, _ = _install_stub_runtime(monkeypatch)

    def fake_check_output(command, **kwargs):
        raise subprocess.CalledProcessError(4, command, output=b"stdout\nstderr\n")

    monkeypatch.setattr(runtime_module.subprocess, "check_output", fake_check_output)
    install_python_module_runner()

    assert util.run_python_module("hello.py") == ("stdout\nstderr\n", 4)


def test_runner_executes_from_the_active_workspace_root(monkeypatch, tmp_path):
    util, _, _ = _install_stub_runtime(monkeypatch)
    calls = []

    def fake_check_output(command, **kwargs):
        calls.append((list(command), kwargs))
        return b""

    monkeypatch.setattr(runtime_module.subprocess, "check_output", fake_check_output)
    install_python_module_runner()

    with SimulatorRuntime().activate(tmp_path):
        assert util.run_python_module("hello.py") == ("", 0)

    assert calls == [
        (
            [sys.executable, "-m", "docassemble.demo.hello"],
            {"stderr": subprocess.STDOUT, "cwd": tmp_path.resolve()},
        )
    ]


def test_runner_installation_is_idempotent(monkeypatch):
    util, legal, _ = _install_stub_runtime(monkeypatch)

    install_python_module_runner()
    patched = util.run_python_module
    install_python_module_runner()

    assert util.run_python_module is patched
    assert legal.run_python_module is patched
