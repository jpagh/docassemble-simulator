"""Shared stub-runtime helpers for fast tests (no docassemble required)."""

from __future__ import annotations

import importlib.abc
import sys
import types
from contextlib import contextmanager


def docassemble_package():
    """A bare ``docassemble`` + ``docassemble.base`` package skeleton."""
    da = types.ModuleType("docassemble")
    da.__path__ = []
    base = types.ModuleType("docassemble.base")
    base.__path__ = []
    da.base = base
    return da, base


def install_modules(monkeypatch, modules, absent=()):
    """Install stub modules, removing names the test declares absent."""
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    for name in absent:
        monkeypatch.delitem(sys.modules, name, raising=False)


def stub_legacy_server_modules(monkeypatch, *, server=None, daconfig=None):
    """Install stub runtime modules exposing a legacy ``functions.server``."""
    da, base = docassemble_package()
    functions = types.ModuleType("docassemble.base.functions")
    functions.server = types.SimpleNamespace() if server is None else server
    config = types.ModuleType("docassemble.base.config")
    config.daconfig = {} if daconfig is None else daconfig
    util = types.ModuleType("docassemble.base.util")
    util.Individual = type("Individual", (), {})
    da.base = base
    base.functions = functions
    base.config = config
    base.util = util
    install_modules(
        monkeypatch,
        {
            "docassemble": da,
            "docassemble.base": base,
            "docassemble.base.functions": functions,
            "docassemble.base.config": config,
            "docassemble.base.util": util,
        },
    )
    return functions, config


def legacy_functions(thread=None, daconfig=None, omit=()):
    """A recording legacy ``functions`` double with thread-local semantics."""
    thread = thread if thread is not None else types.SimpleNamespace()
    calls = {"restored": []}
    functions = types.SimpleNamespace(
        server=types.SimpleNamespace(),
        this_thread=thread,
    )
    functions.populate_this_thread_defaults = lambda: setattr(thread, "populated", True)
    functions.backup_thread_variables = lambda: setattr(thread, "backed_up", True)

    def restore_thread_variables(saved):
        calls["restored"].append(dict(saved))
        vars(thread).clear()
        vars(thread).update(saved)

    functions.restore_thread_variables = restore_thread_variables
    for name in omit:
        delattr(functions, name)
    functions.calls = calls
    functions.seed_daconfig = dict(daconfig or {})
    return functions


def install_legacy_modules(monkeypatch, functions):
    """Expose a legacy-only stub runtime through ``sys.modules``."""
    da, base = docassemble_package()
    module = types.ModuleType("docassemble.base.functions")
    for name in (
        "server",
        "this_thread",
        "populate_this_thread_defaults",
        "backup_thread_variables",
        "restore_thread_variables",
    ):
        if hasattr(functions, name):
            setattr(module, name, getattr(functions, name))
    config = types.ModuleType("docassemble.base.config")
    config.daconfig = functions.seed_daconfig
    da.base = base
    base.functions = module
    base.config = config
    install_modules(
        monkeypatch,
        {
            "docassemble": da,
            "docassemble.base": base,
            "docassemble.base.functions": module,
            "docassemble.base.config": config,
        },
    )
    return config


def stub_incomplete_runtime(monkeypatch, *, with_server, modern=False):
    """Install a stub runtime missing webapp hooks or the legacy server."""
    da, base = docassemble_package()
    functions = types.ModuleType("docassemble.base.functions")
    if with_server:
        functions.server = types.SimpleNamespace()
    config = types.ModuleType("docassemble.base.config")
    config.daconfig = {}
    util = types.ModuleType("docassemble.base.util")
    util.Individual = type("Individual", (), {})
    webapp = types.ModuleType("docassemble.webapp")
    webapp.__path__ = []
    modules = {
        "docassemble": da,
        "docassemble.base": base,
        "docassemble.base.functions": functions,
        "docassemble.base.config": config,
        "docassemble.base.util": util,
        "docassemble.webapp": webapp,
    }
    if modern:
        pm_module = types.ModuleType("docassemble.base.plugin_manager")
        pm_module.pm = types.SimpleNamespace(get_plugin=lambda name: object())
        modules["docassemble.base.plugin_manager"] = pm_module
    absent = (
        *(("docassemble.base.plugin_manager",) if not modern else ()),
        "docassemble.webapp.main",
        "docassemble.webapp.main.hooks",
        "docassemble.webapp.interview",
        "docassemble.webapp.interview.hooks",
    )
    install_modules(monkeypatch, modules, absent=absent)
    return functions


@contextmanager
def blocked_docassemble_imports():
    """Block every ``docassemble`` import, simulating a wrong interpreter."""
    for name in [
        name
        for name in sys.modules
        if name == "docassemble" or name.startswith("docassemble.")
    ]:
        del sys.modules[name]

    class _Blocker(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "docassemble" or fullname.startswith("docassemble."):
                raise ModuleNotFoundError(
                    f"No module named {fullname!r}", name=fullname
                )

    blocker = _Blocker()
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        sys.meta_path.remove(blocker)


def stub_legacy_without_background(monkeypatch):
    """Legacy stub with thread state but no modern background module."""
    from docassemble_simulator import _runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_BACKGROUND_INSTALLED", False)
    da, base = docassemble_package()
    functions = types.ModuleType("docassemble.base.functions")
    functions.server = types.SimpleNamespace()
    interview = types.SimpleNamespace(
        askfor=lambda *args, **kwargs: {
            "question": types.SimpleNamespace(
                question_type="backgroundresponse", backgroundresponse=42
            )
        }
    )
    functions.this_thread = types.SimpleNamespace(
        current_dict={},
        current_info={},
        interview_status=object(),
        interview=interview,
    )
    config = types.ModuleType("docassemble.base.config")
    config.daconfig = {}
    util = types.ModuleType("docassemble.base.util")
    util.Individual = type("Individual", (), {})
    util.background_action = None
    install_modules(
        monkeypatch,
        {
            "docassemble": da,
            "docassemble.base": base,
            "docassemble.base.functions": functions,
            "docassemble.base.config": config,
            "docassemble.base.util": util,
        },
        absent=(
            "docassemble.base.background",
            "docassemble.base.plugin_manager",
        ),
    )
    return functions, util
