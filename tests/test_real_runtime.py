from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from zipfile import ZipFile

import pytest

from docassemble_simulator import compatibility

pytestmark = pytest.mark.real_runtime


class RuntimeProbe(str, Enum):
    """Capability marker distinguishing the supported runtime families."""

    MODERN = "modern"
    LEGACY = "legacy"
    UNKNOWN = "unknown"


MODERN = RuntimeProbe.MODERN
LEGACY = RuntimeProbe.LEGACY
UNKNOWN = RuntimeProbe.UNKNOWN


@dataclass(frozen=True)
class RuntimeFamily:
    """One supported docassemble runtime family under test."""

    label: str  # "1.10+" or "1.9.x" for failure messages
    interpreter: Path
    probe: RuntimeProbe


def _family_probe_cmd():
    # Capability probe, not version branching: every supported family
    # provides docassemble.base; the thread_context marker distinguishes the
    # modern interface from the legacy one.
    return (
        "import docassemble.base, importlib.util; "
        "print('modern' if importlib.util.find_spec("
        "'docassemble.base.thread_context') is not None else 'legacy')"
    )


def _query_family(interpreter, *, strict, env_var="") -> RuntimeProbe:
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
    return (
        RuntimeProbe.MODERN
        if completed.stdout.strip() == RuntimeProbe.MODERN.value
        else RuntimeProbe.LEGACY
    )


def _probe_interpreter(env_var, description):
    configured = os.environ.get(env_var)
    interpreter = Path(configured or sys.executable).expanduser().absolute()
    if not interpreter.is_file():
        if configured:
            pytest.fail(f"real-runtime interpreter does not exist: {interpreter}")
        pytest.skip(f"set {env_var} to a {description} target-package interpreter")
    _query_family(interpreter, strict=True, env_var=env_var)
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
    (questions / "generic.yml").write_text(
        "---\n"
        "objects:\n"
        "  - rav: DAObject\n"
        "---\n"
        "generic object: DAObject\n"
        "question: |\n"
        "  What is the date?\n"
        "fields:\n"
        "  - label: no label\n"
        "    field: x.date\n"
        "    datatype: date\n"
        "---\n"
        "mandatory: True\n"
        "question: Start\n"
        "fields:\n"
        "  - Start: start\n"
        "---\n"
        "mandatory: True\n"
        "code: |\n"
        "  rav = DAObject()\n"
        "---\n"
        "mandatory: True\n"
        "question: Summary\n"
        "subquestion: |\n"
        "  Date: ${ rav.date }\n"
    )
    (questions / "generic-nested.yml").write_text(
        "---\n"
        "objects:\n"
        "  - rav: DAObject\n"
        "  - rav.cos: DAObject\n"
        "---\n"
        "generic object: DAObject\n"
        "question: |\n"
        "  What is the date?\n"
        "fields:\n"
        "  - label: no label\n"
        "    field: x.date\n"
        "    datatype: date\n"
        "---\n"
        "mandatory: True\n"
        "question: Start\n"
        "fields:\n"
        "  - Start: start\n"
        "---\n"
        "mandatory: True\n"
        "code: |\n"
        "  rav = DAObject()\n"
        "  rav.cos = DAObject()\n"
        "---\n"
        "mandatory: True\n"
        "question: Summary\n"
        "subquestion: |\n"
        "  ${ rav.cos.date }\n"
    )
    (questions / "generic-code-root.yml").write_text(
        "---\n"
        "code: |\n"
        "  T = DAObject()\n"
        "---\n"
        "objects:\n"
        "  - T.miscellaneous: DAObject\n"
        "  - T.miscellaneous.rav: DAObject\n"
        "---\n"
        "generic object: DAObject\n"
        "question: |\n"
        "  What is the date?\n"
        "fields:\n"
        "  - label: no label\n"
        "    field: x.date\n"
        "    datatype: date\n"
        "---\n"
        "mandatory: True\n"
        "question: Start\n"
        "fields:\n"
        "  - Start: start\n"
        "---\n"
        "mandatory: True\n"
        "question: Summary\n"
        "subquestion: |\n"
        "  ${ T.miscellaneous.rav.date }\n"
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
    (questions / "cyclic.yml").write_text(
        "---\n"
        "mandatory: True\n"
        "code: |\n"
        "  M.a = DAObject()\n"
        "  M.b = DAObject()\n"
        "  M.c = DAObject()\n"
        "  M.d = DAObject()\n"
        "  M.a.b = M.b\n"
        "  M.a.c = M.c\n"
        "  M.a.d = M.d\n"
        "  M.b.a = M.a\n"
        "  M.b.c = M.c\n"
        "  M.b.d = M.d\n"
        "  M.c.a = M.a\n"
        "  M.c.b = M.b\n"
        "  M.c.d = M.d\n"
        "  M.d.a = M.a\n"
        "  M.d.b = M.b\n"
        "  M.d.c = M.c\n"
        "---\n"
        "mandatory: True\n"
        "code: |\n"
        "  no_such_variable_xyz\n"
    )
    (questions / "al-bundle.yml").write_text(
        "---\n"
        "include:\n"
        "  - docassemble.AssemblyLine:assembly_line.yml\n"
        "---\n"
        "objects:\n"
        "  - test_doc: ALDocument.using(title='Test', filename='test', "
        "enabled=True, has_addendum=False)\n"
        "  - test_doc2: ALDocument.using(title='Test Two', filename='test2', "
        "enabled=True, has_addendum=False)\n"
        "  - test_bundle: ALDocumentBundle.using(title='Bundle', "
        "filename='bundle', elements=[test_doc, test_doc2])\n"
        "---\n"
        "attachment:\n"
        "  name: Test document\n"
        "  variable name: test_doc[i]\n"
        "  docx template file: bundle.docx\n"
        "---\n"
        "attachment:\n"
        "  name: Test document two\n"
        "  variable name: test_doc2[i]\n"
        "  docx template file: bundle.docx\n"
        "---\n"
        "mandatory: True\n"
        "question: Name\n"
        "fields:\n"
        "  - Name: bundle_user_name\n"
        "---\n"
        "mandatory: True\n"
        "code: |\n"
        "  test_bundle.generate_downloads_task = background_action("
        "test_bundle.attr_name('create_downloads'))\n"
        "---\n"
        "mandatory: True\n"
        "question: Waiting\n"
        "subquestion: |\n"
        "  ready: ${ test_bundle.generate_downloads_task.ready() }\n"
        "fields:\n"
        "  - Task ready: bundle_task_ready\n"
        "    required: False\n"
        "---\n"
        "mandatory: True\n"
        "question: Done\n"
        "subquestion: |\n"
        "  ${ test_bundle.download_list_html(format='pdf', include_zip=True, "
        "include_full_pdf=True, zip_format='pdf', zip_include_pdf=True) }\n"
    )
    generator = """
import sys
from pathlib import Path
from docx import Document

templates = Path(sys.argv[1])
contents = {
    "helpers.docx": "{{ currency(1234.5) }}|{{ redact('secret') }}|{{ nice_number(2) }}|{{ capitalize('hello') }}|{{ declared_helper('x') }}|{{ format_date(as_datetime('2026-08-26'), 'MM/dd/yyyy') }}",
    "date.docx": "{{ filing_date.format('MM/dd/yyyy') }}|{{ caption }}",
    "bundle.docx": "Generated bundle document for {{ bundle_user_name }}.",
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


BIRTHDATE_METADATA_FIXTURE = (
    "---\n"
    "id: simulator-birthdate-fixture\n"
    "mandatory: True\n"
    "question: |\n"
    "  Compatibility birthdate\n"
    "fields:\n"
    "  - Birthdate: simulator_probe_birthdate\n"
    "    datatype: BirthDate\n"
    "    alMonthLabel: ${ word('Month') }\n"
    "    alDayLabel: ${ word('Day') }\n"
    "    alYearLabel: ${ word('Year') }\n"
)

ASSEMBLYLINE_TARGET_FIXTURE = (
    "---\n"
    "include:\n"
    "  - docassemble.AssemblyLine:assembly_line.yml\n"
    "---\n"
    "objects:\n"
    "  - target_client: DAObject\n"
    "---\n"
    "generic object: DAObject\n"
    "question: |\n"
    "  What is the date?\n"
    "fields:\n"
    "  - label: no label\n"
    "    field: x.date\n"
    "    datatype: date\n"
    "---\n"
    "mandatory: True\n"
    "question: |\n"
    "  AssemblyLine-backed target start\n"
    "fields:\n"
    "  - Name: target_user_name\n"
    "  - Do you want to provide a date?: target_wants_date\n"
    "    datatype: yesno\n"
    "---\n"
    "mandatory: True\n"
    "question: |\n"
    "  Target summary\n"
    "subquestion: |\n"
    "  % if target_wants_date:\n"
    "  ${ target_client.date }\n"
    "  % else:\n"
    "  No date provided.\n"
    "  % endif\n"
)

MISSING_INCLUDE_FIXTURE = (
    "---\n"
    "include:\n"
    "  - docassemble.AssemblyLine:does-not-exist.yml\n"
    "---\n"
    "mandatory: True\n"
    "question: |\n"
    "  Missing include\n"
    "fields:\n"
    "  - Name: missing_user_name\n"
)


@pytest.fixture
def assemblyline_workspace(tmp_path):
    """A minimal AssemblyLine compatibility reproducer plus a target smoke path."""
    package = tmp_path / "docassemble" / "altarget"
    questions = package / "data" / "questions"
    questions.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (questions / "birthdate.yml").write_text(BIRTHDATE_METADATA_FIXTURE)
    (questions / "main.yml").write_text(ASSEMBLYLINE_TARGET_FIXTURE)
    (questions / "missing-include.yml").write_text(MISSING_INCLUDE_FIXTURE)
    return tmp_path


@pytest.fixture
def incompatible_assemblyline_workspace(tmp_path):
    """A workspace-local ALToolbox whose BirthDate lacks mako parameters.

    The installed distribution metadata still marks AssemblyLine as present,
    so the compatibility probe runs and the real compiler rejects the
    standard field metadata.
    """
    package = tmp_path / "docassemble" / "altarget"
    questions = package / "data" / "questions"
    questions.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (questions / "birthdate.yml").write_text(BIRTHDATE_METADATA_FIXTURE)
    toolbox = tmp_path / "docassemble" / "ALToolbox"
    toolbox.mkdir()
    (toolbox / "__init__.py").write_text("")
    (toolbox / "ThreePartsDate.py").write_text(
        "from docassemble.base.util import CustomDataType\n\n\n"
        "class BirthDate(CustomDataType):\n"
        '    name = "BirthDate"\n'
        '    input_type = "BirthDate"\n'
    )
    return tmp_path


def _environment():
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    python_path = [str(root / "src")]
    if environment.get("PYTHONPATH"):
        python_path.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    return environment


def _run(real_python, root, *arguments, expected_code=0, timeout=120):
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
        timeout=timeout,
    )
    assert completed.returncode == expected_code, completed.stdout + completed.stderr
    assert completed.stderr == ""
    return json.loads(completed.stdout)


def _run_human(real_python, root, *arguments, expected_code):
    completed = subprocess.run(
        [
            str(real_python),
            "-m",
            "docassemble_simulator",
            "--root",
            str(root),
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_environment(),
        timeout=180,
    )
    assert completed.returncode == expected_code, completed.stdout + completed.stderr
    return completed.stdout + completed.stderr


def _assemblyline_available(interpreter) -> bool:
    completed = subprocess.run(
        [
            str(interpreter),
            "-c",
            (
                "import importlib.metadata as m; "
                "m.version('docassemble-assemblyline'); "
                "m.version('docassemble-altoolbox')"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


def _module_file(interpreter, module_name) -> Path:
    completed = subprocess.run(
        [
            str(interpreter),
            "-c",
            (
                "import importlib.util as u, sys; "
                f"print(u.find_spec('{module_name}').origin)"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return Path(completed.stdout.strip())


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
    # The minimal interview has no further mandatory questions: answering the
    # Dates screen must reach interview completion on either family (issue #1,
    # story 3: same start command, same outcome shape).
    assert answered["result"]["kind"] == "finished", (label, answered["result"])


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
    diagnostics = missing.get("diagnostics") or []
    assert diagnostics, label
    assert all(entry["kind"] == "variable-seek" for entry in diagnostics), label


def test_cyclic_namespace_reports_unresolved_variable(family_python, real_workspace):
    """A dense object graph must not hide the original assembly failure.

    docassemble serializes the whole namespace while handling the failed
    assembly; without a bounded serializer, a few mutually-referencing
    DAObjects make that walk exponential and the command never returns.
    """
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = real_workspace
    _assert_family_interpreter(case)

    failed = _run(
        interpreter,
        root,
        "start",
        "--interview",
        "cyclic.yml",
        expected_code=2,
        timeout=60,
    )
    assert not failed["ok"], label
    assert failed["error"]["kind"] == "unresolved-variable", label
    assert failed["error"]["details"]["sought_variable"] == "no_such_variable_xyz", (
        label
    )


def test_sessions_are_isolated_by_effective_config(real_python, real_workspace):
    interpreter = real_python
    root = real_workspace
    default_config = root / ".config" / "simulator" / "config.toml"
    default_config.parent.mkdir(parents=True, exist_ok=True)
    default_config.write_text('[jinja-data.category]\nfamily = "Family"\n')
    override = root / "override.toml"
    override.write_text(
        '[jinja-data.category]\nfamily = "Family"\nmiscellaneous = "Miscellaneous"\n'
    )
    identical = root / "override-copy.toml"
    identical.write_text(override.read_text())

    assert _run(interpreter, root, "start")["ok"]
    sessions = root / ".simulator" / "sessions"
    assert len(list(sessions.glob("*.pkl"))) == 1

    isolated = _run(
        interpreter,
        root,
        "status",
        "--config",
        str(override),
        expected_code=1,
    )
    assert not isolated["ok"]
    assert isolated["error"]["kind"] == "state"
    assert "different effective configuration" in isolated["error"]["message"]

    assert _run(interpreter, root, "start", "--config", str(override))["ok"]
    assert len(list(sessions.glob("*.pkl"))) == 2

    assert _run(interpreter, root, "status")["ok"]
    assert _run(interpreter, root, "status", "--config", str(override))["ok"]
    # Identical content from a different path is the same effective config.
    assert _run(interpreter, root, "status", "--config", str(identical))["ok"]

    assert _run(
        interpreter,
        root,
        "answer",
        "--config",
        str(override),
        "filing_date=2026-08-26",
        "caption=2026-08-26",
    )["ok"]
    artifact = root / "override.docx"
    assert _run(
        interpreter,
        root,
        "render",
        "date.docx",
        "--config",
        str(override),
        "--no-assemble",
        "--output",
        str(artifact),
    )["ok"]
    assert artifact.is_file()


def test_generic_object_answers_resolve_through_orig_sought(
    real_python, real_workspace
):
    interpreter = real_python
    root = real_workspace
    assert _run(interpreter, root, "start", "--interview", "generic.yml")["ok"]

    screen = _run(
        interpreter, root, "answer", "--interview", "generic.yml", "start=go"
    )["result"]
    assert screen["kind"] == "question"
    assert screen["sought"].endswith("x.date")
    assert screen["orig_sought"].endswith("rav.date")
    assert screen["fields"][0]["variable"] == "x.date"
    assert screen["fields"][0]["type"] == "date"

    answered = _run(
        interpreter,
        root,
        "answer",
        "--interview",
        "generic.yml",
        "rav.date=2026-10-15",
    )
    assert answered["ok"]
    resolved = _run(
        interpreter,
        root,
        "eval",
        "--interview",
        "generic.yml",
        "type(rav.date).__name__",
    )
    assert resolved["result"]["value"] == "'DADateTime'"

    assert _run(interpreter, root, "start", "--interview", "generic.yml")["ok"]
    assert _run(interpreter, root, "answer", "--interview", "generic.yml", "start=go")[
        "ok"
    ]
    assert _run(
        interpreter, root, "answer", "--interview", "generic.yml", "x.date=2026-10-16"
    )["ok"]
    placeholder = _run(
        interpreter, root, "eval", "--interview", "generic.yml", "rav.date.day"
    )
    assert placeholder["result"]["value"] == "16"


def test_seek_defines_missing_roots_without_persisting(real_python, real_workspace):
    interpreter = real_python
    root = real_workspace
    assert _run(interpreter, root, "start", "--interview", "generic.yml")["ok"]
    session = next((root / ".simulator" / "sessions").glob("*.pkl"))
    before = session.read_bytes()

    sought = _run(interpreter, root, "seek", "--interview", "generic.yml", "rav.date")[
        "result"
    ]
    assert sought["kind"] == "question"
    assert sought["sought"].endswith("x.date")
    assert sought["orig_sought"].endswith("rav.date")
    assert session.read_bytes() == before

    fresh = _run(
        interpreter, root, "seek", "--interview", "generic.yml", "rav.date", "--fresh"
    )["result"]
    assert fresh["kind"] == "question"
    assert fresh["orig_sought"].endswith("rav.date")

    assert _run(
        interpreter,
        root,
        "seek",
        "--interview",
        "generic.yml",
        "rav.date",
        "--activate",
    )["ok"]
    persisted = _run(
        interpreter, root, "eval", "--interview", "generic.yml", "type(rav).__name__"
    )
    assert persisted["result"]["value"] == "'DAObject'"

    assert _run(interpreter, root, "start", "--interview", "generic-nested.yml")["ok"]
    chained = _run(
        interpreter,
        root,
        "seek",
        "--interview",
        "generic-nested.yml",
        "rav.cos.date",
    )["result"]
    assert chained["kind"] == "question"
    assert chained["sought"].endswith("x.date")
    assert chained["orig_sought"].endswith("rav.cos.date")

    assert _run(interpreter, root, "start", "--interview", "generic-code-root.yml")[
        "ok"
    ]
    code_root = _run(
        interpreter,
        root,
        "seek",
        "--interview",
        "generic-code-root.yml",
        "T.miscellaneous.rav.date",
    )["result"]
    assert code_root["kind"] == "question"
    assert code_root["sought"].endswith("x.date")
    assert code_root["orig_sought"].endswith("T.miscellaneous.rav.date")


def test_generic_object_nested_target_resolves_through_seek(
    real_python, real_workspace
):
    interpreter = real_python
    root = real_workspace
    assert _run(interpreter, root, "start", "--interview", "generic-nested.yml")["ok"]
    assert _run(
        interpreter,
        root,
        "exec",
        "--interview",
        "generic-nested.yml",
        "--no-assemble",
        "from docassemble.base.util import DAObject\nrav = DAObject()\nrav.cos = DAObject()",
    )["ok"]

    screen = _run(
        interpreter,
        root,
        "seek",
        "--interview",
        "generic-nested.yml",
        "rav.cos.date",
        "--activate",
    )["result"]
    assert screen["kind"] == "question"
    assert screen["sought"].endswith("x.date")
    assert screen["orig_sought"].endswith("rav.cos.date")

    answered = _run(
        interpreter,
        root,
        "answer",
        "--interview",
        "generic-nested.yml",
        "rav.cos.date=2026-10-16",
    )
    assert answered["ok"]
    resolved = _run(
        interpreter,
        root,
        "eval",
        "--interview",
        "generic-nested.yml",
        "type(rav.cos.date).__name__",
    )
    assert resolved["result"]["value"] == "'DADateTime'"


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


@pytest.mark.corpus
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
            "--jobs",
            os.environ.get("DASIMULATOR_CORPUS_JOBS", "1"),
            "--nltk-cache-dir",
            os.environ.get("DASIMULATOR_NLTK_CACHE_DIR", str(tmp_path / "nltk-cache")),
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


@pytest.mark.corpus
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
    assert ".pdf" not in payload["result"]["subquestion_text"].lower(), label
    assert len(payload["attachments"]) == 1, label
    attachment = payload["attachments"][0]
    assert attachment["filename"].lower() == "local_document.docx", label
    assert Path(attachment["path"]).is_file(), label
    assert attachment["uri"] == Path(attachment["path"]).resolve().as_uri(), label
    assert not list((real_workspace / ".simulator" / "files").glob("*.pdf")), label
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


def test_assemblyline_bundle_pdf_requests_skip_to_docx(family_python, real_workspace):
    """A PDF bundle request produces DOCX artifacts instead of stalling.

    The interview asks AssemblyLine's ``create_downloads`` background event for
    a PDF bundle.  The simulator has no PDF converter, so the bundle must
    complete with the real DOCX renderings, a DOCX ZIP, and a DOCX merge; no
    ``.pdf`` artifact and no unresolved ``....pdf`` seek may appear.
    """
    case = family_python
    label, interpreter = case.label, case.interpreter
    if not _assemblyline_available(interpreter):
        pytest.skip(f"AssemblyLine is not provisioned for {label}")
    _assert_family_interpreter(case)
    root = real_workspace

    started = _run(interpreter, root, "start", "--interview", "al-bundle.yml")
    assert started["ok"], label
    answered = _run(
        interpreter,
        root,
        "answer",
        "bundle_user_name=Simulator",
        "--interview",
        "al-bundle.yml",
    )
    assert answered["ok"], label
    assert answered["result"]["kind"] == "question", label
    assert any(
        item["kind"] == "pdf-skip" for item in answered.get("diagnostics", [])
    ), label

    ready = _run(
        interpreter,
        root,
        "eval",
        "test_bundle.generate_downloads_task.ready()",
        "--interview",
        "al-bundle.yml",
    )
    assert ready["result"]["value"] == "True", label
    failed = _run(
        interpreter,
        root,
        "eval",
        "test_bundle.generate_downloads_task.failed()",
        "--interview",
        "al-bundle.yml",
    )
    assert failed["result"]["value"] == "False", label

    done = _run(
        interpreter,
        root,
        "answer",
        "bundle_task_ready=True",
        "--interview",
        "al-bundle.yml",
    )
    assert done["ok"], label
    html = done["result"]["subquestion_text"]
    assert "file://" in html, label
    assert ".docx" in html, label
    assert ".zip" in html, label
    assert ".pdf" not in html.lower(), label
    published = {item["extension"] for item in done["attachments"]}
    assert "docx" in published, label
    files = sorted(path.suffix for path in (root / ".simulator" / "files").glob("*"))
    assert ".pdf" not in files, label
    assert ".docx" in files and ".zip" in files, label


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


def test_assemblyline_birthdate_metadata_compiles_and_starts(
    family_python, assemblyline_workspace
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    if not _assemblyline_available(interpreter):
        pytest.skip(f"AssemblyLine is not provisioned for {label}")
    root = assemblyline_workspace
    _assert_family_interpreter(case)

    checked = _run(interpreter, root, "check", "--interview", "birthdate.yml")
    assert checked["ok"], label
    assert checked["result"]["failures"] == 0, label

    questions = _run(interpreter, root, "questions", "--interview", "birthdate.yml")
    assert questions["ok"], label
    variables = [
        block["variables"]
        for block in questions["result"]["blocks"]
        if block["variables"]
    ]
    assert ["simulator_probe_birthdate"] in variables, label

    indexed = _run(interpreter, root, "index", "--interview", "birthdate.yml")
    assert indexed["ok"], label
    assert "simulator_probe_birthdate" in indexed["result"]["index"], label

    sessions = root / ".simulator" / "sessions"
    assert not sessions.exists() or not list(sessions.glob("*.pkl")), label

    started = _run(interpreter, root, "start", "--interview", "birthdate.yml")
    assert started["ok"], label
    screen = started["result"]
    assert screen["kind"] == "question", label
    assert screen["fields"][0]["type"] == "BirthDate", label
    assert screen["fields"][0]["variable"] == "simulator_probe_birthdate", label

    info = _run(interpreter, root, "info")
    report = info["result"]["runtime_compatibility"]
    assert report["assemblyline_installed"] is True, label
    assert report["tested_matrix"], label
    assert report["probe"]["capability"] == compatibility.PROBE_CAPABILITY, label


def test_assemblyline_backed_target_and_baseline_compile(
    family_python, assemblyline_workspace
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    if not _assemblyline_available(interpreter):
        pytest.skip(f"AssemblyLine is not provisioned for {label}")
    root = assemblyline_workspace

    target = _run(interpreter, root, "check", "--interview", "main.yml")
    assert target["ok"], label
    assert target["result"]["failures"] == 0, label
    # The include must actually contribute the baseline questions; a bare
    # target file alone would compile with a handful of blocks.
    assert target["result"]["results"][0]["blocks"] > 100, label

    baseline = _run(
        interpreter,
        root,
        "check",
        "--interview",
        "docassemble.AssemblyLine:data/questions/assembly_line.yml",
    )
    assert baseline["ok"], label
    assert baseline["result"]["failures"] == 0, label

    # Default configuration must not miss the PostgreSQL-backed AssemblyLine
    # session metadata write: a fresh start reaches the target screen with no
    # database configured at all.
    started = _run(interpreter, root, "start", "--interview", "main.yml")
    assert started["ok"], label
    screen = started["result"]
    assert screen["kind"] == "question", label
    assert screen["question_text"] == "AssemblyLine-backed target start", label


def test_compatibility_checks_do_not_mutate_packages_or_sessions(
    family_python, assemblyline_workspace
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    if not _assemblyline_available(interpreter):
        pytest.skip(f"AssemblyLine is not provisioned for {label}")
    root = assemblyline_workspace
    repository = Path(__file__).resolve().parents[1]
    lock_before = (repository / "uv.lock").read_bytes()
    toolbox_file = _module_file(interpreter, "docassemble.ALToolbox.ThreePartsDate")
    toolbox_before = toolbox_file.read_bytes()

    for arguments in (
        ("check", "--interview", "birthdate.yml"),
        ("questions", "--interview", "birthdate.yml"),
        ("index", "--interview", "birthdate.yml"),
    ):
        assert _run(interpreter, root, *arguments)["ok"], label

    assert (repository / "uv.lock").read_bytes() == lock_before, label
    assert toolbox_file.read_bytes() == toolbox_before, label
    sessions = root / ".simulator" / "sessions"
    assert not sessions.exists() or not list(sessions.glob("*.pkl")), label


def test_missing_assemblyline_include_is_a_structured_compile_failure(
    family_python, assemblyline_workspace
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = assemblyline_workspace

    payload = _run(
        interpreter,
        root,
        "check",
        "--interview",
        "missing-include.yml",
        expected_code=2,
    )

    assert payload["ok"] is False, label
    assert payload["error"]["kind"] == "compile", label
    assert "does-not-exist.yml" in json.dumps(payload["error"]), label
    sessions = root / ".simulator" / "sessions"
    assert not sessions.exists() or not list(sessions.glob("*.pkl")), label


def test_incompatible_assemblyline_pair_fails_structurally(
    family_python, incompatible_assemblyline_workspace
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    if not _assemblyline_available(interpreter):
        pytest.skip(f"AssemblyLine is not provisioned for {label}")
    root = incompatible_assemblyline_workspace

    checked = _run(
        interpreter,
        root,
        "check",
        "--interview",
        "birthdate.yml",
        expected_code=2,
    )
    assert checked["ok"] is False, label
    assert checked["error"]["kind"] == "runtime-compatibility", label
    details = checked["error"]["details"]
    assert details["failing_capability"] == compatibility.PROBE_CAPABILITY, label
    assert "alMonthLabel" in details["underlying_error"], label
    assert details["recovery"], label
    sessions = root / ".simulator" / "sessions"
    assert not sessions.exists() or not list(sessions.glob("*.pkl")), label

    started = _run(
        interpreter,
        root,
        "start",
        "--interview",
        "birthdate.yml",
        expected_code=2,
    )
    assert started["error"]["kind"] == "runtime-compatibility", label
    assert not sessions.exists() or not list(sessions.glob("*.pkl")), label

    human = _run_human(
        interpreter,
        root,
        "check",
        "--interview",
        "birthdate.yml",
        expected_code=2,
    )
    assert "runtime compatibility failure" in human, label
    assert "failing_capability:" in human, label
    assert details["docassemble_assemblyline"] in human, label
    assert "recovery:" in human, label


WHOLE_SCREEN_FIXTURE = (
    "---\n"
    "id: info\n"
    "mandatory: True\n"
    "question: |\n"
    "  Screen\n"
    "fields:\n"
    "  - Required name: person_name\n"
    "  - Optional nickname: nickname\n"
    "    required: False\n"
    "  - Optional date: extra_date\n"
    "    datatype: date\n"
    "    required: False\n"
    "  - Optional count: count\n"
    "    datatype: integer\n"
    "    required: False\n"
    "  - Optional ratio: ratio\n"
    "    datatype: number\n"
    "    required: False\n"
    "  - Agree: agree\n"
    "    datatype: yesno\n"
    "    required: False\n"
    "  - Declined: declined\n"
    "    datatype: noyes\n"
    "    required: False\n"
    "  - Refer: refer\n"
    "    datatype: yesnoradio\n"
    "    required: False\n"
    "  - Topics: topics\n"
    "    datatype: checkboxes\n"
    "    required: False\n"
    "    choices:\n"
    "      - Housing: housing\n"
    "      - Benefits: benefits\n"
    "  - Required topics: required_topics\n"
    "    datatype: checkboxes\n"
    "    choices:\n"
    "      - Housing: housing\n"
    "      - Benefits: benefits\n"
    "  - Maybe: maybe\n"
    "    datatype: yesnomaybe\n"
    "    required: False\n"
    "  - Three: three\n"
    "    datatype: threestate\n"
    "    required: False\n"
    "  - Empty topics: empty_topics\n"
    "    datatype: checkboxes\n"
    "    required: False\n"
    "    choices: []\n"
    "  - Upload: upload\n"
    "    datatype: file\n"
    "    required: False\n"
    "  - Bad default: bad_default\n"
    "    datatype: date\n"
    "    required: False\n"
    "    default: not-a-date\n"
    "  - Signature: signature_value\n"
    "    datatype: signature\n"
    "    required: True\n"
    "  - Hidden: hidden_value\n"
    "    required: True\n"
    "    show if:\n"
    "      code: |\n"
    "        False\n"
    "---\n"
    "id: summary\n"
    "mandatory: True\n"
    "question: |\n"
    "  Summary\n"
    "subquestion: |\n"
    "  ${ person_name } / ${ nickname } / ${ extra_date } / ${ ratio } /\n"
    "  ${ agree } / ${ declined } / ${ refer } / ${ topics } /\n"
    "  [${ signature_value }]\n"
)


def _whole_screen_workspace(tmp_path):
    package = tmp_path / "docassemble" / "screenanswer"
    questions = package / "data" / "questions"
    questions.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (questions / "main.yml").write_text(WHOLE_SCREEN_FIXTURE)
    return tmp_path


def test_answer_submits_the_whole_screen_across_families(family_python, tmp_path):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = _whole_screen_workspace(tmp_path)

    started = _run(interpreter, root, "start")
    assert started["ok"] and started["result"]["question_name"] == "ID info", label

    missing = _run(
        interpreter,
        root,
        "answer",
        "extra_date=2026-01-01",
        expected_code=2,
    )
    assert missing["error"]["kind"] == "validation", (label, missing)
    assert any(
        "person_name" in item for item in missing["error"]["details"]["errors"]
    ), label
    still_open = _run(interpreter, root, "status")
    assert still_open["result"]["question_name"] == "ID info", label

    off_screen = _run(
        interpreter,
        root,
        "answer",
        "person_name=Alice",
        "off_screen=1",
        expected_code=2,
    )
    assert off_screen["error"]["kind"] == "answer-input", (label, off_screen)
    assert any(
        "off_screen" in item for item in off_screen["error"]["details"]["errors"]
    ), label

    # A required checkbox group with every choice false cannot be submitted;
    # the browser requires at least one checked option.
    no_selection = _run(
        interpreter,
        root,
        "answer",
        "person_name=Alice",
        'required_topics={"housing": false, "benefits": false}',
        expected_code=2,
    )
    assert no_selection["error"]["kind"] == "validation", (label, no_selection)
    assert any(
        "required_topics" in item and "at least one" in item
        for item in no_selection["error"]["details"]["errors"]
    ), label

    accepted = _run(
        interpreter,
        root,
        "answer",
        "person_name=Alice",
        'required_topics={"housing": true}',
    )
    assert accepted["ok"], label
    # Optional fields were submitted as browser blanks, so the flow reaches the
    # summary instead of re-seeking the same screen.
    assert accepted["result"]["question_name"] == "ID summary", (label, accepted)
    # Untouched inputs keep the value the browser would post: an empty string
    # for text and dates, and zero for an empty integer.
    assert _run(interpreter, root, "eval", "nickname")["result"]["value"] == ("''"), (
        label
    )
    assert _run(interpreter, root, "eval", "extra_date")["result"]["value"] == ("''"), (
        label
    )
    assert _run(interpreter, root, "eval", "count")["result"]["value"] == "0", label
    assert _run(interpreter, root, "eval", "ratio")["result"]["value"] == ("0.0"), label
    # Checkbox-style booleans take their unchecked value; noyes is True.
    assert _run(interpreter, root, "eval", "agree is False")["result"]["value"] == (
        "True"
    ), label
    assert _run(interpreter, root, "eval", "declined is True")["result"]["value"] == (
        "True"
    ), label
    assert _run(interpreter, root, "eval", "refer is None")["result"]["value"] == (
        "True"
    ), label
    # An untouched checkbox group is a DADict with every choice false.
    assert (
        _run(interpreter, root, "eval", "type(topics).__name__")["result"]["value"]
        == "'DADict'"
    ), label
    assert _run(interpreter, root, "eval", "len(topics)")["result"]["value"] == "2", (
        label
    )
    assert (
        _run(interpreter, root, "eval", "list(topics.true_values())")["result"]["value"]
        == "[]"
    ), label
    # Gatherable objects repr inside the runtime context instead of failing.
    topics_repr = _run(interpreter, root, "eval", "topics")["result"]["value"]
    assert "housing" in topics_repr, label
    assert "unrepr-able" not in topics_repr, label
    # Radio/maybe/threestate selections and empty checkbox lists are None, an
    # untouched file is None, and an unparseable default falls back to the
    # datatype's blank.
    for variable in ("maybe", "three", "empty_topics", "upload"):
        assert (
            _run(interpreter, root, "eval", f"{variable} is None")["result"]["value"]
            == "True"
        ), (label, variable)
    assert _run(interpreter, root, "eval", "bad_default")["result"]["value"] == (
        "''"
    ), label
    # Signatures are never required; they become DAEmpty so documents render,
    # and hidden fields are neither assigned nor required.
    assert (
        _run(interpreter, root, "eval", "type(signature_value).__name__")["result"][
            "value"
        ]
        == "'DAEmpty'"
    ), label
    assert (
        _run(interpreter, root, "eval", "str(signature_value)")["result"]["value"]
        == "''"
    ), label
    hidden = _run(interpreter, root, "eval", "hidden_value", expected_code=2)
    assert hidden["error"]["kind"] == "execution", label

    # An explicitly empty submission for a required field is rejected.
    assert _run(interpreter, root, "start")["ok"], label
    empty_required = _run(interpreter, root, "answer", "person_name=", expected_code=2)
    assert empty_required["error"]["kind"] == "validation", (label, empty_required)
    assert any(
        "person_name" in item and "is empty" in item
        for item in empty_required["error"]["details"]["errors"]
    ), label

    # Explicitly empty submissions follow the same per-datatype conversion.
    assert _run(interpreter, root, "start")["ok"], label
    emptied = _run(
        interpreter,
        root,
        "answer",
        "person_name=Alice",
        'required_topics={"housing": true}',
        "count=",
        "ratio=",
        "agree=",
    )
    assert emptied["ok"], label
    assert _run(interpreter, root, "eval", "count")["result"]["value"] == "0", label
    assert _run(interpreter, root, "eval", "ratio")["result"]["value"] == ("0.0"), label
    assert _run(interpreter, root, "eval", "agree is None")["result"]["value"] == (
        "True"
    ), label


def test_partial_answer_keeps_the_permissive_mode_across_families(
    family_python, tmp_path
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = _whole_screen_workspace(tmp_path)
    assert _run(interpreter, root, "start")["ok"], label

    permissive = _run(
        interpreter,
        root,
        "answer",
        "extra_date=2026-01-01",
        "--partial",
    )

    assert permissive["ok"], label
    assert permissive["result"]["question_name"] == "ID info", (label, permissive)
    assert any("person_name" in item for item in permissive["result"]["warnings"]), (
        label
    )
    undefined = _run(interpreter, root, "eval", "nickname", expected_code=2)
    assert undefined["error"]["kind"] == "execution", label
