"""Shared fixtures: a minimal in-memory stub of the docassemble.base modules.

Only what the execution context and validation path touch at call time. Pure
helpers (parse_value, field_visible, deep_merge, detect.*) need no stubs.
"""
from __future__ import annotations

import sys
import types
from contextlib import contextmanager

import pytest


class _StubThisThread:
    current_info = None
    interview = None
    interview_status = None
    internal = None


class _StubInterviewStatus:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class DAValidationError(Exception):
    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field


@contextmanager
def _global_context(globals_dict):
    yield


@contextmanager
def _user_dict_context(user_dict):
    yield


def _empty_globals():
    return {"__builtins__": __builtins__}


@pytest.fixture
def da_stubs(monkeypatch):
    """Install fake docassemble.base.* modules for the duration of a test."""
    da = types.ModuleType("docassemble")
    base = types.ModuleType("docassemble.base")
    functions = types.ModuleType("docassemble.base.functions")
    parse = types.ModuleType("docassemble.base.parse")
    thread_context = types.ModuleType("docassemble.base.thread_context")
    error = types.ModuleType("docassemble.base.error")

    functions.this_thread = _StubThisThread()
    parse.InterviewStatus = _StubInterviewStatus
    thread_context.empty_globals = _empty_globals
    thread_context.global_context = _global_context
    thread_context.user_dict_context = _user_dict_context
    error.DAValidationError = DAValidationError

    base.functions = functions
    base.parse = parse
    base.thread_context = thread_context
    base.error = error
    da.base = base

    for name, mod in [
        ("docassemble", da),
        ("docassemble.base", base),
        ("docassemble.base.functions", functions),
        ("docassemble.base.parse", parse),
        ("docassemble.base.thread_context", thread_context),
        ("docassemble.base.error", error),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)

    return types.SimpleNamespace(DAValidationError=DAValidationError)
