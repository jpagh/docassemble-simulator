"""Absent object-selection inputs through real screen descriptions/submissions (#85)."""

from __future__ import annotations

import pytest
from empty_control_helpers import saved_sessions, start_empty_control_journey
from test_real_runtime import (  # noqa: F401 - fixtures registered by import
    family_python,
    real_python,
    real_python_19,
    worker_journey,
)

pytestmark = pytest.mark.real_runtime

INTERVIEW = """\
---
objects:
  - candidate: DAObject
---
mandatory: True
question: Choose a trust
fields:
  - Trust: selected_trust
    datatype: object_radio
    choices: |
      []
    none of the above: False
    minlength: 1
  - note: No trusts are available. Add a trust to continue.
validation code: |
  validated = selected_trust
---
mandatory: True
question: Selection complete
"""


def _journey(tmp_path, family, start_worker, interview=INTERVIEW):
    return start_empty_control_journey(
        tmp_path, family, start_worker, "issue85", interview
    )


def test_required_absent_object_radio_reaches_validation(
    family_python,  # noqa: F811 - imported fixture
    tmp_path,
    worker_journey,  # noqa: F811 - imported fixture
):
    journey = _journey(tmp_path, family_python, worker_journey)
    started = journey.start()
    assert started["ok"], started
    field = started["result"]["fields"][0]
    assert field["required"] is True
    assert field.get("rendered_input") is False, field
    accepted = journey.answer(defaults=True, strict=True)
    assert accepted["ok"], accepted
    assert accepted["result"]["question_text"] == "Selection complete"
    assert (
        journey.evaluate("(selected_trust, validated)")["result"]["value"]
        == "(None, None)"
    )


@pytest.mark.parametrize("generated", [False, True])
@pytest.mark.parametrize("sought", [False, True])
@pytest.mark.parametrize("datatype", ["object", "object_radio"])
def test_generic_absent_object_input_is_described_and_submitted(
    family_python,  # noqa: F811 - imported fixture
    tmp_path,
    worker_journey,  # noqa: F811 - imported fixture
    generated,
    sought,
    datatype,
):
    interview = (
        INTERVIEW.replace(
            "  - candidate: DAObject", "  - candidate: DAObject\n  - rav: DAObject"
        )
        .replace(
            "mandatory: True\nquestion: Choose a trust",
            "mandatory: True\ncode: |\n  rav.selected_trust\n---\n"
            "generic object: DAObject\nsets: [x.selected_trust]\nquestion: Choose a trust",
        )
        .replace("Trust: selected_trust", "Trust: x.selected_trust")
        .replace("validated = selected_trust", "validated = x.selected_trust")
        .replace("datatype: object_radio", f"datatype: {datatype}")
    )
    if generated:
        interview = interview.replace(
            f"  - Trust: x.selected_trust\n    datatype: {datatype}\n    choices: |\n"
            "      []\n    none of the above: False\n    minlength: 1",
            "  - code: |\n"
            "      [{'Trust': 'x.selected_trust', 'datatype': "
            f"'{datatype}', 'choices': '[]', 'none of the above': False, 'minlength': 1}}, "
            "{'note': 'No trusts are available.'}]",
        )
    if datatype == "object":
        interview = interview.replace("    none of the above: False\n", "").replace(
            ", 'none of the above': False", ""
        )
    journey = _journey(tmp_path, family_python, worker_journey, interview)
    started = journey.start()
    assert started["ok"], started
    if sought:
        started = journey.request("seek", variable="rav.selected_trust", activate=True)
        assert started["ok"], started
    assert started["result"].get("fields"), started
    field = started["result"]["fields"][0]
    assert field["required"] is True, field
    assert field.get("rendered_input") is False, field
    accepted = journey.answer(defaults=True, strict=True)
    assert accepted["ok"], accepted
    assert accepted["result"]["question_text"] == "Selection complete"
    assert (
        journey.evaluate("(rav.selected_trust, validated)")["result"]["value"]
        == "(None, None)"
    )


def test_absent_object_input_does_not_post_an_authored_default(
    family_python,  # noqa: F811 - imported fixture
    tmp_path,
    worker_journey,  # noqa: F811 - imported fixture
):
    interview = INTERVIEW.replace(
        "    datatype: object_radio",
        "    datatype: object_radio\n    default: candidate",
    )
    journey = _journey(tmp_path, family_python, worker_journey, interview)
    assert journey.start()["ok"]
    accepted = journey.answer(defaults=True, strict=True)
    assert accepted["ok"], accepted
    assert (
        journey.evaluate("(selected_trust, validated)")["result"]["value"]
        == "(None, None)"
    )


@pytest.mark.parametrize("previous", ["candidate", "None"])
def test_absent_control_preserves_defined_selection_without_browser_constraints(
    family_python,  # noqa: F811 - imported fixture
    tmp_path,
    worker_journey,  # noqa: F811 - imported fixture
    previous,
):
    interview = INTERVIEW.replace("minlength: 1", "minlength: 100").replace(
        "mandatory: True\nquestion: Choose a trust",
        f"mandatory: True\ncode: |\n  selected_trust = {previous}\n---\n"
        "mandatory: True\nquestion: Choose a trust",
    )
    journey = _journey(tmp_path, family_python, worker_journey, interview)
    assert journey.start()["ok"]
    accepted = journey.answer(defaults=True, strict=True)
    assert accepted["ok"], accepted
    assert (
        journey.evaluate(f"selected_trust is {previous} and validated is {previous}")[
            "result"
        ]["value"]
        == "True"
    )


def test_absent_trust_reports_authored_validation_and_rolls_back(
    family_python,  # noqa: F811 - imported fixture
    tmp_path,
    worker_journey,  # noqa: F811 - imported fixture
):
    interview = INTERVIEW.replace(
        "  validated = selected_trust",
        "  validated = selected_trust\n"
        "  if selected_trust is None:\n"
        "    validation_error('You must select at least one Trust')",
    )
    journey = _journey(tmp_path, family_python, worker_journey, interview)
    assert journey.start()["ok"]
    before = journey.status()["result"]
    saved = saved_sessions(tmp_path)
    for options in ({}, {"strict": True}):
        rejected = journey.answer(defaults=True, **options)
        assert not rejected["ok"] and rejected["error"]["kind"] == "validation", (
            rejected
        )
        assert rejected["error"]["details"]["errors"] == [
            "You must select at least one Trust"
        ], rejected
        assert journey.status()["result"] == before
        assert saved_sessions(tmp_path) == saved
        assert (
            journey.evaluate("(defined('selected_trust'), defined('validated'))")[
                "result"
            ]["value"]
            == "(False, False)"
        )

    # Skipping authored validation still performs native empty-field handling.
    assert journey.answer(defaults=True, no_validate=True, strict=True)["ok"]
    assert journey.evaluate("selected_trust")["result"]["value"] == "None"


def test_input_rejection_guidance_does_not_demand_an_absent_control(
    family_python,  # noqa: F811 - imported fixture
    tmp_path,
    worker_journey,  # noqa: F811 - imported fixture
):
    journey = _journey(tmp_path, family_python, worker_journey)
    assert journey.start()["ok"]
    before = journey.status()["result"]
    saved = saved_sessions(tmp_path)
    rejected = journey.answer("off_screen=1")
    assert not rejected["ok"] and rejected["error"]["kind"] == "answer-input", rejected
    assert rejected["error"]["details"]["missing_required"] == [], rejected
    assert journey.status()["result"] == before
    assert saved_sessions(tmp_path) == saved
    assert journey.evaluate("defined('selected_trust')")["result"]["value"] == "False"


@pytest.mark.parametrize("control", ["choices", "combobox", "datalist", "null-choice"])
def test_actual_required_inputs_still_reject_omission_and_blank(
    family_python,  # noqa: F811 - imported fixture
    tmp_path,
    worker_journey,  # noqa: F811 - imported fixture
    control,
):
    interview = INTERVIEW.replace("    minlength: 1\n", "")
    if control == "choices":
        interview = interview.replace("      []", "      [candidate]")
    elif control == "null-choice":
        interview = interview.replace("      []", "      [candidate]").replace(
            "none of the above: False", "none of the above: True"
        )
    else:
        interview = interview.replace(
            "    datatype: object_radio",
            f"    datatype: object\n    input type: {control}",
        ).replace("    none of the above: False\n", "")
    journey = _journey(tmp_path, family_python, worker_journey, interview)
    started = journey.start()
    assert started["ok"], started
    field = started["result"]["fields"][0]
    assert field["required"] is True, field
    assert field.get("rendered_input") is not False, field
    before = journey.status()["result"]
    saved = saved_sessions(tmp_path)
    for assignments in ((), ("selected_trust=",), ("selected_trust=None",)):
        rejected = journey.answer(*assignments, defaults=not assignments)
        assert not rejected["ok"] and rejected["error"]["kind"] == "validation", (
            rejected
        )
        assert rejected["error"]["details"]["missing_required"] == ["selected_trust"], (
            rejected
        )
        assert journey.status()["result"] == before
        assert saved_sessions(tmp_path) == saved
