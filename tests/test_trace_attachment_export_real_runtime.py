"""Issue #84: retained published files through native replay transports."""

from __future__ import annotations

from collections import Counter
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from test_real_runtime import (  # noqa: F401 - fixtures registered by import
    family_python,
    real_python,
    real_python_19,
    real_workspace,
    worker_journey,
)
from test_trace_real_runtime import _command

pytestmark = pytest.mark.real_runtime


def _replay(transport, case, root, journey, golden, output):
    if transport == "worker":
        return journey.request(
            "trace-replay", golden=str(golden), artifact_dir=str(output)
        )
    code, response = _command(
        case.interpreter,
        root,
        "trace",
        "replay",
        str(golden),
        "--artifact-dir",
        str(output),
    )
    assert code == (
        0
        if response["ok"]
        else (2 if response["error"]["kind"] == "trace-mismatch" else 1)
    ), response
    return response


@pytest.mark.parametrize("transport", ["worker", "cli"])
def test_native_replay_retains_published_docx_and_zip(
    family_python,  # noqa: F811
    real_workspace,  # noqa: F811
    worker_journey,  # noqa: F811
    tmp_path,
    transport,
):
    case = family_python
    root = real_workspace
    golden = tmp_path / "bundle.trace.jsonl"
    source = root / "docassemble/regression/data/questions/al-bundle.yml"
    source.write_text(
        source.read_text().replace(
            "  test_bundle.generate_downloads_task =",
            "  hidden = DAFile('hidden')\n"
            "  hidden.initialize(filename='not-published.txt')\n"
            "  hidden.write('Private intermediate')\n"
            "  test_bundle.generate_downloads_task =",
        )
    )
    journey = worker_journey(
        case.interpreter,
        root,
        family=case.probe,
        interview="al-bundle.yml",
        arguments=("--offline", "--seek-diagnostics", "off"),
    )
    for operation, options in (
        ("start", {}),
        ("answer", {"assignments": ["bundle_user_name=Replay Reader"]}),
        ("answer", {"assignments": ["bundle_task_ready=True"]}),
    ):
        response = journey.request(operation, record=str(golden), **options)
        assert response["ok"], response
    original = golden.read_bytes()
    saved = {p: p.read_bytes() for p in journey.session_root.rglob("*") if p.is_file()}
    baseline = journey.request("trace-replay", golden=str(golden))
    assert baseline["ok"], baseline
    assert "attachments" not in baseline["result"]

    output = tmp_path / "retained"
    exported = _replay(transport, case, root, journey, golden, output)
    assert exported["ok"], exported
    manifest = exported["result"]["attachments"]
    assert Counter(item["filename"] for item in manifest) == {
        "Test_document.docx": 1,
        "Test_document_two.docx": 1,
        "bundle.zip": 1,
        "bundle.docx": 3,
    }
    assert {item["extension"] for item in manifest} == {"docx", "zip"}
    assert list((root / ".simulator").glob("trace-replay-*")) == []
    for item in manifest:
        path = Path(item["path"])
        assert path.is_relative_to(output)
        assert item["uri"] == path.as_uri()
        assert item["mimetype"] == (
            "application/zip"
            if item["extension"] == "zip"
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        assert item["structure"] == ("ok" if item["extension"] == "docx" else "n/a")
        with ZipFile(path) as archive:
            assert archive.testzip() is None
            if item["extension"] == "docx":
                assert b"Replay Reader" in archive.read("word/document.xml")
            else:
                assert archive.namelist()
                for member in archive.namelist():
                    assert member.endswith(".docx")
                    with ZipFile(BytesIO(archive.read(member))) as document:
                        assert b"Replay Reader" in document.read("word/document.xml")
    assert {p for p in output.rglob("*") if p.is_file()} == {
        Path(item["path"]) for item in manifest
    }
    report = dict(exported["result"])
    report.pop("attachments")
    assert report == baseline["result"]
    assert golden.read_bytes() == original
    assert {
        p: p.read_bytes() for p in journey.session_root.rglob("*") if p.is_file()
    } == saved
    assert list((root / ".simulator").glob("trace-replay-*")) == []


@pytest.mark.parametrize("transport", ["worker", "cli"])
def test_native_replay_reports_export_failures_without_masking_divergence(
    family_python,  # noqa: F811
    real_workspace,  # noqa: F811
    worker_journey,  # noqa: F811
    tmp_path,
    transport,
):
    case = family_python
    root = real_workspace
    source = root / "docassemble/regression/data/questions/download.yml"
    authored = source.read_text()
    golden = tmp_path / "download.trace.jsonl"
    journey = worker_journey(
        case.interpreter,
        root,
        family=case.probe,
        interview="download.yml",
        arguments=("--offline", "--seek-diagnostics", "off"),
    )
    assert journey.request("start", record=str(golden))["ok"]
    original = golden.read_bytes()
    invalid = _replay(transport, case, root, journey, golden, "")
    assert not invalid["ok"], invalid
    assert invalid["error"]["kind"] == "input"
    assert "artifact_dir must be a path string" in invalid["error"]["message"]
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("Keep this file")
    failed = _replay(transport, case, root, journey, golden, blocked)
    assert not failed["ok"], failed
    assert failed["error"]["kind"] == "input"
    details = failed["error"]["details"]
    assert details["clean"] is True
    assert details["counts"] == {"expected": 1, "replayed": 1, "matched": 1}
    assert details["attachments"] == []
    assert "published attachment export failed" in details["export_error"]["message"]
    assert blocked.read_text() == "Keep this file"

    # Native publication still succeeds, but the author deletes the source
    # before replay's copy. This exercises an actual copy failure, not a mock.
    missing_source = (
        authored + '  ${ __import__("os").unlink(final_document.docx.path()) }\n'
    )
    source.write_text(missing_source)
    failed = _replay(transport, case, root, journey, golden, tmp_path / "missing")
    assert not failed["ok"], failed
    assert failed["error"]["kind"] == "input"
    details = failed["error"]["details"]
    assert details["clean"] is True
    assert "No such file" in details["export_error"]["message"]
    assert details["attachments"] == []
    assert not [p for p in (tmp_path / "missing").rglob("*") if p.is_file()]

    source.write_text(authored.replace("question: Done", "id: changed\nquestion: Done"))
    divergent = _replay(transport, case, root, journey, golden, tmp_path / "divergent")
    assert not divergent["ok"], divergent
    assert divergent["error"]["kind"] == "trace-mismatch"
    details = divergent["error"]["details"]
    assert details["clean"] is False
    assert details["first_divergence"]["step"] == 1
    assert len(details["attachments"]) == 1
    with ZipFile(details["attachments"][0]["path"]) as document:
        assert document.read("word/document.xml")

    failed = _replay(transport, case, root, journey, golden, blocked)
    assert not failed["ok"], failed
    assert failed["error"]["kind"] == "trace-mismatch"
    failed_details = failed["error"]["details"]
    assert failed_details["counts"] == details["counts"]
    assert failed_details["steps"] == details["steps"]
    assert failed_details["first_divergence"] == details["first_divergence"]
    assert (
        "published attachment export failed"
        in failed_details["export_error"]["message"]
    )
    assert failed_details["attachments"] == []
    assert golden.read_bytes() == original
    assert list((root / ".simulator").glob("trace-replay-*")) == []


def test_native_replay_retains_completed_copies_when_a_later_copy_fails(
    family_python,  # noqa: F811
    real_workspace,  # noqa: F811
    worker_journey,  # noqa: F811
    tmp_path,
):
    case = family_python
    root = real_workspace
    source = root / "docassemble/regression/data/questions/download.yml"
    source.write_text(
        source.read_text()
        + "  ${ missing_document.docx.url_for() }\n"
        + '  ${ __import__("os").unlink(missing_document.docx.path()) }\n'
        + "---\nattachment:\n  name: Missing document\n"
        + "  filename: missing_document\n  variable name: missing_document\n"
        + "  docx template file: helpers.docx\n  valid formats: [docx]\n"
    )
    golden = tmp_path / "partial.trace.jsonl"
    journey = worker_journey(
        case.interpreter,
        root,
        family=case.probe,
        interview="download.yml",
        arguments=("--offline", "--seek-diagnostics", "off"),
    )
    assert journey.request("start", record=str(golden))["ok"]
    output = tmp_path / "partial"
    failed = journey.request(
        "trace-replay", golden=str(golden), artifact_dir=str(output)
    )
    assert not failed["ok"], failed
    assert failed["error"]["kind"] == "input"
    details = failed["error"]["details"]
    assert details["clean"] is True
    assert "No such file" in details["export_error"]["message"]
    assert [item["filename"] for item in details["attachments"]] == [
        "local_document.docx"
    ]
    retained = Path(details["attachments"][0]["path"])
    assert {p for p in output.rglob("*") if p.is_file()} == {retained}
    assert list((root / ".simulator").glob("trace-replay-*")) == []
    with ZipFile(retained) as document:
        assert document.read("word/document.xml")


def test_native_automatic_replay_exports_published_files(
    family_python,  # noqa: F811
    real_workspace,  # noqa: F811
    worker_journey,  # noqa: F811
    tmp_path,
):
    case = family_python
    root = real_workspace
    source = root / "docassemble/regression/data/questions/download.yml"
    source.write_text(
        source.read_text().replace(
            "question: Done", "question: Done\ncontinue button field: downloaded"
        )
        + "---\ncode: |\n  finished = downloaded\n"
    )
    golden = tmp_path / "automatic.trace.jsonl"
    journey = worker_journey(
        case.interpreter,
        root,
        family=case.probe,
        interview="download.yml",
        arguments=("--offline", "--seek-diagnostics", "off"),
    )
    recorded = journey.request(
        "seek", variable="finished", fresh=True, auto=True, record=str(golden)
    )
    assert recorded["ok"], recorded
    baseline = journey.request("trace-replay", golden=str(golden))
    assert baseline["ok"], baseline
    exported = journey.request(
        "trace-replay", golden=str(golden), artifact_dir=str(tmp_path / "automatic")
    )
    assert exported["ok"], exported
    report = dict(exported["result"])
    manifest = report.pop("attachments")
    assert report == baseline["result"]
    assert report["counts"] == {"expected": 2, "replayed": 2, "matched": 2}
    assert [item["filename"] for item in manifest] == ["local_document.docx"]
    assert list((root / ".simulator").glob("trace-replay-*")) == []
    with ZipFile(manifest[0]["path"]) as document:
        assert document.read("word/document.xml")
