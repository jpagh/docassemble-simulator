from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile

import pytest

MODERN = "modern"
LEGACY = "legacy"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class RuntimeFamily:
    """One supported docassemble runtime family under test."""

    label: str  # "1.10+" or "1.9.x" for failure messages
    interpreter: Path
    probe: str  # MODERN or LEGACY capability marker


def _family_probe_cmd():
    # Capability probe, not version branching: every supported family
    # provides docassemble.base; the thread_context marker distinguishes the
    # modern interface from the legacy one.
    return (
        "import docassemble.base, importlib.util; "
        "print('modern' if importlib.util.find_spec("
        "'docassemble.base.thread_context') is not None else 'legacy')"
    )


def _query_family(interpreter, *, strict, env_var="", description=""):
    """Return MODERN/LEGACY for an interpreter, or UNKNOWN when lax."""
    completed = subprocess.run(
        [str(interpreter), "-c", _family_probe_cmd()],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        if not strict:
            return UNKNOWN
        message = (completed.stderr or completed.stdout).strip()
        if env_var and os.environ.get(env_var):
            pytest.fail(
                "the configured real-runtime interpreter cannot import the "
                "docassemble runtime: " + message
            )
        pytest.skip(
            "docassemble runtime is not installed in the pytest interpreter: " + message
        )
    return MODERN if completed.stdout.strip() == MODERN else LEGACY


def _probe_interpreter(env_var, description):
    configured = os.environ.get(env_var)
    interpreter = Path(configured or sys.executable).expanduser().absolute()
    if not interpreter.is_file():
        if configured:
            pytest.fail(f"real-runtime interpreter does not exist: {interpreter}")
        pytest.skip(f"set {env_var} to a {description} target-package interpreter")
    _query_family(interpreter, strict=True, env_var=env_var, description=description)
    return interpreter


@pytest.fixture
def real_python():
    # Run against the interpreter executing pytest by default.  The override
    # remains useful for a separately provisioned target package.
    return _probe_interpreter("DASIMULATOR_REAL_PYTHON", "1.10+")


@pytest.fixture
def real_python_19():
    # A separately provisioned docassemble 1.9.x target-package interpreter,
    # or None when absent.  The family fixture below decides whether the
    # absence skips, so requesting this fixture never skips the modern lane.
    if not os.environ.get("DASIMULATOR_REAL_PYTHON_19"):
        return None
    return _probe_interpreter("DASIMULATOR_REAL_PYTHON_19", "1.9.x")


def _runtime_family(interpreter):
    return _query_family(interpreter, strict=False)


def _assert_family_interpreter(case: RuntimeFamily) -> None:
    """The provisioned interpreter must expose the expected family."""
    assert _runtime_family(case.interpreter) == case.probe, (
        f"{case.label} interpreter does not expose the expected runtime family"
    )


def _assert_question_screen(screen, case: RuntimeFamily) -> None:
    """The stable snake-case screen shape holds for either family."""
    assert screen["kind"] == "question", case.label
    assert screen["question_text"], case.label
    for leaked in ("questionText", "subquestionText", "continueLabel"):
        assert leaked not in screen, (case.label, leaked)


@pytest.fixture(params=[MODERN, LEGACY])
def family_python(request, real_python, real_python_19):
    """One RuntimeFamily per supported runtime family.

    The legacy entry skips when no 1.9.x interpreter is provisioned.
    """
    if request.param == MODERN:
        return RuntimeFamily("1.10+", real_python, MODERN)
    if real_python_19 is None:
        pytest.skip("set DASIMULATOR_REAL_PYTHON_19 to a 1.9.x interpreter")
    return RuntimeFamily("1.9.x", real_python_19, LEGACY)


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
    (questions / "background.yml").write_text(
        "---\n"
        "mandatory: True\n"
        "question: Greeter\n"
        "fields:\n"
        "  - Your name: user_name\n"
        "---\n"
        "code: |\n"
        "  greeting_task = background_action('shout_name')\n"
        "  greeting = greeting_task.get()\n"
        "---\n"
        "mandatory: True\n"
        "question: Result\n"
        "subquestion: ${ greeting }\n"
        "---\n"
        "event: shout_name\n"
        "code: |\n"
        "  background_response('hi:' + user_name)\n"
    )
    (questions / "download.yml").write_text(
        "---\n"
        "modules:\n"
        "  - docassemble.regression.helpers\n"
        "---\n"
        "mandatory: True\n"
        "code: |\n"
        "  final_document\n"
        "---\n"
        "attachment:\n"
        "  name: Local document\n"
        "  filename: local_document\n"
        "  variable name: final_document\n"
        "  docx template file: helpers.docx\n"
        "  valid formats:\n"
        "    - docx\n"
        "---\n"
        "mandatory: True\n"
        "question: Done\n"
        "subquestion: |\n"
        '  <a href="${ final_document.docx.url_for() }">Download</a>\n'
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


def test_minimal_start_answer_contract_across_families(family_python, real_workspace):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = real_workspace

    _assert_family_interpreter(case)

    checked = _run(interpreter, root, "check")
    assert checked["ok"], label
    assert checked["result"]["failures"] == 0, label

    download_checked = _run(interpreter, root, "check", "--interview", "download.yml")
    assert download_checked["ok"], label
    assert download_checked["result"]["failures"] == 0, label

    started = _run(interpreter, root, "start")
    assert started["ok"], label
    screen = started["result"]
    _assert_question_screen(screen, case)

    answered = _run(
        interpreter,
        root,
        "answer",
        "filing_date=2026-08-26",
        "caption=2026-08-26",
    )
    assert answered["ok"], label


def test_seek_contract_across_families(family_python, real_workspace):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = real_workspace
    _assert_family_interpreter(case)

    assert _run(interpreter, root, "start")["ok"], label

    sought = _run(interpreter, root, "seek", "filing_date", "--activate")
    assert sought["ok"], label
    screen = sought["result"]
    _assert_question_screen(screen, case)

    missing = _run(
        interpreter,
        root,
        "seek",
        "no_such_variable_xyz",
        "--fresh",
        expected_code=2,
    )
    assert not missing["ok"], label
    assert missing["error"]["kind"] == "unresolved-variable", label
    assert "no_such_variable_xyz" in missing["error"]["message"], label


def test_foreground_background_action_contract_across_families(
    family_python, real_workspace
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = real_workspace
    _assert_family_interpreter(case)

    checked = _run(interpreter, root, "check", "--interview", "background.yml")
    assert checked["ok"], label
    assert checked["result"]["failures"] == 0, label

    started = _run(interpreter, root, "start", "--interview", "background.yml")
    assert started["ok"], label
    assert started["result"]["kind"] == "question", label

    answered = _run(
        interpreter,
        root,
        "answer",
        "--interview",
        "background.yml",
        "user_name=Ada",
    )
    assert answered["ok"], label
    assert "hi:Ada" in answered["result"].get("subquestion_text", ""), label

    evaluated = _run(
        interpreter, root, "eval", "--interview", "background.yml", "greeting"
    )
    assert evaluated["ok"], label
    assert evaluated["result"]["value"] == "'hi:Ada'", label


def test_real_runtime_rehydrates_helpers_for_every_render_source(
    family_python, real_workspace
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = real_workspace
    _assert_family_interpreter(case)
    snapshot = root / "state.snapshot"
    fresh = root / "fresh.docx"
    fixture = root / "fixture.docx"
    from_snapshot = root / "snapshot.docx"
    saved = root / "saved.docx"

    assert _run(
        interpreter,
        root,
        "render",
        "helpers.docx",
        "--fresh",
        "--no-assemble",
        "--save-snapshot",
        str(snapshot),
        "--output",
        str(fresh),
    )["ok"], label
    assert not list((root / ".simulator" / "sessions").glob("*.pkl")), label
    assert _run(
        interpreter,
        root,
        "render",
        "helpers.docx",
        "--fixture",
        str(root / "fixture.py"),
        "--output",
        str(fixture),
    )["ok"], label
    assert _run(
        interpreter,
        root,
        "render",
        "helpers.docx",
        "--snapshot",
        str(snapshot),
        "--no-assemble",
        "--output",
        str(from_snapshot),
    )["ok"], label
    assert _run(interpreter, root, "start")["ok"], label
    assert _run(
        interpreter,
        root,
        "render",
        "helpers.docx",
        "--no-assemble",
        "--output",
        str(saved),
    )["ok"], label

    for artifact in (fresh, fixture, from_snapshot, saved):
        _assert_helper_output(artifact)


def test_demo_corpus_runner_canary(real_python, tmp_path):
    fixture_root = Path(
        os.environ.get(
            "DASIMULATOR_DEMO_FIXTURES",
            Path(__file__).resolve().parents[2]
            / "docassemble-yaml"
            / "lsp"
            / "tests"
            / "fixtures",
        )
    )
    if not fixture_root.is_dir():
        pytest.skip("demo corpus checkout is not available")
    repository = Path(__file__).resolve().parents[1]
    output = tmp_path / "demo-corpus"
    completed = subprocess.run(
        [
            str(real_python),
            "-m",
            "docassemble_simulator.demo_corpus",
            "--fixtures",
            str(fixture_root),
            "--python",
            str(real_python),
            "--compile",
            "--start",
            "--prepare-runtime-data",
            "--match",
            r"^(yesno|fields|attachment-simple|objects-from-file|age_in_years|sections(-horizontal|-auto-open)?|path-and-mimetype|device(-ip)?|relationships)\.yml$",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=repository,
        env=_environment(),
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary = json.loads((output / "summary.json").read_text())
    assert summary["total"] == 24
    assert summary["unexpected"] == 0


def test_demo_package_compiles_with_all_includes(real_python):
    package_root = (
        Path(
            os.environ.get(
                "DASIMULATOR_DEMO_FIXTURES",
                Path(__file__).resolve().parents[2]
                / "docassemble-yaml"
                / "lsp"
                / "tests"
                / "fixtures",
            )
        )
        / "demo_package"
    )
    if not package_root.is_dir():
        pytest.skip("demo package fixture is not available")

    payload = _run(real_python, package_root, "check")

    assert payload["ok"]
    assert payload["result"]["checked"] == 3
    assert payload["result"]["failures"] == 0


def test_generated_attachment_has_durable_local_uri_and_manifest(
    family_python, real_workspace
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    _assert_family_interpreter(case)
    payload = _run(
        interpreter,
        real_workspace,
        "start",
        "--interview",
        "download.yml",
    )

    assert payload["ok"], label
    assert 'href="None"' not in payload["result"]["subquestion_text"], label
    assert 'href="file://' in payload["result"]["subquestion_text"], label
    assert len(payload["attachments"]) == 1, label
    attachment = payload["attachments"][0]
    assert attachment["filename"].lower() == "local_document.docx", label
    assert Path(attachment["path"]).is_file(), label
    assert attachment["uri"] == Path(attachment["path"]).resolve().as_uri(), label
    index = real_workspace / ".simulator" / "files" / "index.json"
    assert index.is_file(), label

    refreshed = _run(
        interpreter,
        real_workspace,
        "refresh",
        "--interview",
        "download.yml",
    )
    assert refreshed["ok"], label
    assert refreshed["attachments"][0]["uri"] == attachment["uri"], label


def test_real_date_answer_formats_and_rejections_roll_back(
    family_python, real_workspace
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    _assert_family_interpreter(case)
    root = real_workspace
    assert _run(interpreter, root, "start")["ok"], label
    session = next((root / ".simulator" / "sessions").glob("*.pkl"))
    before = session.read_bytes()

    rejected = _run(
        interpreter,
        root,
        "answer",
        "filing_date=2026-02-30",
        "caption=changed",
        expected_code=2,
    )
    assert rejected["error"]["kind"] == "answer-input", label
    assert session.read_bytes() == before, label

    accepted = _run(
        interpreter,
        root,
        "answer",
        "filing_date=2026-08-26",
        "caption=2026-08-26",
    )
    assert accepted["ok"], label
    assert (
        _run(interpreter, root, "eval", "type(filing_date).__name__")["result"]["value"]
        == "'DADateTime'"
    ), label
    assert (
        _run(interpreter, root, "eval", "filing_date.format('MM/dd/yyyy')")["result"][
            "value"
        ]
        == "'08/26/2026'"
    ), label
    assert (
        _run(interpreter, root, "eval", "type(caption).__name__")["result"]["value"]
        == "'str'"
    ), label

    artifact = root / "date.docx"
    assert _run(
        interpreter,
        root,
        "render",
        "date.docx",
        "--no-assemble",
        "--output",
        str(artifact),
    )["ok"], label
    xml = _document_xml(artifact)
    assert "08/26/2026" in xml, label
    assert "2026-08-26" in xml, label

    assert _run(interpreter, root, "start")["ok"], label
    assert _run(
        interpreter,
        root,
        "answer",
        "--code",
        "filing_date='2026-08-26'",
    )["ok"], label
    assert (
        _run(interpreter, root, "eval", "type(filing_date).__name__")["result"]["value"]
        == "'str'"
    ), label
