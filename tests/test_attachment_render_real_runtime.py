"""Real-runtime attachment-aware DOCX rendering across docassemble 1.9.x and 1.10.x."""

from __future__ import annotations

import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

import pytest
from test_real_runtime import (  # noqa: F401 - fixtures registered by import
    _environment,
    _run,
    family_python,
    real_python,
    real_python_19,
)

pytestmark = pytest.mark.real_runtime


MAIN_INTERVIEW = """\
---
objects:
  - documents: DADict.using(auto_gather=False, gathered=True)
---
code: |
  documents.valid_formats = ['docx', 'pdf']
  stage_a = documents.initializeObject('stage_a', DAObject)
  stage_a.title = 'Variance Petition Alpha'
  stage_a.secret = 'SSN-999-00-1234'
  stage_a.subdoc_note = 'Included subdoc note from stage A'
  stage_a.items = ['ZeroItem', 'OneItem']
  stage_a.initializeAttribute('original', DAList, auto_gather=False, gathered=True)
  stage_a.original.appendObject()
  stage_a.original.appendObject()
  stage_a.initializeAttribute('redacted', DAList, auto_gather=False, gathered=True)
  stage_a.redacted.appendObject()

  stage_b = documents.initializeObject('stage_b', DAObject)
  stage_b.title = 'Variance Petition Beta'
  stage_b.secret = 'SSN-888-00-5678'
  stage_b.subdoc_note = 'Included subdoc note from stage B'
  stage_b.items = ['BetaZero', 'BetaOne']
  stage_b.initializeAttribute('original', DAList, auto_gather=False, gathered=True)
  stage_b.original.appendObject()
  stages_ready = True
---
mandatory: True
code: |
  _ = stages_ready
  _ = documents['stage_a'].original[0].petition
  _ = documents['stage_a'].redacted[0].petition
  _ = documents['stage_a'].original[0].editable_doc
---
generic object: DAObject
attachments:
  - name: Petition Original
    filename: Petition_Original
    variable name: x.original[0].petition
    docx template file: petition.docx
    valid formats:
      code: documents.valid_formats
    redact: False
    fields:
      - label: Original Variant
    field code:
      - S: raw(x)
  - name: Petition Redacted
    filename: Petition_Redacted
    variable name: x.redacted[0].petition
    docx template file: petition.docx
    valid formats:
      code: documents.valid_formats
    redact: True
    fields:
      - label: Redacted Variant
    field code:
      - S: raw(x)
---
generic object: DAObject
attachments:
  - name: Indexed Petition
    filename: Indexed_Petition
    variable name: x.original[i].indexed_doc
    docx template file: indexed.docx
    valid formats:
      - docx
    field code:
      - S: raw(x)
---
generic object: DAObject
attachments:
  - name: Missing Flag Petition
    filename: Missing_Flag
    variable name: x.original[0].missing_check
    docx template file: missing_flag.docx
    valid formats:
      - docx
    field code:
      - S: raw(x)
---
generic object: DAObject
attachments:
  - name: PDF Only Petition
    filename: PDF_Only
    variable name: x.original[0].pdf_only_doc
    docx template file: editable.docx
    valid formats:
      - pdf
    field code:
      - S: raw(x)
---
generic object: DAObject
attachments:
  - name: Editable Petition
    filename: Editable_Petition
    variable name: x.original[0].editable_doc
    docx template file: editable.docx
    valid formats:
      - docx
    field code:
      - S: raw(x)
---
attachments:
  - name: Standalone Certificate
    filename: Standalone_Certificate
    variable name: standalone_doc
    docx template file: standalone.docx
    valid formats:
      - docx
    fields:
      - greeting: Hello from standalone fields
    field code:
      - S: raw(documents['stage_a'])
---
mandatory: True
question: All documents prepared.
"""


SINGLE_CANDIDATE_INTERVIEW = """\
---
objects:
  - documents: DADict.using(auto_gather=False, gathered=True)
---
code: |
  stage_a = documents.initializeObject('stage_a', DAObject)
  stage_a.title = 'Wrong Stage Alpha'
  stage_a.secret = 'A-secret'
  stage_a.subdoc_note = 'A-subdoc'
  stage_a.initializeAttribute('original', DAList, auto_gather=False, gathered=True)
  stage_a.original.appendObject()

  stage_b = documents.initializeObject('stage_b', DAObject)
  stage_b.title = 'Right Stage Beta'
  stage_b.secret = 'B-secret'
  stage_b.subdoc_note = 'B-subdoc'
  stage_b.initializeAttribute('original', DAList, auto_gather=False, gathered=True)
  stage_b.original.appendObject()
  stages_ready = True
---
mandatory: True
code: |
  _ = stages_ready
  _ = documents['stage_b'].original[0].petition
---
generic object: DAObject
attachments:
  - name: Petition Original
    filename: Petition_Original
    variable name: x.original[0].petition
    docx template file: petition.docx
    valid formats:
      - docx
    redact: False
    fields:
      - label: Single Candidate
    field code:
      - S: raw(x)
---
mandatory: True
question: Ready for auto-resolution.
"""


UNFINISHED_INTERVIEW = """\
---
objects:
  - documents: DADict.using(auto_gather=False, gathered=True)
---
question: Tell me your name.
fields:
  - Name: user_name
---
code: |
  stage_a = documents.initializeObject('stage_a', DAObject)
  stage_a.title = user_name
  stage_a.secret = 'secret'
  stage_a.subdoc_note = 'note'
  stage_a.initializeAttribute('original', DAList, auto_gather=False, gathered=True)
  stage_a.original.appendObject()
  stages_ready = True
---
mandatory: True
code: |
  _ = stages_ready
  _ = documents['stage_a'].original[0].petition
---
generic object: DAObject
attachments:
  - name: Petition Original
    filename: Petition_Original
    variable name: x.original[0].petition
    docx template file: petition.docx
    valid formats:
      - docx
    redact: False
    fields:
      - label: Unfinished
    field code:
      - S: raw(x)
---
mandatory: True
question: Done.
"""


SIBLING_ISOLATION_INTERVIEW = """\
---
objects:
  - documents: DADict.using(auto_gather=False, gathered=True)
---
code: |
  stage_a = documents.initializeObject('stage_a', DAObject)
  stage_a.title = 'Good Sibling Title'
  stage_a.initializeAttribute('original', DAList, auto_gather=False, gathered=True)
  stage_a.original.appendObject()
  stage_a.initializeAttribute('redacted', DAList, auto_gather=False, gathered=True)
  stage_a.redacted.appendObject()
  stages_ready = True
---
mandatory: True
code: |
  _ = stages_ready
---
generic object: DAObject
attachments:
  - name: Good Sibling
    filename: Good_Sibling
    variable name: x.original[0].good_doc
    docx template file: editable.docx
    valid formats:
      - docx
    field code:
      - S: raw(x)
  - name: Broken Sibling
    filename: Broken_Sibling
    variable name: x.redacted[0].broken_doc
    docx template file: missing_flag.docx
    valid formats:
      - docx
    field code:
      - S: raw(x)
---
mandatory: True
question: Sibling test ready.
"""


def _write_docx(interpreter: Path, destination: Path, paragraphs: list[str]) -> None:
    generated = subprocess.run(
        [
            str(interpreter),
            "-c",
            (
                "import json, sys\n"
                "from docx import Document\n"
                "document = Document()\n"
                "for text in json.loads(sys.argv[1]):\n"
                "    document.add_paragraph(text)\n"
                "document.save(sys.argv[2])\n"
            ),
            json.dumps(paragraphs),
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_environment(),
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr


def _docx_text(path: Path) -> str:
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    return "\n".join(root.itertext())


def _session_hashes(workspace: Path) -> dict[str, str]:
    sessions = workspace / ".simulator" / "sessions"
    if not sessions.is_dir():
        return {}
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(sessions.glob("*.pkl"))
    }


@pytest.fixture
def attachment_workspace(tmp_path, real_python):  # noqa: F811 - imported fixture
    package = tmp_path / "docassemble" / "attrender"
    questions = package / "data" / "questions"
    templates = package / "data" / "templates"
    questions.mkdir(parents=True)
    templates.mkdir()
    (package / "__init__.py").write_text("")

    (questions / "main.yml").write_text(MAIN_INTERVIEW)
    (questions / "single_candidate.yml").write_text(SINGLE_CANDIDATE_INTERVIEW)
    (questions / "unfinished.yml").write_text(UNFINISHED_INTERVIEW)
    (questions / "sibling_isolation.yml").write_text(SIBLING_ISOLATION_INTERVIEW)

    _write_docx(
        real_python,
        templates / "petition.docx",
        [
            "Title: {{ S.title }}",
            "Variant: {{ label }}",
            "AttachmentName: {{ current_context().attachment.name }}",
            "Secret: {{ redact(S.secret) }}",
            "{{p include_docx_template('partial.docx') }}",
        ],
    )
    _write_docx(
        real_python,
        templates / "partial.docx",
        ["Subdoc: {{ S.subdoc_note }}"],
    )
    _write_docx(
        real_python,
        templates / "indexed.docx",
        ["Index={{ i }} Value={{ S.items[i] }}"],
    )
    _write_docx(
        real_python,
        templates / "missing_flag.docx",
        ["Title: {{ S.title }}", "Notary: {{ S.notary_block_yn }}"],
    )
    _write_docx(
        real_python,
        templates / "editable.docx",
        ["Version-One: {{ S.title }}"],
    )
    _write_docx(
        real_python,
        templates / "standalone.docx",
        ["Greeting: {{ greeting }}", "Stage: {{ S.title }}"],
    )
    return tmp_path


def test_explicit_attachment_renders_twins_subdoc_redact_and_preserves_session(
    family_python,  # noqa: F811 - imported fixture
    attachment_workspace,
    tmp_path,
):
    case = family_python
    interview = "docassemble.attrender:data/questions/main.yml"

    started = _run(
        case.interpreter, attachment_workspace, "start", "--interview", interview
    )
    assert started["ok"] is True, case.label
    before_hashes = _session_hashes(attachment_workspace)
    assert before_hashes, case.label

    orig_out = tmp_path / f"orig-{case.label}.docx"
    orig = _run(
        case.interpreter,
        attachment_workspace,
        "render",
        "--interview",
        interview,
        "--attachment",
        "documents['stage_a'].original[0].petition",
        "--output",
        str(orig_out),
    )
    assert orig["ok"] is True, (case.label, orig)
    assert orig["result"]["template"] == "petition.docx"
    assert orig["result"]["attachment"] == "documents['stage_a'].original[0].petition"
    assert orig["result"]["artifact"] == str(orig_out.resolve())
    assert orig["result"]["paragraphs"] >= 4
    assert len(orig.get("attachments", [])) == 1
    assert orig["attachments"][0]["extension"] == "docx"
    assert orig["attachments"][0]["structure"] == "ok"

    orig_text = _docx_text(orig_out)
    assert "Title: Variance Petition Alpha" in orig_text, case.label
    assert "Variant: Original Variant" in orig_text, case.label
    assert "AttachmentName: Petition Original" in orig_text, case.label
    assert "SSN-999-00-1234" in orig_text, case.label
    assert "Subdoc: Included subdoc note from stage A" in orig_text, case.label

    # Redacted twin with double-quoted caller spelling
    red_out = tmp_path / f"red-{case.label}.docx"
    red = _run(
        case.interpreter,
        attachment_workspace,
        "render",
        "--interview",
        interview,
        "--attachment",
        'documents["stage_a"].redacted[0].petition',
        "--output",
        str(red_out),
    )
    assert red["ok"] is True, (case.label, red)
    assert red["result"]["attachment"] == "documents['stage_a'].redacted[0].petition"

    red_text = _docx_text(red_out)
    assert "Title: Variance Petition Alpha" in red_text, case.label
    assert "Variant: Redacted Variant" in red_text, case.label
    assert "AttachmentName: Petition Redacted" in red_text, case.label
    assert "SSN-999-00-1234" not in red_text, case.label
    assert "Subdoc: Included subdoc note from stage A" in red_text, case.label

    # valid formats included 'pdf', yet no PDF file was created in .simulator/files
    files_dir = attachment_workspace / ".simulator" / "files"
    assert not list(files_dir.glob("*.pdf")), case.label

    # Saved session is byte-identical after ephemeral attachment renders
    assert _session_hashes(attachment_workspace) == before_hashes, case.label


def test_indexed_attachment_binds_index_and_stale_collection_rerenders_on_edit(
    family_python,  # noqa: F811 - imported fixture
    attachment_workspace,
    tmp_path,
):
    case = family_python
    interview = "docassemble.attrender:data/questions/main.yml"
    _run(case.interpreter, attachment_workspace, "start", "--interview", interview)

    for idx, expected_item in ((0, "ZeroItem"), (1, "OneItem")):
        out = tmp_path / f"idx-{idx}-{case.label}.docx"
        payload = _run(
            case.interpreter,
            attachment_workspace,
            "render",
            "--interview",
            interview,
            "--attachment",
            f"documents['stage_a'].original[{idx}].indexed_doc",
            "--output",
            str(out),
        )
        assert payload["ok"] is True, (case.label, payload)
        text = _docx_text(out)
        assert f"Index={idx} Value={expected_item}" in text, case.label

    # Stale cached collection re-renders after template edit on disk
    first_out = tmp_path / f"edit-1-{case.label}.docx"
    first = _run(
        case.interpreter,
        attachment_workspace,
        "render",
        "--interview",
        interview,
        "--attachment",
        "documents['stage_a'].original[0].editable_doc",
        "--output",
        str(first_out),
    )
    assert first["ok"] is True, (case.label, first)
    assert "Version-One: Variance Petition Alpha" in _docx_text(first_out)

    editable_template = (
        attachment_workspace
        / "docassemble"
        / "attrender"
        / "data"
        / "templates"
        / "editable.docx"
    )
    _write_docx(
        case.interpreter,
        editable_template,
        ["Version-Two-Edited: {{ S.title }}"],
    )

    second_out = tmp_path / f"edit-2-{case.label}.docx"
    second = _run(
        case.interpreter,
        attachment_workspace,
        "render",
        "--interview",
        interview,
        "--attachment",
        "documents['stage_a'].original[0].editable_doc",
        "--output",
        str(second_out),
    )
    assert second["ok"] is True, (case.label, second)
    assert "Version-Two-Edited: Variance Petition Alpha" in _docx_text(second_out)


def test_expect_missing_aliases_pdf_only_and_sibling_isolation(
    family_python,  # noqa: F811 - imported fixture
    attachment_workspace,
    tmp_path,
):
    case = family_python
    interview = "docassemble.attrender:data/questions/main.yml"
    _run(case.interpreter, attachment_workspace, "start", "--interview", interview)

    # Missing variable fails with render error and resolved sought_variable
    failed = _run(
        case.interpreter,
        attachment_workspace,
        "render",
        "--interview",
        interview,
        "--attachment",
        "documents['stage_a'].original[0].missing_check",
        expected_code=2,
    )
    assert failed["ok"] is False, case.label
    assert failed["error"]["kind"] == "render", case.label
    assert failed["error"]["details"]["template"] == "missing_flag.docx", case.label
    assert (
        failed["error"]["details"]["sought_variable"]
        == "documents['stage_a'].notary_block_yn"
    ), case.label

    # --expect-missing matches both alias (S.notary_block_yn) and resolved spelling
    for expected_var in ("S.notary_block_yn", "documents['stage_a'].notary_block_yn"):
        passed = _run(
            case.interpreter,
            attachment_workspace,
            "render",
            "--interview",
            interview,
            "--attachment",
            "documents['stage_a'].original[0].missing_check",
            "--expect-missing",
            expected_var,
        )
        assert passed["ok"] is True, (case.label, expected_var, passed)
        assert passed["result"]["paragraphs"] == 0, case.label
        assert "artifact" not in passed["result"], case.label

    # PDF-only attachment maps to pdf-unavailable (exit 2)
    pdf_only = _run(
        case.interpreter,
        attachment_workspace,
        "render",
        "--interview",
        interview,
        "--attachment",
        "documents['stage_a'].original[0].pdf_only_doc",
        expected_code=2,
    )
    assert pdf_only["ok"] is False, case.label
    assert pdf_only["error"]["kind"] == "pdf-unavailable", case.label

    # Sibling isolation: broken sibling in same block does not abort rendering good_doc
    sibling_interview = "docassemble.attrender:data/questions/sibling_isolation.yml"
    _run(
        case.interpreter,
        attachment_workspace,
        "start",
        "--interview",
        sibling_interview,
    )
    good_out = tmp_path / f"good-{case.label}.docx"
    good = _run(
        case.interpreter,
        attachment_workspace,
        "render",
        "--interview",
        sibling_interview,
        "--attachment",
        "documents['stage_a'].original[0].good_doc",
        "--output",
        str(good_out),
    )
    assert good["ok"] is True, (case.label, good)
    assert "Good Sibling Title" in _docx_text(good_out), case.label


def test_automatic_resolution_ambiguity_single_stage_concrete_partial_snapshot_and_worker(
    family_python,  # noqa: F811 - imported fixture
    attachment_workspace,
    tmp_path,
):
    case = family_python
    main_interview = "docassemble.attrender:data/questions/main.yml"
    _run(case.interpreter, attachment_workspace, "start", "--interview", main_interview)

    # Dual-variant template reports both candidates as input error
    ambiguous = _run(
        case.interpreter,
        attachment_workspace,
        "render",
        "petition.docx",
        "--interview",
        main_interview,
        expected_code=1,
    )
    assert ambiguous["ok"] is False, case.label
    assert ambiguous["error"]["kind"] == "input", case.label
    assert ambiguous["error"]["details"]["candidates"] == [
        "documents['stage_a'].original[0].petition",
        "documents['stage_a'].redacted[0].petition",
    ], case.label

    # Concrete attachment entry auto-resolves even when not previously assembled
    standalone_out = tmp_path / f"standalone-{case.label}.docx"
    standalone = _run(
        case.interpreter,
        attachment_workspace,
        "render",
        "standalone.docx",
        "--interview",
        main_interview,
        "--output",
        str(standalone_out),
    )
    assert standalone["ok"] is True, (case.label, standalone)
    assert standalone["result"]["attachment"] == "standalone_doc", case.label
    standalone_text = _docx_text(standalone_out)
    assert "Hello from standalone fields" in standalone_text, case.label
    assert "Variance Petition Alpha" in standalone_text, case.label

    # Standalone partial (not referenced by any attachment entry) stays on direct path
    partial_out = tmp_path / f"partial-{case.label}.docx"
    partial = _run(
        case.interpreter,
        attachment_workspace,
        "render",
        "partial.docx",
        "--interview",
        main_interview,
        "--bind",
        "S=documents['stage_a']",
        "--output",
        str(partial_out),
    )
    assert partial["ok"] is True, (case.label, partial)
    assert "attachment" not in partial["result"], case.label
    assert "Included subdoc note from stage A" in _docx_text(partial_out), case.label

    # Single-candidate multi-stage session binds the right stage (stage_b, not stage_a)
    single_interview = "docassemble.attrender:data/questions/single_candidate.yml"
    _run(
        case.interpreter, attachment_workspace, "start", "--interview", single_interview
    )
    before_single_hashes = _session_hashes(attachment_workspace)

    snap_path = tmp_path / f"single-{case.label}.snapshot"
    auto_out = tmp_path / f"auto-{case.label}.docx"
    auto = _run(
        case.interpreter,
        attachment_workspace,
        "render",
        "petition.docx",
        "--interview",
        single_interview,
        "--save-snapshot",
        str(snap_path),
        "--output",
        str(auto_out),
    )
    assert auto["ok"] is True, (case.label, auto)
    assert auto["result"]["attachment"] == "documents['stage_b'].original[0].petition"
    auto_text = _docx_text(auto_out)
    assert "Right Stage Beta" in auto_text, case.label
    assert "Wrong Stage Alpha" not in auto_text, case.label
    assert _session_hashes(attachment_workspace) == before_single_hashes, case.label

    # Snapshot source auto-resolves identically
    snap_out = tmp_path / f"snap-{case.label}.docx"
    from_snap = _run(
        case.interpreter,
        attachment_workspace,
        "render",
        "petition.docx",
        "--interview",
        single_interview,
        "--snapshot",
        str(snap_path),
        "--no-assemble",
        "--output",
        str(snap_out),
    )
    assert from_snap["ok"] is True, (case.label, from_snap)
    assert (
        from_snap["result"]["attachment"] == "documents['stage_b'].original[0].petition"
    )
    assert "Right Stage Beta" in _docx_text(snap_out), case.label

    # Unfinished session with --no-assemble gives the "no assembled instance" input error
    unfinished_interview = "docassemble.attrender:data/questions/unfinished.yml"
    _run(
        case.interpreter,
        attachment_workspace,
        "start",
        "--interview",
        unfinished_interview,
    )
    unfinished = _run(
        case.interpreter,
        attachment_workspace,
        "render",
        "petition.docx",
        "--interview",
        unfinished_interview,
        "--no-assemble",
        expected_code=1,
    )
    assert unfinished["ok"] is False, case.label
    assert unfinished["error"]["kind"] == "input", case.label
    assert "no assembled attachment instance" in unfinished["error"]["message"], (
        case.label
    )

    # Persistent worker accepts attachment render requests end-to-end
    worker_out = tmp_path / f"worker-{case.label}.docx"
    requests = (
        json.dumps({"id": "1", "operation": "start"})
        + "\n"
        + json.dumps(
            {
                "id": "2",
                "operation": "render",
                "attachment": "documents['stage_a'].original[0].petition",
                "output": str(worker_out),
            }
        )
        + "\n"
        + json.dumps({"id": "3", "operation": "shutdown"})
        + "\n"
    )
    completed = subprocess.run(
        [
            str(case.interpreter),
            "-m",
            "docassemble_simulator",
            "--root",
            str(attachment_workspace),
            "--interview",
            main_interview,
            "run",
        ],
        input=requests,
        capture_output=True,
        text=True,
        check=False,
        env=_environment(),
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    responses = [
        json.loads(line) for line in completed.stdout.splitlines() if line.strip()
    ]
    assert responses[1]["ok"] is True, (case.label, responses[1])
    assert (
        responses[1]["result"]["attachment"]
        == "documents['stage_a'].original[0].petition"
    )
    assert "Title: Variance Petition Alpha" in _docx_text(worker_out), case.label
