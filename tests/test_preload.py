"""Tests for the webapp-shaped docassemble module preload pass."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from stub_runtime import purge_docassemble_modules

from docassemble_simulator import _preload


@pytest.fixture(autouse=True)
def _reset_preload():
    _preload.reset_preload_state()
    yield
    _preload.reset_preload_state()


@pytest.fixture
def imported_modules():
    """Delete ``docassemble`` modules a test imports without touching others."""
    before = set(sys.modules)
    yield
    for name in list(sys.modules):
        if name not in before and (
            name == "docassemble" or name.startswith("docassemble.")
        ):
            del sys.modules[name]


def _package_tree(tmp_path: Path) -> Path:
    root = tmp_path / "docassemble"
    package = root / "pkg"
    package.mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "withclass.py").write_text("class Thing:\n    pass\n")
    (package / "donotload.py").write_text(
        "# do not pre-load\nclass Hidden:\n    pass\n"
    )
    (package / "optin.py").write_text("# pre-load\nMARKER = 'optin'\n")
    (package / "update.py").write_text(
        "import docassemble.base.util\ndocassemble.base.util.update(x='y')\n"
    )
    core = root / "base"
    core.mkdir()
    (core / "__init__.py").write_text("")
    (core / "util.py").write_text("def update(**kwargs):\n    return None\n")
    (core / "ignored.py").write_text("class Core:\n    pass\n")
    return root


def _preload_tree(root, monkeypatch, config):
    """Make a stub package tree the target of the preload entrypoint."""
    monkeypatch.syspath_prepend(str(root.parent))
    monkeypatch.setattr(_preload, "_package_directories", lambda: [root])
    monkeypatch.setattr(_preload, "_effective_config", lambda: config)
    monkeypatch.setattr(_preload, "_runtime_available", lambda: False)
    purge_docassemble_modules(monkeypatch)


def test_preload_imports_opted_in_modules_and_skips_the_rest(
    tmp_path, monkeypatch, imported_modules
):
    _preload_tree(_package_tree(tmp_path), monkeypatch, {})

    assert _preload.preload_installed_modules() == ()

    assert "docassemble.pkg.withclass" in sys.modules
    assert "docassemble.pkg.optin" in sys.modules
    assert "docassemble.pkg.update" in sys.modules
    assert "docassemble.pkg.donotload" not in sys.modules
    assert "docassemble.base.ignored" not in sys.modules


def test_whitelisted_modules_are_imported(tmp_path, monkeypatch, imported_modules):
    _preload_tree(
        _package_tree(tmp_path),
        monkeypatch,
        {"module whitelist": ["docassemble.pkg.*"]},
    )

    assert _preload.preload_installed_modules() == ()
    assert "docassemble.pkg.donotload" in sys.modules
    assert "docassemble.pkg.withclass" in sys.modules


def test_blacklisted_modules_are_not_imported(tmp_path, monkeypatch, imported_modules):
    _preload_tree(
        _package_tree(tmp_path),
        monkeypatch,
        {"module blacklist": ["docassemble.pkg.update"]},
    )

    assert _preload.preload_installed_modules() == ()
    assert "docassemble.pkg.update" not in sys.modules
    assert "docassemble.pkg.withclass" in sys.modules


def test_preload_imports_class_modules_and_skips_opt_outs(
    tmp_path, monkeypatch, imported_modules
):
    _preload_tree(_package_tree(tmp_path), monkeypatch, {})

    failures = _preload.preload_installed_modules()

    assert failures == ()
    assert "docassemble.pkg.withclass" in sys.modules
    assert "docassemble.pkg.donotload" not in sys.modules


def test_preload_failures_are_reported_but_not_fatal(
    tmp_path, monkeypatch, imported_modules
):
    root = _package_tree(tmp_path)
    (root / "pkg" / "broken.py").write_text(
        "raise RuntimeError('boom')\nclass AlsoBroken:\n    pass\n"
    )
    _preload_tree(root, monkeypatch, {})

    failures = _preload.preload_installed_modules()

    names = {name for name, _ in failures}
    assert "docassemble.pkg.broken" in names
    assert not any(name.endswith(("withclass", "optin")) for name in names)


def test_preload_is_a_no_op_without_a_package_directory(monkeypatch):
    monkeypatch.setattr(_preload, "_package_directories", list)

    assert _preload.preload_installed_modules() == ()
