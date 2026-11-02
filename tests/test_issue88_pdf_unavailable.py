"""Typed PDF-unavailable classification for stripped attachments (#88)."""

from __future__ import annotations

import re
import sys
import types
from types import SimpleNamespace

import pytest

from docassemble_simulator import _runtime as runtime_module
from docassemble_simulator._outcomes import ErrorKind
from docassemble_simulator._runtime import (
    PDF_UNAVAILABLE_MESSAGE,
    PDFConversionUnavailable,
    install_attachment_filename_fallback,
    install_pdf_concatenate_fallback,
    pdf_stripped_attachment,
)
from docassemble_simulator.execution import (
    Evaluate,
    Execute,
    Refresh,
    Seek,
    Start,
)


class _DAError(Exception):
    pass


class _DAErrorMissingVariable(_DAError):
    def __init__(self, message, variable):
        super().__init__(message)
        self.variable = variable


class _DAFileCollection:
    def __init__(self, instance_name, info):
        self.instanceName = instance_name
        self.info = info


class _DAFile:
    pass


class _DAFileList(list):
    pass


class _DAList(list):
    pass


def _install_stub_runtime(monkeypatch, *, pdf_concatenate=None):
    """Install the docassemble surface these seams import at call time."""
    da = types.ModuleType("docassemble")
    da.__path__ = []
    base = types.ModuleType("docassemble.base")
    base.__path__ = []
    error = types.ModuleType("docassemble.base.error")
    error.DAError = _DAError
    error.DAErrorMissingVariable = _DAErrorMissingVariable
    error.DAAttributeError = AttributeError
    parse = types.ModuleType("docassemble.base.parse")
    parse.substitute_vars_from_user_dict = (
        lambda variable, user_dict, is_generic=False: variable
    )
    functions = types.ModuleType("docassemble.base.functions")
    functions.intrinsic_name_of = lambda variable, the_user_dict=None: variable
    util = types.ModuleType("docassemble.base.util")
    util.DAFileCollection = _DAFileCollection
    util.DAFileList = _DAFileList
    util.DAList = _DAList
    util.DAFile = _DAFile
    util.DAStaticFile = _DAFile
    if pdf_concatenate is not None:
        util.pdf_concatenate = pdf_concatenate
    base.error = error
    base.parse = parse
    base.functions = functions
    base.util = util
    da.base = base
    for name, module in (
        ("docassemble", da),
        ("docassemble.base", base),
        ("docassemble.base.error", error),
        ("docassemble.base.functions", functions),
        ("docassemble.base.parse", parse),
        ("docassemble.base.util", util),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return util


def _marked_collection(name="complaint", **info):
    return _DAFileCollection(
        name,
        {"formats": ["rtf", "html"], runtime_module._PDF_STRIPPED_INFO_KEY: True}
        | info,
    )


class _FinalizerQuestion:
    """A finalizer that creates the collection the way docassemble does."""

    def finalize_attachment(self, attachment, result, user_dict):
        variable_name = attachment["variable_name"]
        user_dict[variable_name] = _DAFileCollection(
            variable_name, {"formats": list(result["formats_to_use"])}
        )
        return result


def test_finalizer_marks_the_substituted_collection_name(monkeypatch):
    """Marker resolution follows upstream intrinsic/generic name substitution."""
    _install_stub_runtime(monkeypatch)
    functions = sys.modules["docassemble.base.functions"]
    parse = sys.modules["docassemble.base.parse"]
    functions.intrinsic_name_of = lambda variable, the_user_dict=None: variable

    def substitute(variable, user_dict, is_generic=False):
        if is_generic and user_dict.get("x") is not None:
            variable = re.sub(r"^x\b", user_dict["x"].instanceName, variable)
        return variable

    parse.substitute_vars_from_user_dict = substitute
    collection = _DAFileCollection("docs", {"formats": ["docx"]})
    namespace = {
        "x": SimpleNamespace(instanceName="docs"),
        "docs": [collection],
    }

    runtime_module._mark_pdf_stripped_attachment(
        {"variable_name": "x[0]"}, namespace, is_generic=True
    )

    assert collection.info[runtime_module._PDF_STRIPPED_INFO_KEY] is True


def test_finalizer_marks_an_attachment_only_when_it_stripped_pdf(monkeypatch):
    _install_stub_runtime(monkeypatch)
    parse = sys.modules["docassemble.base.parse"]
    parse.Question = _FinalizerQuestion
    install_attachment_filename_fallback()

    user_dict = {}
    question = _FinalizerQuestion()
    question.finalize_attachment(
        {"variable_name": "complaint"},
        {"formats_to_use": ["pdf", "docx"], "valid_formats": ["pdf", "docx"]},
        user_dict,
    )
    assert user_dict["complaint"].info[runtime_module._PDF_STRIPPED_INFO_KEY] is True

    user_dict = {}
    _FinalizerQuestion().finalize_attachment(
        {"variable_name": "only_docx"},
        {"formats_to_use": ["docx"], "valid_formats": ["docx"]},
        user_dict,
    )
    assert runtime_module._PDF_STRIPPED_INFO_KEY not in user_dict["only_docx"].info


def test_pdf_stripped_attachment_requires_a_marked_collection(monkeypatch):
    _install_stub_runtime(monkeypatch)
    marked = _marked_collection()
    unmarked = _DAFileCollection("only_docx", {"formats": ["docx"]})

    assert pdf_stripped_attachment("complaint.pdf", {"complaint": marked}) is marked
    assert pdf_stripped_attachment("complaint.pdf", {}) is None
    assert pdf_stripped_attachment("complaint.pdf", {"complaint": unmarked}) is None
    assert pdf_stripped_attachment("only_docx.pdf", {"only_docx": unmarked}) is None
    assert pdf_stripped_attachment("complaint", {"complaint": marked}) is None


def test_pdf_unavailable_failure_keeps_other_missing_variables_unresolved(
    monkeypatch,
):
    from docassemble_simulator.execution import _pdf_unavailable_failure

    _install_stub_runtime(monkeypatch)
    namespace = {"complaint": _marked_collection()}
    missing_pdf = _DAErrorMissingVariable(
        "There was a reference to a variable 'complaint.pdf'", "complaint.pdf"
    )
    message, details = _pdf_unavailable_failure(missing_pdf, namespace)
    assert message == PDF_UNAVAILABLE_MESSAGE
    assert details == {
        "sought_variable": "complaint.pdf",
        "error_type": "_DAErrorMissingVariable",
    }

    assert _pdf_unavailable_failure(missing_pdf, {}) is None
    non_pdf = _DAErrorMissingVariable(
        "There was a reference to a variable 'other'", "other"
    )
    assert _pdf_unavailable_failure(non_pdf, namespace) is None


def test_pdf_conversion_unavailable_maps_to_the_capability_kind(monkeypatch):
    from docassemble_simulator.execution import _pdf_unavailable_failure

    _install_stub_runtime(monkeypatch)
    message, details = _pdf_unavailable_failure(
        PDFConversionUnavailable(PDF_UNAVAILABLE_MESSAGE), {}
    )
    assert message == PDF_UNAVAILABLE_MESSAGE
    assert details == {"error_type": "PDFConversionUnavailable"}
    assert ErrorKind.PDF_UNAVAILABLE == "pdf-unavailable"


def test_pdf_concatenate_no_files_over_a_stripped_attachment_is_typed(monkeypatch):
    def no_valid_files(*args, **kwargs):
        raise _DAError("pdf_concatenate: no valid files to concatenate")

    util = _install_stub_runtime(monkeypatch, pdf_concatenate=no_valid_files)
    install_pdf_concatenate_fallback()

    stripped = _marked_collection("document_one")
    with pytest.raises(PDFConversionUnavailable, match="PDF output"):
        util.pdf_concatenate(stripped, _marked_collection("document_two"))

    unmarked = _DAFileCollection("document_one", {"formats": ["docx"]})
    with pytest.raises(_DAError, match="no valid files to concatenate"):
        util.pdf_concatenate(unmarked)


def test_pdf_concatenate_materializes_one_shot_iterables(monkeypatch):
    seen = []

    def no_valid_files(*args, **kwargs):
        seen.append(args)
        raise _DAError("pdf_concatenate: no valid files to concatenate")

    util = _install_stub_runtime(monkeypatch, pdf_concatenate=no_valid_files)
    install_pdf_concatenate_fallback()
    stripped = _marked_collection("document_one")

    with pytest.raises(PDFConversionUnavailable, match="PDF output"):
        util.pdf_concatenate(item for item in (stripped, stripped))
    assert seen[-1] == ((stripped, stripped),)

    with pytest.raises(PDFConversionUnavailable, match="PDF output"):
        util.pdf_concatenate([(item for item in (stripped, stripped))])
    assert seen[-1] == ([(stripped, stripped)],)

    with pytest.raises(_DAError, match="no valid files to concatenate"):
        util.pdf_concatenate(item for item in (object(),))


def test_pdf_concatenate_installation_is_idempotent_and_rebinds_reexports(
    monkeypatch,
):
    def original_pdf_concatenate(*args, **kwargs):
        return None

    util = _install_stub_runtime(monkeypatch, pdf_concatenate=original_pdf_concatenate)
    legal = types.ModuleType("docassemble.base.legal")
    legal.pdf_concatenate = original_pdf_concatenate
    al_document = types.ModuleType("docassemble.AssemblyLine.al_document")
    al_document.pdf_concatenate = original_pdf_concatenate
    monkeypatch.setitem(sys.modules, "docassemble.base.legal", legal)
    monkeypatch.setitem(
        sys.modules, "docassemble.AssemblyLine.al_document", al_document
    )

    install_pdf_concatenate_fallback()
    patched = util.pdf_concatenate
    assert patched is not original_pdf_concatenate
    assert getattr(patched, "_dasimulator_pdf_concatenate_fallback", False)
    assert legal.pdf_concatenate is patched
    assert al_document.pdf_concatenate is patched

    install_pdf_concatenate_fallback()
    assert util.pdf_concatenate is patched
    assert legal.pdf_concatenate is patched
    assert al_document.pdf_concatenate is patched


def test_refresh_translates_a_pdf_conversion_failure_to_the_capability(
    tmp_path, monkeypatch, da_stubs
):
    from test_detect_session import FakeInterview, _execution

    class SeekingInterview(FakeInterview):
        fail = False

        def askfor(self, variable, *args, **kwargs):
            if self.fail:
                raise PDFConversionUnavailable(PDF_UNAVAILABLE_MESSAGE)
            return {"type": "continue"}

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, SeekingInterview)
    assert execution.run(Start()).ok
    assert execution.run(Seek("target", activate=True)).ok
    SeekingInterview.fail = True

    outcome = execution.run(Refresh())

    assert not outcome.ok
    assert outcome.error.kind == ErrorKind.PDF_UNAVAILABLE
    assert outcome.error.message == PDF_UNAVAILABLE_MESSAGE


def test_refresh_translates_a_stripped_pdf_seek_to_the_capability(
    tmp_path, monkeypatch, da_stubs
):
    from test_detect_session import FakeInterview, _execution

    class SeekingInterview(FakeInterview):
        fail = False

        def populate_non_pickleable(self, namespace):
            namespace["complaint"] = _marked_collection()

        def askfor(self, variable, *args, **kwargs):
            if self.fail:
                raise _DAErrorMissingVariable("missing complaint.pdf", "complaint.pdf")
            return {"type": "continue"}

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, SeekingInterview)
    monkeypatch.setattr(
        sys.modules["docassemble.base.error"],
        "DAErrorMissingVariable",
        _DAErrorMissingVariable,
        raising=False,
    )
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"],
        "DAFileCollection",
        _DAFileCollection,
        raising=False,
    )
    assert execution.run(Start()).ok
    assert execution.run(Seek("complaint.pdf", activate=True)).ok
    SeekingInterview.fail = True

    outcome = execution.run(Refresh())

    assert not outcome.ok
    assert outcome.error.kind == ErrorKind.PDF_UNAVAILABLE
    assert outcome.error.details["sought_variable"] == "complaint.pdf"


def test_refresh_reports_the_capability_through_execution(
    tmp_path, monkeypatch, da_stubs
):
    from test_detect_session import FakeInterview, _execution

    class SeekingInterview(FakeInterview):
        def askfor(self, variable, *args, **kwargs):
            return {"type": "continue"}

    execution, _, selected = _execution(
        tmp_path, monkeypatch, da_stubs, SeekingInterview
    )
    assert execution.run(Start()).ok
    assert execution.run(Seek("target", activate=True)).ok

    class PdfFailureInterview(FakeInterview):
        def askfor(self, variable, *args, **kwargs):
            raise PDFConversionUnavailable(PDF_UNAVAILABLE_MESSAGE)

    selected["interview"] = PdfFailureInterview
    outcome = execution.run(Refresh())

    assert not outcome.ok
    assert outcome.error.kind == ErrorKind.PDF_UNAVAILABLE
    assert outcome.error.message == PDF_UNAVAILABLE_MESSAGE


@pytest.mark.parametrize(
    "operation", [Evaluate("fail()"), Execute("fail()", assemble=False)]
)
def test_evaluate_and_execute_translate_a_pdf_conversion_failure_to_the_capability(
    tmp_path, monkeypatch, da_stubs, operation
):
    from test_detect_session import FakeInterview, _execution

    def fail():
        raise PDFConversionUnavailable(PDF_UNAVAILABLE_MESSAGE)

    class PdfFailureInterview(FakeInterview):
        def populate_non_pickleable(self, namespace):
            namespace["fail"] = fail

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, PdfFailureInterview)
    assert execution.run(Start()).ok

    outcome = execution.run(operation)

    assert not outcome.ok
    assert outcome.error.kind == ErrorKind.PDF_UNAVAILABLE
    assert outcome.error.message == PDF_UNAVAILABLE_MESSAGE
