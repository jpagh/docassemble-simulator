"""Real-runtime PDF capability boundary for stripped attachments (#88)."""

from __future__ import annotations

import json
import subprocess

import pytest
from test_real_runtime import (  # noqa: F401 - fixtures registered by import
    _assemblyline_available,
    _environment,
    _run,
    family_python,
    real_python,
    real_python_19,
)

from docassemble_simulator._runtime import PDF_UNAVAILABLE_MESSAGE

pytestmark = pytest.mark.real_runtime

URL_HELPER = """\
---
mandatory: True
question: |
  All done.
subquestion: |
  [download your pleading](${ complaint.pdf.url_for() })
---
attachment:
  filename: complaint
  name: Complaint
  variable name: complaint
  content: |
    This is a complaint.
"""

TEMPLATE_REFERENCE = """\
---
mandatory: True
question: |
  All done.
subquestion: |
  ${ summary }
---
attachment:
  filename: complaint
  name: Complaint
  variable name: complaint
  content: |
    This is a complaint.
---
attachment:
  filename: summary
  name: Summary
  variable name: summary
  docx template file: summary.docx
"""

MANUAL_ATTACHMENT = """\
---
question: Your document is ready.
attachment:
  - name: A merged document
    filename: report
    manual:
      pdf: pdf_concatenate(document_one, document_two)
mandatory: True
---
attachment:
  variable name: document_one
  content: |
    Hello, world!
---
attachment:
  variable name: document_two
  content: |
    Goodbye, world!
"""

MANUAL_CODE_ATTACHMENT = """\
---
question: Your document is ready.
attachment:
  - name: A merged document
    filename: report
    manual code: |
      {'pdf': pdf_concatenate(document_one, document_two)}
mandatory: True
---
attachment:
  variable name: document_one
  content: |
    Hello, world!
---
attachment:
  variable name: document_two
  content: |
    Goodbye, world!
"""

GENERATOR_CONCATENATION = """\
---
question: Your document is ready.
attachment:
  - name: A merged document
    filename: report
    manual code: |
      {'pdf': pdf_concatenate(item for item in (document_one, document_two))}
mandatory: True
---
attachment:
  variable name: document_one
  content: |
    Hello, world!
---
attachment:
  variable name: document_two
  content: |
    Goodbye, world!
"""

NESTED_GENERATOR_CONCATENATION = """\
---
question: Your document is ready.
attachment:
  - name: A merged document
    filename: report
    manual code: |
      {'pdf': pdf_concatenate([(item for item in (document_one, document_two))])}
mandatory: True
---
attachment:
  variable name: document_one
  content: |
    Hello, world!
---
attachment:
  variable name: document_two
  content: |
    Goodbye, world!
"""

GENERIC_ATTACHMENT = """\
---
objects:
  - clients: DAList
---
mandatory: True
code: |
  clients.appendObject()
---
generic object: DAObject
attachment:
  filename: complaint
  name: Complaint
  variable name: x.complaint
  content: |
    Hello.
---
mandatory: True
question: |
  All done.
subquestion: |
  [doc](${ clients[0].complaint.pdf.url_for() })
"""

INDEXED_ATTACHMENT = """\
---
include:
  - docassemble.AssemblyLine:assembly_line.yml
---
objects:
  - indexed_doc: ALDocument.using(title='Indexed', filename='indexed', enabled=True, has_addendum=False)
---
attachment:
  name: Indexed document
  variable name: indexed_doc[i]
  docx template file: plain.docx
---
mandatory: True
code: |
  indexed_doc.as_docx()
---
mandatory: True
question: |
  All done.
subquestion: |
  [pdf](${ indexed_doc[0].pdf.url_for() })
"""

PDF_ONLY_ATTACHMENT = """\
---
mandatory: True
question: |
  All done.
subquestion: |
  ${ pdf_only }
---
attachment:
  filename: pdf_only
  name: PDF only
  variable name: pdf_only
  content: |
    This is PDF only.
  valid formats:
    - pdf
"""

NON_ATTACHMENT_PDF_ROOT = """\
---
objects:
  - not_an_attachment: DAObject
---
mandatory: True
question: |
  All done.
subquestion: |
  [download](${ not_an_attachment.pdf.url_for() })
"""

NON_PDF_MISSING = """\
---
mandatory: True
question: |
  All done.
subquestion: |
  ${ missing_value }
"""

PDF_PRESENCE_PROBE = """\
---
mandatory: True
question: |
  All done.
subquestion: |
  has: ${ hasattr(complaint, 'pdf') }
  get: ${ getattr(complaint, 'pdf', None) }
  formats: ${ complaint.info['formats'] }
---
attachment:
  filename: complaint
  name: Complaint
  variable name: complaint
  content: |
    This is a complaint.
"""


@pytest.fixture
def issue88_workspace(tmp_path, real_python):  # noqa: F811 - imported fixture
    package = tmp_path / "docassemble" / "issue88"
    questions = package / "data" / "questions"
    templates = package / "data" / "templates"
    questions.mkdir(parents=True)
    templates.mkdir()
    (package / "__init__.py").write_text("")
    fixtures = {
        "url.yml": URL_HELPER,
        "template.yml": TEMPLATE_REFERENCE,
        "manual.yml": MANUAL_ATTACHMENT,
        "manual-code.yml": MANUAL_CODE_ATTACHMENT,
        "generator-concat.yml": GENERATOR_CONCATENATION,
        "nested-generator-concat.yml": NESTED_GENERATOR_CONCATENATION,
        "generic.yml": GENERIC_ATTACHMENT,
        "indexed.yml": INDEXED_ATTACHMENT,
        "pdf-only.yml": PDF_ONLY_ATTACHMENT,
        "non-attachment-root.yml": NON_ATTACHMENT_PDF_ROOT,
        "non-pdf.yml": NON_PDF_MISSING,
        "probe.yml": PDF_PRESENCE_PROBE,
    }
    for name, text in fixtures.items():
        (questions / name).write_text(text)
    generated = subprocess.run(
        [
            str(real_python),
            "-c",
            (
                "import sys\n"
                "from docx import Document\n"
                "document = Document()\n"
                "document.add_paragraph(sys.argv[1])\n"
                "document.save(sys.argv[2])\n"
            ),
            "complaint: {{ complaint.pdf }}",
            str(templates / "summary.docx"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_environment(),
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr
    generated = subprocess.run(
        [
            str(real_python),
            "-c",
            (
                "import sys\n"
                "from docx import Document\n"
                "document = Document()\n"
                "document.add_paragraph(sys.argv[1])\n"
                "document.save(sys.argv[2])\n"
            ),
            "Indexed document.",
            str(templates / "plain.docx"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_environment(),
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr
    return tmp_path


@pytest.mark.parametrize(
    ("filename", "sought"),
    [
        ("url.yml", "complaint.pdf"),
        ("template.yml", "complaint.pdf"),
        ("generic.yml", "clients[0].complaint.pdf"),
        ("manual.yml", None),
        ("manual-code.yml", None),
        ("generator-concat.yml", None),
        ("nested-generator-concat.yml", None),
        ("pdf-only.yml", None),
    ],
)
def test_pdf_requests_report_the_typed_capability(
    family_python,  # noqa: F811 - imported fixture
    issue88_workspace,
    filename,
    sought,
):
    case = family_python
    payload = _run(
        case.interpreter,
        issue88_workspace,
        "start",
        "--interview",
        f"docassemble.issue88:data/questions/{filename}",
        expected_code=2,
    )

    assert payload["ok"] is False, case.label
    error = payload["error"]
    assert error["kind"] == "pdf-unavailable", case.label
    assert error["message"] == PDF_UNAVAILABLE_MESSAGE, case.label
    if sought is not None:
        assert error["details"]["sought_variable"] == sought, case.label


def test_info_publishes_the_same_capability_message(
    family_python,  # noqa: F811 - imported fixture
    issue88_workspace,
):
    case = family_python
    payload = _run(case.interpreter, issue88_workspace, "info")

    assert payload["result"]["capabilities"]["pdf_message"] == PDF_UNAVAILABLE_MESSAGE


def test_explicit_pdf_seek_reports_the_capability(
    family_python,  # noqa: F811 - imported fixture
    issue88_workspace,
):
    case = family_python
    _run(
        case.interpreter,
        issue88_workspace,
        "start",
        "--interview",
        "docassemble.issue88:data/questions/url.yml",
        expected_code=2,
    )

    payload = _run(
        case.interpreter,
        issue88_workspace,
        "seek",
        "complaint.pdf",
        "--interview",
        "docassemble.issue88:data/questions/url.yml",
        expected_code=2,
    )

    assert payload["error"]["kind"] == "pdf-unavailable", case.label
    assert payload["error"]["message"] == PDF_UNAVAILABLE_MESSAGE, case.label


@pytest.mark.parametrize(
    ("filename", "sought"),
    [
        ("non-attachment-root.yml", "not_an_attachment.pdf"),
        ("non-pdf.yml", "missing_value"),
    ],
)
def test_genuine_missing_variables_keep_unresolved_provenance(
    family_python,  # noqa: F811 - imported fixture
    issue88_workspace,
    filename,
    sought,
):
    case = family_python
    payload = _run(
        case.interpreter,
        issue88_workspace,
        "start",
        "--interview",
        f"docassemble.issue88:data/questions/{filename}",
        expected_code=2,
    )

    assert payload["ok"] is False, case.label
    error = payload["error"]
    assert error["kind"] == "unresolved-variable", case.label
    assert error["details"]["sought_variable"] == sought, case.label
    culprit = error["details"]["culprit"]
    assert culprit["sought_variable"] == sought, case.label
    assert culprit["provenance"]["source_file"].endswith(filename), case.label


def test_pdf_presence_probes_keep_their_answers(
    family_python,  # noqa: F811 - imported fixture
    issue88_workspace,
):
    case = family_python
    payload = _run(
        case.interpreter,
        issue88_workspace,
        "start",
        "--interview",
        "docassemble.issue88:data/questions/probe.yml",
    )

    text = payload["result"]["subquestion_text"]
    assert "has: False" in text, case.label
    assert "get: None" in text, case.label
    assert "formats: ['rtf', 'html']" in text, case.label


def test_indexed_attachment_pdf_seek_is_typed(
    family_python,  # noqa: F811 - imported fixture
    issue88_workspace,
):
    case = family_python
    if not _assemblyline_available(case.interpreter):
        pytest.skip(f"AssemblyLine is not provisioned for {case.label}")

    payload = _run(
        case.interpreter,
        issue88_workspace,
        "start",
        "--interview",
        "docassemble.issue88:data/questions/indexed.yml",
        expected_code=2,
    )

    assert payload["error"]["kind"] == "pdf-unavailable", case.label
    assert payload["error"]["message"] == PDF_UNAVAILABLE_MESSAGE, case.label


ALIAS_HARNESS = """\
import json
import sys
from pathlib import Path

from docassemble_simulator import _preload
from docassemble_simulator._runtime import SimulatorRuntime
from docassemble_simulator.bootstrap import bootstrap
from docassemble_simulator.preflight import ensure_importable

root = Path(sys.argv[1]).resolve()
observed = {}
original_preload = _preload.preload_installed_modules


def checking_preload():
    result = original_preload()
    from docassemble.AssemblyLine import al_document
    from docassemble.base import util

    observed["preload_alias_same"] = (
        al_document.pdf_concatenate is util.pdf_concatenate
    )
    observed["preload_alias_marked"] = getattr(
        al_document.pdf_concatenate,
        "_dasimulator_pdf_concatenate_fallback",
        False,
    )
    return result


_preload.preload_installed_modules = checking_preload
with SimulatorRuntime().activate(root):
    ensure_importable(root, offline=True)
    bootstrap()
    from docassemble.AssemblyLine import al_document
    from docassemble.base import legal, util

    print(json.dumps({
        "preload_alias_same": observed.get("preload_alias_same", False),
        "preload_alias_marked": observed.get("preload_alias_marked", False),
        "util_marked": getattr(
            util.pdf_concatenate, "_dasimulator_pdf_concatenate_fallback", False
        ),
        "al_document_same": al_document.pdf_concatenate is util.pdf_concatenate,
        "legal_same": legal.pdf_concatenate is util.pdf_concatenate,
    }))
"""


def test_pdf_concatenate_install_precedes_preload_and_reaches_aliases(
    family_python,  # noqa: F811 - imported fixture
    issue88_workspace,
):
    case = family_python
    if not _assemblyline_available(case.interpreter):
        pytest.skip(f"AssemblyLine is not provisioned for {case.label}")

    completed = subprocess.run(
        [str(case.interpreter), "-c", ALIAS_HARNESS, str(issue88_workspace)],
        capture_output=True,
        text=True,
        check=False,
        env=_environment(),
        timeout=180,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["preload_alias_same"], case.label
    assert payload["preload_alias_marked"], case.label
    assert payload["util_marked"], case.label
    assert payload["al_document_same"], case.label
    assert payload["legal_same"], case.label
