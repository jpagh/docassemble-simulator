from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import warnings
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
    (questions / "resolution.yml").write_text(
        "---\n"
        "modules:\n"
        "  - docassemble.regression.helpers\n"
        "---\n"
        "order:\n"
        "  - resolution-helper\n"
        "  - resolution-fallback\n"
        "---\n"
        "id: resolution-helper\n"
        "mandatory: True\n"
        "if: declared_helper('ready') == 'declared:ready'\n"
        "question: Helper-backed candidate\n"
        "fields:\n"
        "  - Value: resolution_value\n"
        "---\n"
        "id: resolution-fallback\n"
        "mandatory: True\n"
        "if: declared_helper('missing') == 'declared:missing'\n"
        "question: Fallback candidate\n"
        "fields:\n"
        "  - Value: resolution_value\n"
    )
    (questions / "resolution-unknown.yml").write_text(
        "---\n"
        "mandatory: True\n"
        "question: Resolution seed\n"
        "fields:\n"
        "  - Seed: resolution_seed\n"
        "---\n"
        "id: unknown-first\n"
        "if: missing_resolution_guard == 'one'\n"
        "question: Unknown first\n"
        "fields:\n"
        "  - Value: unresolved_resolution_value\n"
        "---\n"
        "id: unknown-second\n"
        "if: missing_resolution_guard == 'one'\n"
        "question: Unknown second\n"
        "fields:\n"
        "  - Value: unresolved_resolution_value\n"
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
    # A module warns while it is *compiled*, so a third-party package with an
    # invalid escape sequence or a `break` in a `finally` block writes to
    # stderr on its first import after a reinstall, then stays silent once its
    # bytecode is cached. That is environment noise, not simulator output, and
    # the stderr assertions in _run/_command are about the latter. Appended
    # filters take precedence, so an outer PYTHONWARNINGS setting survives.
    filters = (
        [environment["PYTHONWARNINGS"]] if environment.get("PYTHONWARNINGS") else []
    )
    filters.append("ignore::SyntaxWarning")
    environment["PYTHONWARNINGS"] = ",".join(filters)
    return environment


def _run(
    real_python,
    root,
    *arguments,
    expected_code=0,
    timeout=120,
    stderr_contains=None,
):
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
    if stderr_contains is None:
        assert completed.stderr == ""
    else:
        assert stderr_contains in completed.stderr
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


_WORKER_EXIT = object()

# How long a bounded stderr wait may take before it fails the journey. The
# diagnostic is written by the worker process, not the drain thread, so a
# generous bound only ever costs time on a genuine failure.
_STDERR_WAIT = 30.0

# The interpreter runtime writes this line just before aborting when CPython
# finalizes while a locked C++ object (docassemble's native dependencies) is
# still alive. It happens intermittently on the legacy family and is written
# after the shutdown response, so it is teardown noise from the runtime rather
# than authored interview output: a journey whose worker died this way is not
# held to the empty-stderr guarantee, and the abort is reported as a warning so
# it stays visible in the gate's summary.
_RUNTIME_ABORT = "libc++abi: terminating due to uncaught exception"


class WorkerJourney:
    """One persistent ``sim worker`` process for a branching runtime drive.

    The process compiles the interview once and serves every later operation in
    a fraction of a per-command startup, so a multi-operation scenario costs
    one interpreter start instead of one per command. Requests are written one
    line at a time and one response is read back, so a drive can branch on the
    screen it just received instead of committing to a pre-written journey
    file. Authored output is drained from stderr on a thread so it can neither
    deadlock the pipe nor be mistaken for a protocol frame.

    That drain thread is asynchronous, so ``stderr_text()`` is only a snapshot:
    use ``wait_for_stderr()`` when a response is expected to have produced a
    diagnostic. ``close()`` joins the process and the drain before checking
    stderr, so a journey that produced output nobody expected fails there
    unless the test declared it with ``allow_stderr()``; a worker that aborted
    at interpreter teardown reports its runtime abort as a warning instead
    (see ``_RUNTIME_ABORT``).

    ``SimulatorRunner`` (ADR-0015) is not reused here on purpose. It returns
    transport failures (timeout, crash, unusable frame) as typed ``ok: false``
    responses, which is right for a client and wrong for a contract harness: a
    hung or crashed worker must fail the test with its drained stderr attached,
    not arrive as one more interview-shaped outcome. It also inherits stderr
    rather than capturing it, and it owns workspace isolation, config layers,
    and ``PYTHONUNBUFFERED`` environment handling this harness must not adopt.
    """

    def __init__(
        self,
        interpreter,
        root,
        *,
        interview=None,
        session_root=None,
        arguments=(),
        operation_timeout=120.0,
        response_timeout=120.0,
    ):
        self.interpreter = Path(interpreter)
        self.root = Path(root)
        self.session_root = (
            Path(session_root)
            if session_root is not None
            else self.root / ".simulator" / "sessions"
        )
        self.response_timeout = response_timeout
        self.operation_timeout = operation_timeout
        self._responses: queue.Queue = queue.Queue()
        self._stderr: list[str] = []
        self._allowed_stderr: list[str] = []
        self._aborted_at_exit = False
        self._closed = False
        self._sequence = 0
        command = [
            str(self.interpreter),
            "-m",
            "docassemble_simulator",
            "--root",
            str(self.root),
            *(["--interview", interview] if interview else []),
            *arguments,
            "worker",
            "--session-root",
            str(self.session_root),
            "--timeout",
            str(operation_timeout),
        ]
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=_environment(),
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name="worker-journey-stderr", daemon=True
        )
        self._stdout_thread = threading.Thread(
            target=self._drain_stdout, name="worker-journey-stdout", daemon=True
        )
        self._stderr_thread.start()
        self._stdout_thread.start()

    # -- transport ---------------------------------------------------------
    def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self._stderr.append(line)

    def _drain_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self._responses.put(line)
        self._responses.put(_WORKER_EXIT)

    def stderr_text(self) -> str:
        """A snapshot of the diagnostics drained so far; may still grow."""
        return "".join(self._stderr)

    def wait_for_stderr(self, text: str, *, timeout: float = _STDERR_WAIT) -> str:
        """Return the drained stderr once it contains ``text``.

        A response only proves the operation ran, not that the drain thread has
        consumed the diagnostics it wrote, so this is the deterministic accessor
        for asserting on authored output. It fails the journey with the drained
        stderr attached once the deadline passes or the worker is gone.
        """
        deadline = time.monotonic() + timeout
        while True:
            drained = self.stderr_text()
            if text in drained:
                return drained
            if time.monotonic() >= deadline or not self._stderr_thread.is_alive():
                raise AssertionError(
                    self.failure_message(
                        f"worker stderr never reported {text!r} within {timeout:g}s"
                    )
                )
            time.sleep(0.05)

    def allow_stderr(self, text: str) -> None:
        """Declare authored output this journey is expected to write.

        ``close()`` fails a journey that leaves stderr empty of anything it did
        not expect, which is the guarantee the migrated per-command runs had.
        Tests whose subject *is* a diagnostic declare it here.
        """
        self._allowed_stderr.append(text)

    def assert_no_unexpected_stderr(self) -> None:
        """Fail unless every stderr line was declared with ``allow_stderr``."""
        if self._aborted_at_exit:
            warnings.warn(
                self.failure_message("worker journey aborted at exit"),
                RuntimeWarning,
                stacklevel=2,
            )
            return
        unexpected = [
            line
            for line in self._stderr
            if line.strip()
            and not any(allowed in line for allowed in self._allowed_stderr)
        ]
        assert not unexpected, self.failure_message(
            "unexpected worker stderr:\n" + "".join(unexpected)
        )

    def failure_message(self, reason: str) -> str:
        return (
            f"{reason}\n"
            f"interpreter: {self.interpreter}\n"
            f"worker stderr:\n{self.stderr_text()}"
        )

    def request(self, operation: str, **fields):
        """Send one operation and wait for its single protocol response."""
        self._sequence += 1
        request = {"id": str(self._sequence), "operation": operation, **fields}
        payload = json.dumps(request) + "\n"
        try:
            assert self.process.stdin is not None
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except (BrokenPipeError, ValueError) as error:
            raise AssertionError(
                self.failure_message(
                    f"worker stdin rejected a {operation} request: {error}"
                )
            ) from error
        try:
            line = self._responses.get(timeout=self.response_timeout)
        except queue.Empty:
            raise AssertionError(
                self.failure_message(
                    f"worker did not answer {operation} within "
                    f"{self.response_timeout:g}s"
                )
            )
        if line is _WORKER_EXIT:
            raise AssertionError(
                self.failure_message(f"worker exited before answering {operation}")
            )
        response = json.loads(line)
        assert response["id"] == request["id"], self.failure_message(
            f"worker answered {response['id']!r} for {request['id']!r}"
        )
        return response

    # -- operation helpers -------------------------------------------------
    def start(self):
        return self.request("start")

    def status(self):
        return self.request("status")

    def refresh(self):
        return self.request("refresh")

    def answer(self, *assignments, **options):
        if assignments:
            options["assignments"] = list(assignments)
        return self.request("answer", **options)

    def seek(self, variable, **options):
        return self.request("seek", variable=variable, **options)

    def evaluate(self, expression):
        return self.request("eval", expression=expression)

    def execute(self, code, *, assemble=True):
        return self.request("exec", code=code, assemble=assemble)

    def seed(self, code, *, assemble=False):
        return self.request("seed", code=code, assemble=assemble)

    # -- lifecycle ---------------------------------------------------------
    def _send_shutdown(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            assert self.process.stdin is not None
            self.process.stdin.write('{"id":"shutdown","operation":"shutdown"}\n')
            self.process.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    def close(self) -> None:
        """Stop the worker, drain its streams, and check its stderr."""
        if self._closed:
            return
        self._closed = True
        self._send_shutdown()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        # Join the drains before closing the pipes: once the process is gone
        # every buffered diagnostic is in ``self._stderr``, and the snapshot
        # close() asserts on is then deterministic.
        self._stderr_thread.join(timeout=5)
        self._stdout_thread.join(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self._aborted_at_exit = self.process.returncode is not None and (
            self.process.returncode < 0
            and all(_RUNTIME_ABORT in line for line in self._stderr if line.strip())
        )
        self.assert_no_unexpected_stderr()


@pytest.fixture
def worker_journey():
    """Start persistent workers and close every one, including after failures."""
    journeys: list[WorkerJourney] = []

    def start(interpreter, root, **options):
        journey = WorkerJourney(interpreter, root, **options)
        journeys.append(journey)
        return journey

    try:
        yield start
    finally:
        for journey in journeys:
            journey.close()


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


def test_persistent_worker_and_runner_contract_across_families(
    family_python, real_workspace
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = real_workspace
    _assert_family_interpreter(case)
    session_root = root / f"worker-session-{case.probe.value}"
    requests = [
        {"id": "start", "operation": "start"},
        {
            "id": "print",
            "operation": "exec",
            "code": "print('authored worker output')",
            "assemble": False,
        },
        {"id": "status", "operation": "status"},
        {"id": "shutdown", "operation": "shutdown"},
    ]
    completed = subprocess.run(
        [
            str(interpreter),
            "-m",
            "docassemble_simulator",
            "--root",
            str(root),
            "--offline",
            "--interview",
            "main.yml",
            "run",
            "--session-root",
            str(session_root),
            "--timeout",
            "30",
        ],
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True,
        text=True,
        check=False,
        env=_environment(),
        timeout=180,
    )
    assert completed.returncode == 0, (
        label + "\\n" + completed.stdout + completed.stderr
    )
    frames = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [frame["id"] for frame in frames] == [request["id"] for request in requests]
    assert all(frame["ok"] for frame in frames), label
    assert "authored worker output" in completed.stderr, label

    from docassemble_simulator import SimulatorRunner

    with SimulatorRunner(
        root,
        interview="main.yml",
        python=interpreter,
        isolate=False,
        offline=True,
        timeout=30,
    ) as runner:
        started = runner.start()
        assert started.ok, (label, started)
        assert runner.status().ok, label


class _DeferredStderrJourney(WorkerJourney):
    """A worker journey whose stderr drain thread starts behind a gate.

    Nothing synchronizes the drain thread with the protocol, so a received
    response does not prove its diagnostic has been consumed. Closing the gate
    makes that interleaving deterministic: until the test opens it, no
    diagnostic can be in ``stderr_text()``, and only the bounded
    ``wait_for_stderr()`` accessor can observe output the drain has not read
    yet.
    """

    def __init__(self, *args, **kwargs):
        # The drain thread starts inside the parent constructor, so the gate
        # must exist before it can be consulted.
        self.stderr_gate = threading.Event()
        super().__init__(*args, **kwargs)

    def _drain_stderr(self) -> None:
        self.stderr_gate.wait(timeout=60)
        super()._drain_stderr()


def test_worker_journey_stderr_wait_survives_a_deferred_drain(
    family_python, real_workspace
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = real_workspace
    _assert_family_interpreter(case)
    journey = _DeferredStderrJourney(interpreter, root)
    journey.allow_stderr("late worker diagnostic")
    try:
        assert journey.start()["ok"], label
        executed = journey.execute("print('late worker diagnostic')", assemble=False)
        assert executed["ok"], (label, executed)
        # The premise: the response is in hand and the diagnostic is not.
        assert "late worker diagnostic" not in journey.stderr_text(), label
        journey.stderr_gate.set()
        assert "late worker diagnostic" in journey.wait_for_stderr(
            "late worker diagnostic"
        ), label
    finally:
        journey.stderr_gate.set()
        journey.close()


def test_worker_journey_close_flags_unexpected_stderr(family_python, real_workspace):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = real_workspace
    _assert_family_interpreter(case)
    journey = WorkerJourney(interpreter, root)
    try:
        assert journey.start()["ok"], label
        executed = journey.execute("print('unexpected worker output')", assemble=False)
        assert executed["ok"], (label, executed)
        with pytest.raises(AssertionError, match="unexpected worker stderr"):
            journey.close()
        # A closed journey stays closed, so the fixture's cleanup cannot
        # repeat the failure after the test has observed it.
        journey.close()
    finally:
        journey.close()


def test_catalog_duplicate_relationships_across_families(family_python, real_workspace):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = real_workspace
    _assert_family_interpreter(case)
    questions = root / "docassemble" / "regression" / "data" / "questions"
    (questions / "id-check-source.yml").write_text(
        "---\n"
        "id: cross-file-id\n"
        "question: Source block\n"
        "fields:\n"
        "  - Value: source_value\n"
        "---\n"
        "id: relationship-base\n"
        "question: Base block\n"
        "fields:\n"
        "  - Value: relationship_base\n"
        "---\n"
        "id: relationship-replacement\n"
        "supersedes: relationship-base\n"
        "question: Replacement block\n"
        "fields:\n"
        "  - Value: relationship_replacement\n"
        "---\n"
        "id: unrelated-id\n"
        "question: First unrelated block\n"
        "fields:\n"
        "  - Value: unrelated_first\n"
        "---\n"
        "id: unrelated-id\n"
        "supersedes: missing-target\n"
        "question: Second unrelated block\n"
        "fields:\n"
        "  - Value: unrelated_second\n"
        "---\n"
        "id: mixed-base\n"
        "question: Deliberate base block\n"
        "fields:\n"
        "  - Value: mixed_first\n"
        "---\n"
        "id: mixed-replacement\n"
        "supersedes: mixed-base\n"
        "question: Deliberate replacement block\n"
        "fields:\n"
        "  - Value: mixed_second\n"
        "---\n"
        "id: mixed-replacement\n"
        "question: Accidental third block\n"
        "fields:\n"
        "  - Value: mixed_third\n"
        "---\n"
        "id: unique-id\n"
        "question: Unique block\n"
        "fields:\n"
        "  - Value: unique_value\n"
    )
    target = questions / "id-check-target.yml"
    target.write_text(
        "---\n"
        "include:\n"
        "  - id-check-source.yml\n"
        "---\n"
        "id: cross-file-id\n"
        "question: Included duplicate block\n"
        "fields:\n"
        "  - Value: target_value\n"
    )

    checked = _run(
        interpreter,
        root,
        "check",
        "--interview",
        "id-check-target.yml",
        stderr_contains="warning: reference in a supersedes",
    )
    assert checked["ok"], label
    result = checked["result"]
    reports = result["duplicate_ids"]
    assert result["totals"]["duplicate_ids"] == 3, label
    assert {item["id"] for item in reports} == {
        "cross-file-id",
        "unrelated-id",
        "mixed-replacement",
        "relationship-replacement",
    }, label
    cross_file = next(item for item in reports if item["id"] == "cross-file-id")
    assert {
        block["source_file"].rsplit("/", 1)[-1] for block in cross_file["blocks"]
    } == {"id-check-source.yml", "id-check-target.yml"}, label
    relationship = next(
        item for item in reports if item["id"] == "relationship-replacement"
    )
    assert relationship["kind"] == "deliberate-override", label
    assert [block["id"] for block in relationship["blocks"]] == [
        "relationship-base",
        "relationship-replacement",
    ], label
    assert not any(item["id"] == "unique-id" for item in reports), label

    strict = _run(
        interpreter,
        root,
        "check",
        "--strict",
        "--interview",
        "id-check-target.yml",
        expected_code=2,
        stderr_contains="warning: reference in a supersedes",
    )
    assert strict["error"]["kind"] == "duplicate-id", label
    assert strict["error"]["details"]["totals"]["duplicate_ids"] == 3, label


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


def test_resolution_explanation_is_runtime_ordered_and_read_only(
    family_python, real_workspace
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = real_workspace
    _assert_family_interpreter(case)

    static = _run(
        interpreter,
        root,
        "why",
        "resolution_value",
        "--interview",
        "resolution.yml",
    )
    assert static["ok"], label
    assert static["result"]["session_state"] == "not saved", label
    assert static["result"]["candidates"][0]["winner"] is True, label
    assert static["result"]["candidates"][0]["language"] in {"en", "*"}, label

    started = _run(
        interpreter,
        root,
        "start",
        "--interview",
        "resolution.yml",
    )
    assert started["ok"], label
    assert started["result"]["kind"] == "question", started
    assert started["result"]["question_text"] == "Helper-backed candidate", label
    session = next((root / ".simulator" / "sessions").glob("*.pkl"))
    before = session.read_bytes()
    prepared = _run(
        interpreter,
        root,
        "why",
        "resolution_value",
        "--interview",
        "resolution.yml",
    )
    assert prepared["ok"], label
    report = prepared["result"]
    assert report["session_state"] == "saved", label
    assert report["winner"]["id"] == "resolution-helper", label
    by_id = {candidate["id"]: candidate for candidate in report["candidates"]}
    assert by_id["resolution-helper"]["live"] is True, label
    assert by_id["resolution-fallback"]["live"] is True, label
    assert report["conflict"] is True, label
    assert report["winner"]["provenance"]["source_file"], label
    strict = _run(
        interpreter,
        root,
        "why",
        "resolution_value",
        "--interview",
        "resolution.yml",
        "--strict",
        expected_code=2,
    )
    assert strict["error"]["kind"] == "resolution-conflict", label
    assert session.read_bytes() == before, label


def test_unknown_resolution_overlap_is_conservative_across_families(
    family_python, real_workspace
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = real_workspace
    _assert_family_interpreter(case)

    static = _run(
        interpreter,
        root,
        "why",
        "unresolved_resolution_value",
        "--interview",
        "resolution-unknown.yml",
    )
    assert static["ok"], label
    assert static["result"]["session_state"] == "not saved", label

    started = _run(
        interpreter,
        root,
        "start",
        "--interview",
        "resolution-unknown.yml",
    )
    assert started["ok"], label
    assert started["result"]["question_text"] == "Resolution seed", label
    session = next(
        path
        for path in (root / ".simulator" / "sessions").glob("*.pkl")
        if "resolution-unknown" in path.name
    )
    before = session.read_bytes()
    prepared = _run(
        interpreter,
        root,
        "why",
        "unresolved_resolution_value",
        "--interview",
        "resolution-unknown.yml",
    )
    assert prepared["ok"], label
    report = prepared["result"]
    assert report["conflict"] is True, label
    assert report.get("winner") is None, label
    assert {candidate["state"] for candidate in report["candidates"]} == {"unknown"}, (
        label
    )
    assert all(
        "NameError" in candidate["guard_error"] for candidate in report["candidates"]
    ), label
    strict = _run(
        interpreter,
        root,
        "why",
        "unresolved_resolution_value",
        "--interview",
        "resolution-unknown.yml",
        "--strict",
        expected_code=2,
    )
    assert strict["error"]["kind"] == "resolution-conflict", label
    assert session.read_bytes() == before, label


def test_seek_contract_across_families(family_python, real_workspace, worker_journey):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = real_workspace
    _assert_family_interpreter(case)
    journey = worker_journey(
        interpreter, root, arguments=("--seek-diagnostics", "capture")
    )

    assert journey.start()["ok"], label

    sought = journey.seek("filing_date", activate=True)
    assert sought["ok"], label
    screen = sought["result"]
    _assert_question_screen(screen, case)

    missing = journey.seek("no_such_variable_xyz", fresh=True)
    assert not missing["ok"], label
    assert missing["error"]["kind"] == "unresolved-variable", label
    assert "no_such_variable_xyz" in missing["error"]["message"], label
    diagnostics = missing.get("diagnostics") or []
    assert diagnostics, label
    assert all(entry["kind"] == "variable-seek" for entry in diagnostics), label


def test_terminal_choices_failure_names_the_real_referencing_block(
    family_python, real_workspace, worker_journey
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = real_workspace
    _assert_family_interpreter(case)
    failure_path = (
        root
        / "docassemble"
        / "regression"
        / "data"
        / "questions"
        / "choices-failure.yml"
    )
    failure_path.write_text(
        "---\n"
        "id: choices-failure\n"
        "mandatory: True\n"
        "question: Choose a value\n"
        "fields:\n"
        "  - Choice: choice\n"
        "    code: missing_choices\n"
    )

    failed = worker_journey(
        interpreter,
        root,
        interview="choices-failure.yml",
        arguments=("--seek-diagnostics", "capture"),
    ).start()

    assert failed["error"]["kind"] == "unresolved-variable", label
    details = failed["error"]["details"]
    assert details["sought_variable"] == "missing_choices", label
    assert details["culprit"]["provenance"]["id"] == "choices-failure", label
    assert "missing_choices" in details["culprit"]["expression"], label
    culprit_diagnostics = [
        item
        for item in failed.get("diagnostics", [])
        if item.get("details", {}).get("culprit")
    ]
    assert culprit_diagnostics, label
    assert culprit_diagnostics[-1]["details"]["culprit"] == details["culprit"], label


def test_tolerated_review_reference_is_not_attributed_to_a_later_failure(
    family_python, real_workspace, worker_journey
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = real_workspace
    _assert_family_interpreter(case)
    failure_path = (
        root
        / "docassemble"
        / "regression"
        / "data"
        / "questions"
        / "tolerated-review.yml"
    )
    failure_path.write_text(
        "---\n"
        "id: tolerated-review\n"
        "mandatory: True\n"
        "question: Review\n"
        "review:\n"
        "  - label: Optional review item\n"
        "    field: missing_review\n"
        "continue button field: review_ok\n"
        "---\n"
        "id: actual-terminal\n"
        "mandatory: True\n"
        "question: Choose a value\n"
        "fields:\n"
        "  - Choice: choice\n"
        "    code: missing_terminal\n"
    )
    journey = worker_journey(
        interpreter,
        root,
        interview="tolerated-review.yml",
        arguments=("--seek-diagnostics", "capture"),
    )

    started = journey.start()
    assert started["ok"], label
    assert started["result"]["question_name"] == "ID tolerated-review", label
    # The tolerated review reference is reported to stderr rather than
    # attributed to the later terminal failure. Receiving the response does not
    # prove the drain thread consumed that diagnostic yet, so wait for it.
    journey.allow_stderr("Exception in review block")
    assert "Exception in review block" in journey.wait_for_stderr(
        "Exception in review block"
    ), label

    failed = journey.answer(defaults=True)
    assert failed["error"]["kind"] == "unresolved-variable", label
    culprit = failed["error"]["details"]["culprit"]
    assert culprit["provenance"]["id"] == "actual-terminal", label
    assert culprit["sought_variable"] == "missing_terminal", label
    assert all(
        item.get("details", {}).get("culprit", {}).get("provenance", {}).get("id")
        != "tolerated-review"
        for item in failed.get("diagnostics", [])
        if item.get("details", {}).get("culprit")
    ), label


def test_nested_terminal_seek_names_the_last_real_referencing_block(
    family_python, real_workspace, worker_journey
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = real_workspace
    _assert_family_interpreter(case)
    failure_path = (
        root
        / "docassemble"
        / "regression"
        / "data"
        / "questions"
        / "nested-failure.yml"
    )
    failure_path.write_text(
        "---\n"
        "id: nested-source\n"
        "mandatory: True\n"
        "question: Choose a value\n"
        "fields:\n"
        "  - Choice: choice\n"
        "    code: missing_outer\n"
        "---\n"
        "id: nested-code\n"
        "code: |\n"
        "  missing_outer = missing_terminal\n"
    )

    failed = worker_journey(
        interpreter,
        root,
        interview="nested-failure.yml",
        arguments=("--seek-diagnostics", "capture"),
    ).start()

    assert failed["error"]["kind"] == "unresolved-variable", label
    details = failed["error"]["details"]
    assert details["sought_variable"] == "missing_terminal", label
    assert details["culprit"]["provenance"]["id"] == "nested-code", label
    assert details["culprit"]["expression"] == "missing_outer = missing_terminal", label


def test_auto_seek_submits_browser_defaults_and_completes_across_families(
    family_python, real_workspace, worker_journey
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = real_workspace
    _assert_family_interpreter(case)
    journey = worker_journey(interpreter, root)

    assert journey.start()["ok"], label
    auto = journey.seek("filing_date", auto=True)

    assert auto["ok"], (label, auto)
    assert auto["result"]["auto_submitted"], (label, auto)
    assert auto["result"]["auto_stop"] == {"reason": "interview completed"}, (
        label,
        auto,
    )
    assert auto["result"]["kind"] == "finished", (label, auto)


def test_auto_seek_rejection_is_transactional_and_can_be_answered_after_activation(
    family_python, tmp_path, worker_journey
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = _whole_screen_workspace(tmp_path)
    _assert_family_interpreter(case)
    journey = worker_journey(interpreter, root)

    assert journey.start()["ok"], label
    rejected = journey.seek("person_name", auto=True, activate=True)
    assert rejected["ok"], (label, rejected)
    assert rejected["result"]["auto_submitted"] == [], (label, rejected)
    assert rejected["result"]["auto_stop"]["reason"] == "required fields missing", (
        label,
        rejected,
    )
    assert journey.evaluate("nickname")["error"]["kind"] == "execution", label

    answered = journey.answer(
        "person_name=Alice",
        'required_topics={"housing": true}',
    )
    assert answered["ok"], (label, answered)
    assert answered["result"]["question_name"] == "ID summary", label


def test_cyclic_namespace_reports_unresolved_variable(
    family_python, real_workspace, worker_journey
):
    """A dense object graph must not hide the original assembly failure.

    docassemble serializes the whole namespace while handling the failed
    assembly; without a bounded serializer, a few mutually-referencing
    DAObjects make that walk exponential and the command never returns.
    """
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = real_workspace
    _assert_family_interpreter(case)

    failed = worker_journey(
        interpreter, root, interview="cyclic.yml", operation_timeout=60
    ).start()
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
    real_python, real_workspace, worker_journey
):
    interpreter = real_python
    root = real_workspace
    journey = worker_journey(interpreter, root, interview="generic.yml")
    assert journey.start()["ok"]

    screen = journey.answer("start=go")["result"]
    assert screen["kind"] == "question"
    assert screen["sought"].endswith("x.date")
    assert screen["orig_sought"].endswith("rav.date")
    assert screen["fields"][0]["variable"] == "x.date"
    assert screen["fields"][0]["type"] == "date"

    answered = journey.answer("rav.date=2026-10-15")
    assert answered["ok"]
    resolved = journey.evaluate("type(rav.date).__name__")
    assert resolved["result"]["value"] == "'DADateTime'"

    assert journey.start()["ok"]
    assert journey.answer("start=go")["ok"]
    assert journey.answer("x.date=2026-10-16")["ok"]
    placeholder = journey.evaluate("rav.date.day")
    assert placeholder["result"]["value"] == "16"


def test_seek_defines_missing_roots_without_persisting(
    real_python, real_workspace, worker_journey
):
    interpreter = real_python
    root = real_workspace
    journey = worker_journey(interpreter, root, interview="generic.yml")
    assert journey.start()["ok"]
    session = next((root / ".simulator" / "sessions").glob("*.pkl"))
    before = session.read_bytes()

    sought = journey.seek("rav.date")["result"]
    assert sought["kind"] == "question"
    assert sought["sought"].endswith("x.date")
    assert sought["orig_sought"].endswith("rav.date")
    assert session.read_bytes() == before

    fresh = journey.seek("rav.date", fresh=True)["result"]
    assert fresh["kind"] == "question"
    assert fresh["orig_sought"].endswith("rav.date")

    assert journey.seek("rav.date", activate=True)["ok"]
    persisted = journey.evaluate("type(rav).__name__")
    assert persisted["result"]["value"] == "'DAObject'"

    nested = worker_journey(interpreter, root, interview="generic-nested.yml")
    assert nested.start()["ok"]
    chained = nested.seek("rav.cos.date")["result"]
    assert chained["kind"] == "question"
    assert chained["sought"].endswith("x.date")
    assert chained["orig_sought"].endswith("rav.cos.date")

    code_journey = worker_journey(interpreter, root, interview="generic-code-root.yml")
    assert code_journey.start()["ok"]
    code_root = code_journey.seek("T.miscellaneous.rav.date")["result"]
    assert code_root["kind"] == "question"
    assert code_root["sought"].endswith("x.date")
    assert code_root["orig_sought"].endswith("T.miscellaneous.rav.date")


def test_generic_object_nested_target_resolves_through_seek(
    real_python, real_workspace, worker_journey
):
    interpreter = real_python
    root = real_workspace
    journey = worker_journey(interpreter, root, interview="generic-nested.yml")
    assert journey.start()["ok"]
    assert journey.execute(
        "from docassemble.base.util import DAObject\n"
        "rav = DAObject()\n"
        "rav.cos = DAObject()",
        assemble=False,
    )["ok"]

    screen = journey.seek("rav.cos.date", activate=True)["result"]
    assert screen["kind"] == "question"
    assert screen["sought"].endswith("x.date")
    assert screen["orig_sought"].endswith("rav.cos.date")

    answered = journey.answer("rav.cos.date=2026-10-16")
    assert answered["ok"]
    resolved = journey.evaluate("type(rav.cos.date).__name__")
    assert resolved["result"]["value"] == "'DADateTime'"


def test_foreground_background_action_contract_across_families(
    family_python, real_workspace, worker_journey
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = real_workspace
    _assert_family_interpreter(case)

    checked = _run(interpreter, root, "check", "--interview", "background.yml")
    assert checked["ok"], label
    assert checked["result"]["failures"] == 0, label

    journey = worker_journey(interpreter, root, interview="background.yml")
    started = journey.start()
    assert started["ok"], label
    assert started["result"]["kind"] == "question", label

    answered = journey.answer("user_name=Ada")
    assert answered["ok"], label
    assert "hi:Ada" in answered["result"].get("subquestion_text", ""), label

    evaluated = journey.evaluate("greeting")
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
        "--full-seek-diagnostics",
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


def test_answer_submits_the_whole_screen_across_families(
    family_python, tmp_path, worker_journey
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = _whole_screen_workspace(tmp_path)
    journey = worker_journey(interpreter, root)

    def value(expression):
        response = journey.evaluate(expression)
        assert response["ok"], (label, expression, response)
        return response["result"]["value"]

    started = journey.start()
    assert started["ok"] and started["result"]["question_name"] == "ID info", label

    missing = journey.answer("extra_date=2026-01-01")
    assert missing["error"]["kind"] == "validation", (label, missing)
    assert any(
        "person_name" in item for item in missing["error"]["details"]["errors"]
    ), label
    still_open = journey.status()
    assert still_open["result"]["question_name"] == "ID info", label

    off_screen = journey.answer("person_name=Alice", "off_screen=1")
    assert off_screen["error"]["kind"] == "answer-input", (label, off_screen)
    assert any(
        "off_screen" in item for item in off_screen["error"]["details"]["errors"]
    ), label
    assert off_screen["error"]["details"]["missing_required"] == ["required_topics"], (
        label
    )

    parse_failure = journey.answer("extra_date=not-a-date")
    assert parse_failure["error"]["kind"] == "answer-input", (label, parse_failure)
    assert parse_failure["error"]["details"]["missing_required"] == [
        "person_name",
        "required_topics",
    ], label

    # A required checkbox group with every choice false cannot be submitted;
    # the browser requires at least one checked option.
    no_selection = journey.answer(
        "person_name=Alice",
        'required_topics={"housing": false, "benefits": false}',
    )
    assert no_selection["error"]["kind"] == "validation", (label, no_selection)
    assert any(
        "required_topics" in item and "at least one" in item
        for item in no_selection["error"]["details"]["errors"]
    ), label

    accepted = journey.answer(
        "person_name=Alice",
        'required_topics={"housing": true}',
    )
    assert accepted["ok"], label
    # Optional fields were submitted as browser blanks, so the flow reaches the
    # summary instead of re-seeking the same screen.
    assert accepted["result"]["question_name"] == "ID summary", (label, accepted)
    # Untouched inputs keep the value the browser would post: an empty string
    # for text and dates, and zero for an empty integer.
    assert value("nickname") == "''", label
    assert value("extra_date") == "''", label
    assert value("count") == "0", label
    assert value("ratio") == "0.0", label
    # Checkbox-style booleans take their unchecked value; noyes is True.
    assert value("agree is False") == "True", label
    assert value("declined is True") == "True", label
    assert value("refer is None") == "True", label
    # An untouched checkbox group is a DADict with every choice false.
    assert value("type(topics).__name__") == "'DADict'", label
    assert value("len(topics)") == "2", label
    assert value("list(topics.true_values())") == "[]", label
    # Gatherable objects repr inside the runtime context instead of failing.
    topics_repr = value("topics")
    assert "housing" in topics_repr, label
    assert "unrepr-able" not in topics_repr, label
    # Radio/maybe/threestate selections and empty checkbox lists are None, an
    # untouched file is None, and an unparseable default falls back to the
    # datatype's blank.
    for variable in ("maybe", "three", "empty_topics", "upload"):
        assert value(f"{variable} is None") == "True", (label, variable)
    assert value("bad_default") == "''", label
    # Signatures are never required; they become DAEmpty so documents render,
    # and hidden fields are neither assigned nor required.
    assert value("type(signature_value).__name__") == "'DAEmpty'", label
    assert value("str(signature_value)") == "''", label
    hidden = journey.evaluate("hidden_value")
    assert hidden["error"]["kind"] == "execution", label

    # An explicitly empty submission for a required field is rejected.
    assert journey.start()["ok"], label
    empty_required = journey.answer("person_name=")
    assert empty_required["error"]["kind"] == "validation", (label, empty_required)
    assert any(
        "person_name" in item and "is empty" in item
        for item in empty_required["error"]["details"]["errors"]
    ), label

    # Explicitly empty submissions follow the same per-datatype conversion.
    assert journey.start()["ok"], label
    emptied = journey.answer(
        "person_name=Alice",
        'required_topics={"housing": true}',
        "count=",
        "ratio=",
        "agree=",
    )
    assert emptied["ok"], label
    assert value("count") == "0", label
    assert value("ratio") == "0.0", label
    assert value("agree is None") == "True", label


OBJECT_NAMESPACE_FIXTURE = (
    "---\n"
    "modules:\n"
    "  - docassemble.regression.namespace_aware\n"
    "---\n"
    "objects:\n"
    "  - alice: DAObject\n"
    "  - bob: DAObject\n"
    "  - carol: DAObject\n"
    "---\n"
    "id: shortlist setup\n"
    "mandatory: True\n"
    "code: |\n"
    "  namespace_marker = True\n"
    "  alice.name = 'Alice'\n"
    "  bob.name = 'Bob'\n"
    "  carol.name = 'Carol'\n"
    "  if not defined('shortlist'):\n"
    "      shortlist = NamespaceAwareList('shortlist', auto_gather=False)\n"
    "      shortlist.append(bob)\n"
    "      shortlist.append(carol)\n"
    "---\n"
    "id: shortlist choice\n"
    "mandatory: True\n"
    "question: |\n"
    "  Who belongs on the shortlist?\n"
    "fields:\n"
    "  - Shortlist: shortlist\n"
    "    datatype: object_checkboxes\n"
    "    choices: |\n"
    "      [alice, carol]\n"
)

OBJECT_NAMESPACE_HELPER = (
    "from docassemble.base.functions import get_user_dict\n"
    "from docassemble.base.util import DAList\n"
    "\n"
    "\n"
    "class NamespaceAwareList(DAList):\n"
    '    """A list whose mutations resolve the interview, like walkup references."""\n'
    "\n"
    "    def init(self, *args, **kwargs):\n"
    "        super().init(*args, **kwargs)\n"
    "        self.auto_gather = False\n"
    "\n"
    "    def _require_namespace(self, operation):\n"
    "        if 'namespace_marker' not in get_user_dict():\n"
    "            raise RuntimeError(\n"
    "                'the interview namespace is unavailable to ' + operation\n"
    "            )\n"
    "\n"
    "    def append(self, item):\n"
    "        self._require_namespace('append')\n"
    "        super().append(item)\n"
    "\n"
    "    def remove(self, item):\n"
    "        self._require_namespace('remove')\n"
    "        super().remove(item)\n"
    "\n"
    "    def clear(self):\n"
    "        self._require_namespace('clear')\n"
    "        super().clear()\n"
)


def _object_namespace_workspace(tmp_path, question_fixture=OBJECT_NAMESPACE_FIXTURE):
    package = tmp_path / "docassemble" / "regression"
    questions = package / "data" / "questions"
    questions.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "namespace_aware.py").write_text(OBJECT_NAMESPACE_HELPER)
    (questions / "main.yml").write_text(question_fixture)
    return tmp_path


def test_object_checkboxes_answer_applies_inside_the_interview_namespace(
    family_python, tmp_path, worker_journey
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = _object_namespace_workspace(tmp_path)
    journey = worker_journey(interpreter, root)

    started = journey.start()
    assert started["ok"], (label, started)
    assert started["result"]["question_name"] == "ID shortlist choice", (label, started)
    choices = started["result"]["fields"][0]["choices"]
    assert len(choices) == 2, (label, started)
    alice = choices[0]["value"]

    accepted = journey.answer(f"shortlist={json.dumps([alice])}")

    assert accepted["ok"], (label, accepted)
    assert accepted["result"]["kind"] == "finished", (label, accepted)
    # Bob has no checkbox on the screen, so he survives. Carol is rendered and
    # unticked, so she is removed. Alice is ticked and appended.
    assert (
        journey.evaluate("[item.name for item in shortlist.elements]")["result"][
            "value"
        ]
        == "['Bob', 'Alice']"
    ), label
    assert journey.evaluate("shortlist.gathered")["result"]["value"] == "True", label


EMPTY_CHOICE_OBJECT_FIXTURE = (
    "---\n"
    "id: empty shortlist\n"
    "mandatory: True\n"
    "question: |\n"
    "  Anything to note?\n"
    "fields:\n"
    "  - Note: memo\n"
    "    datatype: text\n"
    "  - Shortlist: shortlist\n"
    "    datatype: object_checkboxes\n"
    "    choices: |\n"
    "      []\n"
)


EMPTY_CHOICE_NAMESPACE_FIXTURE = (
    "---\n"
    "modules:\n"
    "  - docassemble.regression.namespace_aware\n"
    "---\n"
    "id: empty namespace shortlist setup\n"
    "mandatory: True\n"
    "code: |\n"
    "  namespace_marker = True\n"
    "  if not defined('shortlist'):\n"
    "      shortlist = NamespaceAwareList('shortlist', auto_gather=False)\n"
    "---\n"
    "id: empty namespace shortlist\n"
    "mandatory: True\n"
    "question: |\n"
    "  Anything to note?\n"
    "fields:\n"
    "  - Note: memo\n"
    "    datatype: text\n"
    "    required: False\n"
    "  - Shortlist: shortlist\n"
    "    datatype: object_checkboxes\n"
    "    required: False\n"
    "    choices: |\n"
    "      []\n"
)


def _plain_question_workspace(tmp_path, fixture):
    package = tmp_path / "docassemble" / "regression"
    questions = package / "data" / "questions"
    questions.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (questions / "main.yml").write_text(fixture)
    return tmp_path


def test_required_empty_choice_object_group_is_cleared_and_gathered(
    family_python, tmp_path, worker_journey
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = _plain_question_workspace(tmp_path, EMPTY_CHOICE_OBJECT_FIXTURE)
    journey = worker_journey(interpreter, root)

    started = journey.start()
    assert started["ok"], (label, started)
    assert started["result"]["question_name"] == "ID empty shortlist", (label, started)

    accepted = journey.answer("memo=hello")

    assert accepted["ok"], (label, accepted)
    # The group is required (the YAML sets no ``required: False``) and rendered
    # no choices, so the answer path clears and gathers it instead of failing
    # the required gate on a selection it could never receive.
    assert journey.evaluate("len(shortlist)")["result"]["value"] == "0", label
    assert journey.evaluate("shortlist.gathered")["result"]["value"] == "True", label


def test_empty_choice_object_group_clears_inside_the_interview_namespace(
    family_python, tmp_path, worker_journey
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = _object_namespace_workspace(tmp_path, EMPTY_CHOICE_NAMESPACE_FIXTURE)
    journey = worker_journey(interpreter, root)

    started = journey.start()
    assert started["ok"], (label, started)
    assert started["result"]["question_name"] == "ID empty namespace shortlist", (
        label,
        started,
    )

    accepted = journey.answer("memo=hello", "shortlist=[]")

    assert accepted["ok"], (label, accepted)
    # The list existed before the screen and refuses mutations from outside the
    # interview namespace, so clearing it proves the exec'd path.
    assert journey.evaluate("len(shortlist)")["result"]["value"] == "0", label
    assert journey.evaluate("shortlist.gathered")["result"]["value"] == "True", label


def test_partial_answer_keeps_the_permissive_mode_across_families(
    family_python, tmp_path, worker_journey
):
    case = family_python
    label, interpreter = case.label, case.interpreter
    root = _whole_screen_workspace(tmp_path)
    journey = worker_journey(interpreter, root)
    assert journey.start()["ok"], label

    permissive = journey.answer("extra_date=2026-01-01", partial=True)

    assert permissive["ok"], label
    assert permissive["result"]["question_name"] == "ID info", (label, permissive)
    assert any("person_name" in item for item in permissive["result"]["warnings"]), (
        label
    )
    undefined = journey.evaluate("nickname")
    assert undefined["error"]["kind"] == "execution", label


def _worker_fixture(root: Path) -> str:
    package = root / "docassemble" / "worker_probe"
    questions = package / "data" / "questions"
    questions.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (questions / "main.yml").write_text(
        "---\n"
        "mandatory: True\n"
        "code: |\n"
        '  print("AUTHORED-WORKER-OUTPUT")\n'
        "  worker_value\n"
        "---\n"
        "question: Worker value\n"
        "field: worker_value\n",
        encoding="utf-8",
    )
    return "docassemble.worker_probe:data/questions/main.yml"


def test_worker_protocol_keeps_real_interview_output_separate(family_python, tmp_path):
    identity = _worker_fixture(tmp_path)
    journey = tmp_path / "journey.json"
    journey.write_text(
        json.dumps(
            [
                {"id": "one", "operation": "start"},
                {"id": "two", "operation": "status"},
            ]
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            str(family_python.interpreter),
            "-m",
            "docassemble_simulator",
            "--root",
            str(tmp_path),
            "--interview",
            identity,
            "--offline",
            "run",
            "--journey",
            str(journey),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_environment(),
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [response["id"] for response in responses] == ["one", "two"]
    assert all(response["ok"] for response in responses)
    assert "AUTHORED-WORKER-OUTPUT" in completed.stderr


def test_real_worker_timeout_keeps_the_previous_saved_session(family_python, tmp_path):
    package = tmp_path / "docassemble" / "worker_timeout"
    questions = package / "data" / "questions"
    questions.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (questions / "main.yml").write_text(
        "---\n"
        "mandatory: True\n"
        "code: |\n"
        "  counter = 1\n"
        "  done\n"
        "---\n"
        "question: Done\n"
        "field: done\n",
        encoding="utf-8",
    )

    from docassemble_simulator.runner import SimulatorRunner

    with SimulatorRunner(
        tmp_path,
        interview="docassemble.worker_timeout:data/questions/main.yml",
        python=family_python.interpreter,
        offline=True,
        seek_diagnostics="off",
        timeout=30,
    ) as runner:
        started = runner.start()
        assert started.ok, started
        timed_out = runner.request(
            "exec",
            code="counter = 2\nimport time\ntime.sleep(1)",
            assemble=False,
            timeout=0.05,
        )
        assert not timed_out.ok
        assert timed_out.error["kind"] == "timeout"
        assert timed_out.error["details"]["operation"] == "exec"
        assert timed_out.error["details"]["timeout"] == 0.05

        counter = runner.evaluate("counter")
        assert counter.ok, counter
        assert counter.result["value"] == "1"


def test_worker_timeout_is_typed_for_background_action(family_python, tmp_path):
    package = tmp_path / "docassemble" / "background_timeout"
    questions = package / "data" / "questions"
    questions.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (questions / "main.yml").write_text(
        "---\n"
        "mandatory: True\n"
        "code: |\n"
        "  task = background_action('pause')\n"
        "  task.get()\n"
        "---\n"
        "event: pause\n"
        "code: |\n"
        "  import time\n"
        "  time.sleep(1)\n"
        "  background_response('done')\n",
        encoding="utf-8",
    )

    from docassemble_simulator.runner import SimulatorRunner

    with SimulatorRunner(
        tmp_path,
        interview="docassemble.background_timeout:data/questions/main.yml",
        python=family_python.interpreter,
        offline=True,
        seek_diagnostics="off",
        timeout=30,
    ) as runner:
        timed_out = runner.request("start", timeout=0.05)
        assert not timed_out.ok
        assert timed_out.error["kind"] == "timeout"
        assert timed_out.error["details"]["operation"] == "start"
