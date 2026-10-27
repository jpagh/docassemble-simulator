"""Browser-shaped empty selections in real automatic journeys (#86)."""

from __future__ import annotations

import pytest
from empty_control_helpers import saved_sessions, start_empty_control_journey
from test_real_runtime import (  # noqa: F401 - fixtures registered by import
    family_python,
    real_python,
    real_python_19,
    worker_journey,
)
from test_trace_real_runtime import _command

from docassemble_simulator.trace import load_trace

pytestmark = pytest.mark.real_runtime

INTERVIEW = """\
---
objects:
  - candidate: DAObject
  - rav: DAObject
---
mandatory: True
code: |
  rav
---
mandatory: True
question: Saved screen
fields:
  - Name: saved_name
---
code: |
  selected_trust
  journey_complete = True
---
question: Choose a trust
fields:
  - Trust: selected_trust
    datatype: object_radio
    choices: |
      []
    none of the above: False
    minlength: 1
  - Note: note
    required: False
  - Count: count
    datatype: integer
    default: 7
validation code: |
  validated = selected_trust
"""


def _journey(tmp_path, family, start_worker, interview=INTERVIEW):
    return start_empty_control_journey(
        tmp_path, family, start_worker, "issue86", interview
    )


def _selection_interview(generic, datatype):
    interview = INTERVIEW.replace("datatype: object_radio", f"datatype: {datatype}")
    if datatype == "object":
        interview = interview.replace("    none of the above: False\n", "")
    if generic:
        interview = (
            interview.replace("  selected_trust\n", "  rav.selected_trust\n")
            .replace(
                "question: Choose a trust",
                "generic object: DAObject\nquestion: Choose a trust",
            )
            .replace("Trust: selected_trust", "Trust: x.selected_trust")
            .replace("validated = selected_trust", "validated = x.selected_trust")
        )
    return interview


def _assert_replay(journey, family, root, record):
    original = record.read_bytes()
    saved = saved_sessions(root)
    golden = load_trace(record)
    code, replay = _command(family.interpreter, root, "trace", "replay", str(record))
    assert code == 0, replay
    assert replay["result"]["clean"] is True, replay
    assert replay["result"]["counts"] == {
        "expected": len(golden.entries),
        "replayed": len(golden.entries),
        "matched": len(golden.entries),
    }
    worker_replay = journey.request("trace-replay", golden=str(record))
    assert worker_replay["ok"], worker_replay
    assert worker_replay["result"] == replay["result"]
    assert record.read_bytes() == original
    assert saved_sessions(root) == saved
    assert list((root / ".simulator").glob("trace-replay-*")) == []


@pytest.mark.parametrize("generic", [False, True])
@pytest.mark.parametrize("datatype", ["object", "object_radio"])
@pytest.mark.parametrize("activate", [False, True])
def test_automatic_seek_and_replay_accept_absent_required_selection(
    family_python,  # noqa: F811
    tmp_path,
    worker_journey,  # noqa: F811
    generic,
    datatype,
    activate,
):
    journey = _journey(
        tmp_path, family_python, worker_journey, _selection_interview(generic, datatype)
    )
    record = tmp_path / "accepted.jsonl"
    assert journey.request("start", record=str(record))["ok"]
    saved = saved_sessions(tmp_path)
    for attempt in range(2):
        walked = journey.request(
            "seek",
            variable="journey_complete",
            auto=True,
            activate=activate,
            record=str(record),
        )
        assert walked["ok"], walked
        result = walked["result"]
        assert result["auto_stop"]["reason"] == "sought variable resolved", result
        expected_attempts = 0 if activate and attempt else 1
        assert len(result["auto_submitted"]) == expected_attempts, result
        assert len(result["auto_trace"]) == expected_attempts, result
        if expected_attempts:
            screen = result["auto_trace"][0]["screen"]
            assert screen["fields"][0]["required"] is True
            assert screen["fields"][0]["rendered_input"] is False
            assert result["auto_trace"][0]["ok"] is True
        if not activate:
            assert saved_sessions(tmp_path) == saved
    selection = "rav.selected_trust" if generic else "selected_trust"
    if activate:
        assert (
            journey.evaluate(f"({selection}, validated, note, count)")["result"][
                "value"
            ]
            == "(None, None, '', 7)"
        )
    else:
        assert journey.status()["result"]["question_text"] == "Saved screen"
        assert (
            journey.evaluate(
                f"(defined('{selection}'), defined('validated'), defined('note'), defined('count'))"
            )["result"]["value"]
            == "(False, False, False, False)"
        )
    golden = load_trace(record)
    assert sum(entry.intent == "auto" for entry in golden.entries) == (
        1 if activate else 2
    )
    assert all(entry.ok for entry in golden.entries)
    _assert_replay(journey, family_python, tmp_path, record)


@pytest.mark.parametrize("generic", [False, True])
@pytest.mark.parametrize("datatype", ["object", "object_radio"])
@pytest.mark.parametrize("activate", [False, True])
def test_automatic_seek_and_replay_preserve_authored_empty_selection_rejection(
    family_python,  # noqa: F811
    tmp_path,
    worker_journey,  # noqa: F811
    generic,
    datatype,
    activate,
):
    interview = (
        _selection_interview(generic, datatype).replace(
            "  validated = ", "  rav.validation_mutation = True\n  validated = "
        )
        + "  if validated is None:\n    validation_error('You must select at least one Trust')\n"
    )
    journey = _journey(tmp_path, family_python, worker_journey, interview)
    record = tmp_path / "rejected.jsonl"
    assert journey.request("start", record=str(record))["ok"]
    saved = saved_sessions(tmp_path)
    walked = journey.request(
        "seek",
        variable="journey_complete",
        auto=True,
        activate=activate,
        record=str(record),
    )
    # Seek reports a stopped walk, not a failed execution operation.
    assert walked["ok"], walked
    result = walked["result"]
    assert result["auto_submitted"] == []
    stop = result["auto_stop"]
    assert stop["reason"] == "validation failed", stop
    assert stop["errors"] == ["You must select at least one Trust"]
    assert "missing_required" not in stop
    assert stop["submitted"] is False
    assert len(result["auto_trace"]) == 1
    rejected = result["auto_trace"][0]
    assert rejected["ok"] is False
    assert rejected["error"]["kind"] == "validation"
    assert rejected["error"]["details"]["errors"] == stop["errors"]
    selection = "rav.selected_trust" if generic else "selected_trust"
    assert (
        journey.evaluate(
            f"(defined('{selection}'), defined('validated'), defined('note'), "
            "defined('count'), defined('rav.validation_mutation'), defined('journey_complete'))"
        )["result"]["value"]
        == "(False, False, False, False, False, False)"
    )
    if not activate:
        assert saved_sessions(tmp_path) == saved
    expected_screen = "Choose a trust" if activate else "Saved screen"
    assert journey.status()["result"]["question_text"] == expected_screen
    refreshed = journey.request("refresh", record=str(record))
    assert refreshed["ok"], refreshed
    assert refreshed["result"]["question_text"] == expected_screen
    golden = load_trace(record)
    assert [entry.intent for entry in golden.entries] == [
        "start",
        "auto",
        "seek",
        "refresh",
    ]
    assert golden.entries[1].ok is False
    assert golden.entries[1].error["details"]["errors"] == stop["errors"]
    assert golden.entries[2].post_auto is True
    _assert_replay(journey, family_python, tmp_path, record)

    # A now-accepted transition must not silently satisfy a recorded rejection.
    source = tmp_path / "docassemble" / "issue86" / "data" / "questions" / "main.yml"
    source.write_text(interview.replace("if validated is None:", "if False:"))
    saved = saved_sessions(tmp_path)
    original = record.read_bytes()
    code, diverged = _command(
        family_python.interpreter, tmp_path, "trace", "replay", str(record)
    )
    assert code == 2, diverged
    assert diverged["error"]["kind"] == "trace-mismatch", diverged
    assert diverged["error"]["details"]["first_divergence"]["step"] == 2
    worker_diverged = journey.request("trace-replay", golden=str(record))
    assert not worker_diverged["ok"], worker_diverged
    assert worker_diverged["error"] == diverged["error"]
    assert saved_sessions(tmp_path) == saved
    assert record.read_bytes() == original
    assert list((tmp_path / ".simulator").glob("trace-replay-*")) == []
