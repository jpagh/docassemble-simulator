"""Real-runtime acceptance for screen traces (issue #7).

A scenario is driven twice through the CLI with ``--record`` and compared;
then the interview is edited (insert, reorder, rename) and the same trace
comparison reports the intended verdict on both runtime families.
"""

from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest
from test_real_runtime import (  # noqa: F401 - fixtures registered by import
    _environment,
    family_python,
    real_python,
    real_python_19,
)

from docassemble_simulator.trace import (
    ComparePolicy,
    compare_traces,
    load_exceptions,
    load_trace,
    screen_identity,
)

pytestmark = pytest.mark.real_runtime

INTERVIEW = "docassemble.tracefix:data/questions/main.yml"
PHASE_ORDER = ("intake", "people", "close")

PHASES = {
    "id:intro": "intake",
    "fields:favorite_color": "intake",
    "fields:chosen_color": "intake",
    "id:any clients": "people",
    "id:client names": "people",
    "id:another client": "people",
    "generic:contacts.name": "people",
    "id:gather checkpoint": "close",
    "id:review": "close",
    "screen:question/deadend": "close",
}

ANSWERS = {
    "id:intro": [{"user_name": "Jack"}],
    "fields:favorite_color": [
        {"favorite_color": "red"},
        {"favorite_color": "blue"},
    ],
    "fields:chosen_color": [
        {"chosen_color": "red"},
        {"chosen_color": "blue"},
    ],
    "id:any clients": [{"clients.there_are_any": "true"}],
    "id:client names": [
        {"clients[i].name": "Alice", "clients[i].complete": "true"},
        {"clients[i].name": "Bob", "clients[i].complete": "true"},
    ],
    "id:another client": [
        {"clients.there_is_another": "true"},
        {"clients.there_is_another": "false"},
    ],
    "generic:contacts.name": [{"x.name": "Carol"}, {"x.name": "Dave"}],
    "id:gather checkpoint": [{"gather_ok": "true"}],
    "id:review": [{"review_ok": "true"}],
}

INTERVIEW_HEADER = """\
---
objects:
  - clients: DAList.using(object_type=DAObject, complete_attribute='complete')
  - contacts: DAList.using(object_type=DAObject)
---
id: intro
mandatory: True
question: |
  Intro
fields:
  - Your name: user_name
---
mandatory: True
question: |
  Favorite color
fields:
  - Color: favorite_color
validation code: |
  if favorite_color == "red":
    validation_error("red is not available")
"""

ANY_CLIENTS_BLOCK = """\
---
id: any clients
question: |
  Are there any clients?
fields:
  - no label: clients.there_are_any
    datatype: yesno
"""

CLIENT_LOOP_BLOCK = """\
---
sets:
  - clients[i].name
  - clients[i].complete
id: client names
question: |
  Client ${ i + 1 } name
fields:
  - Name: clients[i].name
  - Clients complete: clients[i].complete
    datatype: yesno
    default: True
---
id: another client
question: |
  Another client?
fields:
  - no label: clients.there_is_another
    datatype: yesno
"""

CHECKPOINT_BLOCK = """\
---
id: gather checkpoint
mandatory: True
question: |
  Preparing your documents
subquestion: |
  ${ clients } ${ contacts[0].name } ${ contacts[1].name }
continue button field: gather_ok
"""

GENERIC_BLOCK = """\
---
generic object: DAObject
question: |
  Contact name
fields:
  - label: no label
    field: x.name
    required: False
"""

REVIEW_BLOCK = """\
---
id: review
mandatory: True
question: |
  Review
continue button field: review_ok
---
mandatory: True
question: |
  Done
subquestion: |
  ${ user_name }
"""

BASE_INTERVIEW = (
    INTERVIEW_HEADER
    + ANY_CLIENTS_BLOCK
    + CLIENT_LOOP_BLOCK
    + GENERIC_BLOCK
    + CHECKPOINT_BLOCK
    + REVIEW_BLOCK
)

REORDERED_INTERVIEW = (
    INTERVIEW_HEADER.replace(
        """---
id: intro
mandatory: True
question: |
  Intro
fields:
  - Your name: user_name
---
mandatory: True
question: |
  Favorite color
fields:
  - Color: favorite_color
validation code: |
  if favorite_color == "red":
    validation_error("red is not available")
""",
        """---
mandatory: True
question: |
  Favorite color
fields:
  - Color: favorite_color
validation code: |
  if favorite_color == "red":
    validation_error("red is not available")
---
id: intro
mandatory: True
question: |
  Intro
fields:
  - Your name: user_name
""",
    )
    + ANY_CLIENTS_BLOCK
    + CLIENT_LOOP_BLOCK
    + GENERIC_BLOCK
    + CHECKPOINT_BLOCK
    + REVIEW_BLOCK
)

INSERTED_INTERVIEW = (
    "---\nmandatory: True\ncode: |\n  pointless_flag = True\n" + BASE_INTERVIEW
)


def _write_interview(root: Path, interview: str = BASE_INTERVIEW) -> Path:
    package = root / "docassemble" / "tracefix"
    questions = package / "data" / "questions"
    questions.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (questions / "main.yml").write_text(interview)
    return root


def _command(interpreter, root, *arguments):
    completed = subprocess.run(
        [
            str(interpreter),
            "-m",
            "docassemble_simulator",
            "--root",
            str(root),
            "--offline",
            "--json",
            "--seek-diagnostics",
            "off",
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_environment(),
        timeout=180,
    )
    assert completed.stderr == "", completed.stderr
    assert completed.stdout.strip(), completed.stdout + completed.stderr
    return completed.returncode, json.loads(completed.stdout)


def _drive(interpreter, root: Path, record: Path) -> None:
    """Drive the scenario, keying every answer on the current screen identity."""
    consumed: dict[str, int] = defaultdict(int)

    def screen_for(code, payload):
        if payload.get("ok") and isinstance(payload.get("result"), dict):
            return payload["result"]
        assert code == 2, payload
        assert payload["error"]["kind"] == "validation", payload
        # A failed answer leaves the active screen in place.
        status_code, status = _command(interpreter, root, "status")
        assert status_code == 0, status
        return status["result"]

    code, payload = _command(
        interpreter,
        root,
        "start",
        "--record",
        str(record),
        "--phase",
        "intake",
    )
    assert code == 0, payload

    while True:
        screen = screen_for(code, payload)
        key = screen_identity(screen).key
        options = ANSWERS.get(key)
        if not options:
            return
        index = consumed[key]
        consumed[key] += 1
        if index >= len(options):
            return
        assignments = options[index]
        code, payload = _command(
            interpreter,
            root,
            "answer",
            *(f"{name}={value}" for name, value in assignments.items()),
            "--record",
            str(record),
            "--phase",
            PHASES.get(key, "intake"),
        )


def _record_base(interpreter, root: Path, record: Path) -> None:
    _write_interview(root)
    _drive(interpreter, root, record)


def test_screen_trace_acceptance(family_python, tmp_path):  # noqa: F811
    interpreter = family_python.interpreter

    # 1. Record and replay the base scenario.
    golden = tmp_path / "golden.trace.jsonl"
    replay = tmp_path / "replay.trace.jsonl"
    _record_base(interpreter, tmp_path / "golden", golden)
    _record_base(interpreter, tmp_path / "replay", replay)
    expected = load_trace(golden)
    actual = load_trace(replay)
    identities = [entry.identity.key for entry in expected.entries]

    assert expected.metadata.interview == INTERVIEW
    assert identities[0] == "id:intro"
    assert "fields:favorite_color" in identities
    assert identities.count("id:client names") == 2
    assert identities.count("generic:contacts.name") == 2
    assert "id:review" in identities
    assert "screen:question/deadend" in identities
    assert any(
        entry.ok is False and entry.identity.key == "fields:favorite_color"
        for entry in expected.entries
    )

    ordered = compare_traces(expected, actual)
    unordered = compare_traces(expected, actual, ComparePolicy(order="unordered"))
    phased = compare_traces(
        expected,
        actual,
        ComparePolicy(order="phased", phases=PHASE_ORDER),
    )

    assert ordered.matched is True
    assert unordered.matched is True
    assert phased.matched is True
    assert [phase.phase for phase in phased.phases] == list(PHASE_ORDER)
    assert phased.phases[0].expected_count == 4
    assert phased.phases[1].expected_count == 7
    assert phased.phases[2].expected_count == 2

    # 2. Insert an earlier block: generated names shift, identities do not.
    inserted_root = _write_interview(tmp_path / "inserted", INSERTED_INTERVIEW)
    inserted_trace = tmp_path / "inserted.trace.jsonl"
    _drive(interpreter, inserted_root, inserted_trace)
    inserted = load_trace(inserted_trace)
    expected_names = [
        entry.screen.get("question_name")
        for entry in expected.entries
        if entry.screen is not None
    ]
    actual_names = [
        entry.screen.get("question_name")
        for entry in inserted.entries
        if entry.screen is not None
    ]
    assert expected_names != actual_names
    assert compare_traces(expected, inserted).matched is True

    # 3. Reorder two intake questions: ordered fails, phased passes.
    reordered_root = _write_interview(tmp_path / "reordered", REORDERED_INTERVIEW)
    reordered_trace = tmp_path / "reordered.trace.jsonl"
    _drive(interpreter, reordered_root, reordered_trace)
    reordered = load_trace(reordered_trace)
    reordered_ordered = compare_traces(expected, reordered)
    reordered_phased = compare_traces(
        expected,
        reordered,
        ComparePolicy(order="phased", phases=PHASE_ORDER),
    )
    assert reordered_ordered.matched is False
    assert any(diff.kind == "order" for diff in reordered_ordered.diffs)
    assert reordered_ordered.missing_count == 0
    assert reordered_ordered.extra_count == 0
    assert reordered_phased.matched is True

    # 4. Rename a variable: identity coverage changes; exceptions honor it.
    renamed_root = _write_interview(
        tmp_path / "renamed",
        BASE_INTERVIEW.replace("favorite_color", "chosen_color"),
    )
    renamed_trace = tmp_path / "renamed.trace.jsonl"
    _drive(interpreter, renamed_root, renamed_trace)
    renamed = load_trace(renamed_trace)
    comparison = compare_traces(expected, renamed, ComparePolicy(order="unordered"))
    assert comparison.matched is False
    missing = [diff for diff in comparison.diffs if diff.kind == "missing"]
    extra = [diff for diff in comparison.diffs if diff.kind == "extra"]
    assert [diff.identity for diff in missing] == ["fields:favorite_color"]
    assert [diff.identity for diff in extra] == ["fields:chosen_color"]

    exceptions_file = tmp_path / "exceptions.toml"
    exceptions_file.write_text(
        "[[exceptions]]\n"
        f'interview = "{INTERVIEW}"\n'
        'phase = "intake"\n'
        'identity = "fields:favorite_color"\n'
        'category = "capability-boundary"\n'
        'reason = "renamed by design"\n'
        "[[exceptions]]\n"
        f'interview = "{INTERVIEW}"\n'
        'phase = "intake"\n'
        'identity = "fields:chosen_color"\n'
        'category = "capability-boundary"\n'
        'reason = "renamed by design"\n',
        encoding="utf-8",
    )
    excepted = compare_traces(
        expected,
        renamed,
        ComparePolicy(order="unordered"),
        load_exceptions(exceptions_file),
    )
    assert excepted.matched is True
    # Tolerant matching still reports the coverage change: both the accepted
    # and the validation-re-ask occurrence changed identity.
    assert excepted.missing_count == 2
    assert excepted.extra_count == 2
