"""Selected null radio choices through ordinary worker answers and native replay (#83)."""

from __future__ import annotations

import textwrap

import pytest
from test_real_runtime import (  # noqa: F401 - fixtures registered by import
    family_python,
    real_python,
    real_python_19,
    worker_journey,
)

from docassemble_simulator.trace import load_trace

pytestmark = pytest.mark.real_runtime

INTERVIEW = """\
---
objects:
  - rav: DAObject
---
mandatory: True
code: |
  rav.election
---
id: election
generic object: DAObject
question: Pregnancy election
fields:
  - Election: x.election
    datatype: threestate
    input type: radio
    choices:
      - Yes: True
      - No: False
      - Not applicable: null
validation code: |
  validated = x.election
---
id: guardian
mandatory: True
question: Guardian choice
fields:
  - Guardian: guardian
"""


def _journey(tmp_path, family, start_worker, interview=INTERVIEW):
    package = tmp_path / "docassemble" / "issue83"
    questions = package / "data" / "questions"
    questions.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (questions / "main.yml").write_text(textwrap.dedent(interview))
    return start_worker(
        family.interpreter,
        tmp_path,
        family=family.probe,
        arguments=("--offline", "--seek-diagnostics", "off"),
    )


@pytest.mark.parametrize("name", ["x.election", "rav.election"])
@pytest.mark.parametrize("raw", ["None", "null", "True", "False"])
def test_required_offered_null_radio_answer_advances(
    family_python,  # noqa: F811 - imported fixture
    tmp_path,
    worker_journey,  # noqa: F811 - imported fixture
    name,
    raw,
):
    journey = _journey(tmp_path, family_python, worker_journey)
    started = journey.start()
    assert started["ok"], started
    field = started["result"]["fields"][0]
    assert field["type"] == "threestate" and field["required"], field
    assert field["input_type"] == "radio", field
    assert [choice["value"] for choice in field["choices"]] == ["True", "False", None]

    accepted = journey.answer(f"{name}={raw}")
    assert accepted["ok"], accepted
    assert accepted["result"]["question_text"] == "Guardian choice"
    expected = "None" if raw in {"None", "null"} else raw
    assert journey.evaluate("rav.election")["result"]["value"] == expected
    assert journey.evaluate("validated")["result"]["value"] == expected


def _saved_sessions(root):
    return {
        path.name: path.read_bytes()
        for path in (root / ".simulator" / "sessions").glob("*.pkl")
    }


@pytest.mark.parametrize(
    "datatype", ["threestate", "no-null", "text", "integer", "number"]
)
def test_required_unanswered_or_blank_rejects_without_changing_session(
    family_python,  # noqa: F811 - imported fixture
    tmp_path,
    worker_journey,  # noqa: F811 - imported fixture
    datatype,
):
    interview = INTERVIEW
    if datatype == "no-null":
        interview = interview.replace("      - Not applicable: null\n", "")
    elif datatype != "threestate":
        interview = interview.replace(
            "    datatype: threestate\n    input type: radio\n    choices:\n"
            "      - Yes: True\n      - No: False\n      - Not applicable: null\n",
            f"    datatype: {datatype}\n",
        )
    journey = _journey(tmp_path, family_python, worker_journey, interview)
    assert journey.start()["ok"]
    before = journey.status()["result"]
    saved = _saved_sessions(tmp_path)
    attempts = [(), ("x.election=",), ("rav.election=   ",)]
    if datatype != "threestate":
        attempts.extend([("x.election=None",), ("rav.election=null",)])
    for assignments in attempts:
        rejected = journey.answer(*assignments, defaults=not assignments)
        assert not rejected["ok"] and rejected["error"]["kind"] == "validation", (
            rejected
        )
        assert rejected["error"]["details"]["missing_required"] == ["x.election"]
        assert journey.status()["result"] == before
        assert _saved_sessions(tmp_path) == saved
        assert (
            journey.evaluate("(defined('rav.election'), defined('validated'))")[
                "result"
            ]["value"]
            == "(False, False)"
        )

    if datatype == "threestate":
        omitted_record = tmp_path / "omitted.trace.jsonl"
        assert journey.request("start", record=str(omitted_record))["ok"]
        omitted = journey.request(
            "seek",
            variable="rav.election",
            auto=True,
            activate=True,
            record=str(omitted_record),
        )
        assert omitted["result"]["auto_stop"]["missing_required"] == ["x.election"], (
            omitted
        )
        assert omitted["result"]["auto_stop"]["submitted"] is False
        saved = _saved_sessions(tmp_path)
        replayed = journey.request("trace-replay", golden=str(omitted_record))
        assert replayed["ok"], replayed
        assert _saved_sessions(tmp_path) == saved

    # The native replay adapter must reject the same explicit blanks, not just
    # the ordinary answer adapter. Record a legitimate answer, then override it.
    record = tmp_path / "blank.trace.jsonl"
    assert journey.request("start", record=str(record))["ok"]
    valid = "True" if datatype in {"threestate", "no-null"} else "2"
    assert journey.request(
        "answer", assignments=[f"x.election={valid}"], record=str(record)
    )["ok"]
    saved = _saved_sessions(tmp_path)
    seq = next(
        entry.seq for entry in load_trace(record).entries if entry.operation == "answer"
    )
    for raw in ("",) if datatype == "threestate" else ("", "None", "null"):
        replayed = journey.request(
            "trace-replay", golden=str(record), overrides=[f"{seq}.x.election={raw}"]
        )
        assert not replayed["ok"] and replayed["error"]["kind"] == "trace-mismatch", (
            replayed
        )
        assert "missing_required" in str(replayed), replayed
        assert _saved_sessions(tmp_path) == saved


def test_input_rejection_guidance_counts_valid_null_selection(
    family_python,  # noqa: F811 - imported fixture
    tmp_path,
    worker_journey,  # noqa: F811 - imported fixture
):
    journey = _journey(tmp_path, family_python, worker_journey)
    assert journey.start()["ok"]
    before = journey.status()["result"]
    saved = _saved_sessions(tmp_path)
    rejected = journey.answer("rav.election=None", "off_screen=1")
    assert not rejected["ok"] and rejected["error"]["kind"] == "answer-input", rejected
    assert rejected["error"]["details"]["missing_required"] == [], rejected
    assert journey.status()["result"] == before
    assert _saved_sessions(tmp_path) == saved
    assert (
        journey.evaluate("(defined('rav.election'), defined('validated'))")["result"][
            "value"
        ]
        == "(False, False)"
    )


@pytest.mark.parametrize("gate", ["constraints", "authored"])
def test_selected_null_still_runs_remaining_acceptance_gates_transactionally(
    family_python,  # noqa: F811 - imported fixture
    tmp_path,
    worker_journey,  # noqa: F811 - imported fixture
    gate,
):
    interview = INTERVIEW.replace(
        "validation code: |",
        "  - Quantity: quantity\n    datatype: integer\n    min: 2\nvalidation code: |",
    ).replace(
        "  validated = x.election",
        "  validated = x.election\n  if x.election is None:\n    validation_error('Null is disallowed by author')",
    )
    journey = _journey(tmp_path, family_python, worker_journey, interview)
    assert journey.start()["ok"]
    before = journey.status()["result"]
    saved = _saved_sessions(tmp_path)
    rejected = journey.answer(
        "rav.election=None", f"quantity={1 if gate == 'constraints' else 2}"
    )
    assert not rejected["ok"] and rejected["error"]["kind"] == "validation", rejected
    errors = str(rejected["error"]["details"]["errors"])
    assert (
        "min constraint" if gate == "constraints" else "Null is disallowed by author"
    ) in errors
    assert journey.status()["result"] == before
    assert _saved_sessions(tmp_path) == saved
    assert (
        journey.evaluate(
            "(defined('rav.election'), defined('quantity'), defined('validated'))"
        )["result"]["value"]
        == "(False, False, False)"
    )
    assert journey.answer("x.election=True", "quantity=2")["ok"]


@pytest.mark.parametrize("mode", ["answer", "automatic"])
@pytest.mark.parametrize("name", ["x.election", "rav.election"])
def test_native_replay_null_choices_and_overrides(
    family_python,  # noqa: F811 - imported fixture
    tmp_path,
    worker_journey,  # noqa: F811 - imported fixture
    mode,
    name,
):
    # The automatic golden uses a browser-prefilled True; replay's override
    # must preserve an explicit null selection rather than restore that default.
    marker = tmp_path / "authored-selection.txt"
    interview = INTERVIEW.replace(
        "  validated = x.election",
        "  validated = x.election\n"
        "  from pathlib import Path\n"
        f"  Path({str(marker)!r}).write_text(repr(x.election))",
    )
    if mode == "automatic":
        interview = interview.replace(
            "    datatype: threestate", "    default: True\n    datatype: threestate"
        )
        interview += "---\ncode: |\n  replay_target = (rav.election, guardian)\n"
    journey = _journey(tmp_path, family_python, worker_journey, interview)
    record = tmp_path / "null.trace.jsonl"
    assert journey.request("start", record=str(record))["ok"]
    if mode == "answer":
        recorded = journey.request(
            "answer", assignments=[f"{name}=None"], record=str(record)
        )
    else:
        recorded = journey.request(
            "seek",
            variable="replay_target",
            auto=True,
            activate=True,
            record=str(record),
        )
    assert recorded["ok"], recorded
    assert recorded["result"]["question_text"] == "Guardian choice"
    entries = load_trace(record).entries
    submitted = next(
        entry
        for entry in entries
        if entry.operation == "answer" or entry.intent == "auto"
    )
    golden = record.read_bytes()
    saved = _saved_sessions(tmp_path)
    marker.unlink()
    replayed = journey.request("trace-replay", golden=str(record))
    assert replayed["ok"], replayed
    assert marker.read_text() == ("None" if mode == "answer" else "True")
    for raw in ("None", "null", "True", "False"):
        marker.unlink()
        replayed = journey.request(
            "trace-replay",
            golden=str(record),
            overrides=[f"{submitted.seq}.{name}={raw}"],
        )
        assert replayed["ok"], replayed
        assert marker.read_text() == ("None" if raw in {"None", "null"} else raw)
    marker.unlink()
    rejected = journey.request(
        "trace-replay", golden=str(record), overrides=[f"{submitted.seq}.{name}="]
    )
    assert not rejected["ok"] and rejected["error"]["kind"] == "trace-mismatch", (
        rejected
    )
    assert "required fields missing" in str(rejected) or "missing_required" in str(
        rejected
    ), rejected
    assert not marker.exists()  # Required rejection stops before authored validation.
    assert record.read_bytes() == golden
    assert _saved_sessions(tmp_path) == saved
    assert journey.status()["result"]["question_text"] == "Guardian choice"
