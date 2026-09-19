"""PDF skip fallback and local generated-file creation seams.

The fallback patches AssemblyLine document classes, so these tests install a
fake ``docassemble.AssemblyLine.al_document`` module and assert the observable
behavior of the wrapped ``as_pdf`` methods (ADR-0002 adapter seam).
"""

from __future__ import annotations

import sys
import types

import pytest

from docassemble_simulator._diagnostics import capture_diagnostics
from docassemble_simulator._runtime import (
    PDF_SKIP_DIAGNOSTIC,
    install_pdf_skip_fallback,
)


class _DAAttributeError(AttributeError):
    """Stand-in for docassemble's undefined-attribute exception."""


def _install_stub_docassemble(monkeypatch):
    error = types.ModuleType("docassemble.base.error")
    error.DAAttributeError = _DAAttributeError
    functions = types.ModuleType("docassemble.base.functions")
    functions.this_thread = types.SimpleNamespace(misc={})
    base = types.ModuleType("docassemble.base")
    base.error = error
    base.functions = functions
    monkeypatch.setitem(sys.modules, "docassemble.base", base)
    monkeypatch.setitem(sys.modules, "docassemble.base.error", error)
    monkeypatch.setitem(sys.modules, "docassemble.base.functions", functions)
    return functions


def _install_fake_al_document(monkeypatch, *, as_pdf=None):
    module = types.ModuleType("docassemble.AssemblyLine.al_document")

    class FakeDocument:
        def __init__(self, *, docx=True):
            self.docx = docx
            self.instanceName = "fake_document"
            self.pdf_calls = 0

        def _is_docx(self, key="final"):
            return self.docx

        def as_pdf(
            self,
            key="final",
            refresh=True,
            pdfa=False,
            append_matching_suffix=True,
        ):
            self.pdf_calls += 1
            if as_pdf is not None:
                return as_pdf(self, key, refresh, pdfa, append_matching_suffix)
            raise _DAAttributeError("name 'fake_document.pdf' is not defined")

        def as_docx(self, key="final", refresh=True, append_matching_suffix=True):
            return {"format": "docx", "key": key, "refresh": refresh}

    class FakeStaticDocument(FakeDocument):
        pass

    class FakeBundle(FakeDocument):
        pass

    module.ALDocument = FakeDocument
    module.ALStaticDocument = FakeStaticDocument
    module.ALDocumentBundle = FakeBundle
    package = types.ModuleType("docassemble.AssemblyLine")
    package.al_document = module
    monkeypatch.setitem(sys.modules, "docassemble.AssemblyLine", package)
    monkeypatch.setitem(sys.modules, "docassemble.AssemblyLine.al_document", module)
    return module, FakeDocument


def test_docx_backed_pdf_request_returns_docx_without_touching_pdf(monkeypatch):
    _install_stub_docassemble(monkeypatch)
    _, FakeDocument = _install_fake_al_document(monkeypatch)
    install_pdf_skip_fallback()

    document = FakeDocument(docx=True)
    with capture_diagnostics() as diagnostics:
        result = document.as_pdf(key="final", refresh=False, pdfa=True)

    assert result == {"format": "docx", "key": "final", "refresh": False}
    assert document.pdf_calls == 0
    assert [item.kind for item in diagnostics] == [PDF_SKIP_DIAGNOSTIC]
    assert diagnostics[0].details["document"] == "fake_document"
    assert diagnostics[0].details["format"] == "docx"


def test_non_docx_pdf_request_keeps_the_original_failure(monkeypatch):
    _install_stub_docassemble(monkeypatch)
    _, FakeDocument = _install_fake_al_document(monkeypatch)
    install_pdf_skip_fallback()

    document = FakeDocument(docx=False)
    with pytest.raises(_DAAttributeError):
        document.as_pdf()

    assert document.pdf_calls == 1


def test_missing_pdf_error_falls_back_when_docx_appears_after_failure(monkeypatch):
    _install_stub_docassemble(monkeypatch)
    _, FakeDocument = _install_fake_al_document(monkeypatch)
    install_pdf_skip_fallback()

    class LateDocxDocument(FakeDocument):
        def __init__(self):
            super().__init__(docx=True)
            self.checks = 0

        def _is_docx(self, key="final"):
            self.checks += 1
            return self.checks > 1

    document = LateDocxDocument()
    result = document.as_pdf()

    assert result == {"format": "docx", "key": "final", "refresh": True}
    assert document.pdf_calls == 1


def test_pdf_skip_installation_is_idempotent(monkeypatch):
    _install_stub_docassemble(monkeypatch)
    _, FakeDocument = _install_fake_al_document(monkeypatch)

    install_pdf_skip_fallback()
    wrapped_once = FakeDocument.as_pdf
    install_pdf_skip_fallback()

    assert FakeDocument.as_pdf is wrapped_once
    assert FakeDocument(docx=True).as_pdf()["format"] == "docx"


def test_install_is_a_noop_without_assemblyline(monkeypatch):
    _install_stub_docassemble(monkeypatch)
    monkeypatch.setitem(sys.modules, "docassemble.AssemblyLine", None)

    install_pdf_skip_fallback()  # does not raise
