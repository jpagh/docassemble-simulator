"""Tests for runtime import probing and acquisition policy."""

from __future__ import annotations

import pytest

from docassemble_simulator import preflight


def test_missing_runtime_acquisition_disabled_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "_import_error", lambda name: "missing")

    with pytest.raises(SystemExit, match="missing docassemble runtime package"):
        preflight.ensure_importable(tmp_path, install_missing=False)


def test_offline_missing_runtime_never_installs(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(preflight, "_import_error", lambda name: "missing")
    monkeypatch.setattr(
        preflight,
        "_install_missing_runtime",
        lambda root, missing: calls.append(missing),
    )

    with pytest.raises(SystemExit, match="offline mode"):
        preflight.ensure_importable(tmp_path, offline=True)

    assert calls == []


def test_broken_native_import_is_not_reported_as_missing(tmp_path, monkeypatch):
    def fake_import(name):
        if name == "docassemble.base":
            return ImportError("libnative.so: image not found")
        return "missing"

    monkeypatch.setattr(preflight, "_import_error", fake_import)

    with pytest.raises(SystemExit, match="installed but failed to import"):
        preflight.ensure_importable(tmp_path, install_missing=False)


def test_missing_runtime_is_acquired_and_rechecked(tmp_path, monkeypatch):
    state = {"installed": False}
    monkeypatch.setattr(
        preflight,
        "_import_error",
        lambda name: None if state["installed"] else "missing",
    )
    monkeypatch.setattr(
        preflight,
        "_install_missing_runtime",
        lambda root, missing: state.update(installed=True) or "uv pip install ...",
    )

    preflight.ensure_importable(tmp_path)

    assert state["installed"] is True


def test_acquisition_that_does_not_fix_imports_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "_import_error", lambda name: "missing")
    monkeypatch.setattr(
        preflight,
        "_install_missing_runtime",
        lambda root, missing: "uv pip install ...",
    )

    with pytest.raises(
        SystemExit, match="acquisition completed but imports still fail"
    ):
        preflight.ensure_importable(tmp_path)
