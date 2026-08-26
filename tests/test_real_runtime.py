from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest


@pytest.fixture
def real_python():
    configured = os.environ.get("DASIMULATOR_REAL_PYTHON")
    if not configured:
        pytest.skip("set DASIMULATOR_REAL_PYTHON to a target-package interpreter")
    interpreter = Path(configured).expanduser().absolute()
    if not interpreter.is_file():
        pytest.fail(f"real-runtime interpreter does not exist: {interpreter}")
    return interpreter


@pytest.fixture
def real_workspace(tmp_path, real_python):
    package = tmp_path / "docassemble" / "regression"
    questions = package / "data" / "questions"
    templates = package / "data" / "templates"
    questions.mkdir(parents=True)
    templates.mkdir()
    (package / "__init__.py").write_text("")
    (package / "helpers.py").write_text(
        "def declared_helper(value):\n    return f'declared:{value}'\n"
    )
    (questions / "main.yml").write_text(
        "---\n"
        "modules:\n"
        "  - docassemble.regression.helpers\n"
        "---\n"
        "mandatory: True\n"
        "question: Dates\n"
        "fields:\n"
        "  - Filing date: filing_date\n"
        "    datatype: date\n"
        "    required: False\n"
        "  - Caption: caption\n"
        "    required: False\n"
    )
    generator = """
import sys
from pathlib import Path
from docx import Document

templates = Path(sys.argv[1])
contents = {
    "helpers.docx": "{{ currency(1234.5) }}|{{ redact('secret') }}|{{ nice_number(2) }}|{{ capitalize('hello') }}|{{ declared_helper('x') }}|{{ format_date(as_datetime('2026-08-26'), 'MM/dd/yyyy') }}",
    "date.docx": "{{ filing_date.format('MM/dd/yyyy') }}|{{ caption }}",
}
for name, text in contents.items():
    document = Document()
    document.add_paragraph(text)
    document.save(templates / name)
"""
    generated = subprocess.run(
        [str(real_python), "-c", generator, str(templates)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr
    fixture = tmp_path / "fixture.py"
    fixture.write_text("fixture_value = 'loaded'\n")
    return tmp_path


def _environment():
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    python_path = [str(root / "src")]
    if environment.get("PYTHONPATH"):
        python_path.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    return environment


def _run(real_python, root, *arguments, expected_code=0):
    completed = subprocess.run(
        [
            str(real_python),
            "-m",
            "docassemble_simulator",
            "--root",
            str(root),
            "--json",
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_environment(),
        timeout=120,
    )
    assert completed.returncode == expected_code, completed.stdout + completed.stderr
    assert completed.stderr == ""
    return json.loads(completed.stdout)


def _document_xml(path):
    with ZipFile(path) as archive:
        return archive.read("word/document.xml").decode("utf-8")


def _assert_helper_output(path):
    xml = _document_xml(path)
    for expected in (
        "$1,234.50",
        "██████",
        "two",
        "Hello",
        "declared:x",
        "08/26/2026",
    ):
        assert expected in xml


def test_real_runtime_rehydrates_helpers_for_every_render_source(
    real_python, real_workspace
):
    root = real_workspace
    snapshot = root / "state.snapshot"
    fresh = root / "fresh.docx"
    fixture = root / "fixture.docx"
    from_snapshot = root / "snapshot.docx"
    saved = root / "saved.docx"

    assert _run(
        real_python,
        root,
        "render",
        "helpers.docx",
        "--fresh",
        "--no-assemble",
        "--save-snapshot",
        str(snapshot),
        "--output",
        str(fresh),
    )["ok"]
    assert not list((root / ".simulator" / "sessions").glob("*.pkl"))
    assert _run(
        real_python,
        root,
        "render",
        "helpers.docx",
        "--fixture",
        str(root / "fixture.py"),
        "--output",
        str(fixture),
    )["ok"]
    assert _run(
        real_python,
        root,
        "render",
        "helpers.docx",
        "--snapshot",
        str(snapshot),
        "--no-assemble",
        "--output",
        str(from_snapshot),
    )["ok"]
    assert _run(real_python, root, "start")["ok"]
    assert _run(
        real_python,
        root,
        "render",
        "helpers.docx",
        "--no-assemble",
        "--output",
        str(saved),
    )["ok"]

    for artifact in (fresh, fixture, from_snapshot, saved):
        _assert_helper_output(artifact)


def test_real_date_answer_formats_and_rejections_roll_back(real_python, real_workspace):
    root = real_workspace
    assert _run(real_python, root, "start")["ok"]
    session = next((root / ".simulator" / "sessions").glob("*.pkl"))
    before = session.read_bytes()

    rejected = _run(
        real_python,
        root,
        "answer",
        "filing_date=2026-02-30",
        "caption=changed",
        expected_code=2,
    )
    assert rejected["error"]["kind"] == "answer-input"
    assert session.read_bytes() == before

    accepted = _run(
        real_python,
        root,
        "answer",
        "filing_date=2026-08-26",
        "caption=2026-08-26",
    )
    assert accepted["ok"]
    assert (
        _run(real_python, root, "eval", "type(filing_date).__name__")["result"]["value"]
        == "'DADateTime'"
    )
    assert (
        _run(real_python, root, "eval", "filing_date.format('MM/dd/yyyy')")["result"][
            "value"
        ]
        == "'08/26/2026'"
    )
    assert (
        _run(real_python, root, "eval", "type(caption).__name__")["result"]["value"]
        == "'str'"
    )

    artifact = root / "date.docx"
    assert _run(
        real_python,
        root,
        "render",
        "date.docx",
        "--no-assemble",
        "--output",
        str(artifact),
    )["ok"]
    xml = _document_xml(artifact)
    assert "08/26/2026" in xml
    assert "2026-08-26" in xml

    assert _run(real_python, root, "start")["ok"]
    assert _run(
        real_python,
        root,
        "answer",
        "--code",
        "filing_date='2026-08-26'",
    )["ok"]
    assert (
        _run(real_python, root, "eval", "type(filing_date).__name__")["result"]["value"]
        == "'str'"
    )
