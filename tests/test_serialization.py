"""Tests for the bounded docassemble namespace serializer."""

from __future__ import annotations

import sys
import types

from stub_runtime import (
    blocked_docassemble_imports,
    docassemble_package,
    install_modules,
)

from docassemble_simulator._serialization import (
    _NODE_BUDGET,
    install_serialization_guard,
)


def _install_fake_safe_json(monkeypatch):
    """Install a docassemble stub whose recursive calls go through the module."""
    da, base = docassemble_package()
    functions = types.ModuleType("docassemble.base.functions")
    calls: list[int] = []

    def upstream(the_object, level=0, is_key=False):
        calls.append(level)
        if level > 20:
            return "None" if is_key else None
        if isinstance(the_object, dict):
            return {
                key: functions.safe_json(value, level=level + 1)
                for key, value in the_object.items()
            }
        if isinstance(the_object, list):
            return [functions.safe_json(value, level=level + 1) for value in the_object]
        return the_object

    functions.safe_json = upstream
    base.functions = functions
    install_modules(
        monkeypatch,
        {
            "docassemble": da,
            "docassemble.base": base,
            "docassemble.base.functions": functions,
        },
    )
    return functions, calls


def test_guard_cuts_a_cycle_before_re_expanding_it(monkeypatch):
    functions, calls = _install_fake_safe_json(monkeypatch)

    install_serialization_guard()

    node: dict = {}
    node["self"] = node
    result = functions.safe_json(node)

    assert result == {"self": None}
    # The guard, not upstream's depth cutoff, is what stopped the revisit.
    assert len(calls) == 1


def test_guard_bounds_exponential_shared_paths(monkeypatch):
    functions, calls = _install_fake_safe_json(monkeypatch)

    install_serialization_guard()

    current: dict = {}
    root = current
    for _ in range(40):
        shared: dict = {}
        current["a"] = shared
        current["b"] = shared
        current = shared
    result = functions.safe_json(root)

    assert result is not None
    assert len(calls) <= _NODE_BUDGET


def test_guard_leaves_plain_values_unchanged(monkeypatch):
    functions, _ = _install_fake_safe_json(monkeypatch)

    install_serialization_guard()

    plain = {"a": [1, 2, {"b": "c"}], "d": None}
    assert functions.safe_json(plain) == plain


def test_guard_install_is_idempotent(monkeypatch):
    functions, _ = _install_fake_safe_json(monkeypatch)

    install_serialization_guard()
    guarded = functions.safe_json
    install_serialization_guard()

    assert functions.safe_json is guarded
    assert guarded._dasimulator_bounded


def test_guard_is_noop_without_docassemble(monkeypatch):
    with blocked_docassemble_imports(monkeypatch):
        install_serialization_guard()
        assert "docassemble.base.functions" not in sys.modules
