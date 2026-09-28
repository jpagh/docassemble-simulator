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


class _StubObjectList:
    """A picklable stand-in for docassemble's ``DAList``.

    Carries the surface the answer path uses: ``elements``, ``append``,
    ``remove``, ``clear`` and ``gathered``.
    """

    def __init__(self, instanceName=None, auto_gather=False, gathered=False):
        self.instanceName = instanceName
        self.elements = []
        self.auto_gather = auto_gather
        self.gathered = gathered

    def append(self, item):
        self.elements.append(item)

    def remove(self, item):
        self.elements.remove(item)

    def clear(self):
        self.elements.clear()

    def __len__(self):
        return len(self.elements)


def _stub_ensure_object_exists(saveas, datatype, user_dict, commands=None):
    """Mirror the part of ``docassemble.base.parse.ensure_object_exists`` the
    answer path relies on: leave an existing target alone, otherwise define a
    fresh object group in the namespace."""
    try:
        eval(saveas, user_dict)
    except Exception:  # noqa: BLE001 - an undefined target is the case we handle
        user_dict["__stub_object"] = _StubObjectList(instanceName=saveas)
        try:
            exec(  # noqa: S102 - the target is a variable name, not a literal
                f"{saveas} = __stub_object", user_dict
            )
        finally:
            user_dict.pop("__stub_object", None)


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
    parse.ensure_object_exists = _stub_ensure_object_exists
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
