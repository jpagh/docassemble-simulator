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
    WorkerJourney,
    _environment,
    family_python,
    real_python,
    real_python_19,
    worker_journey,
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
    "id:client names@clients[i].complete": "people",
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
    "id:client names@clients[i].complete": [
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

REPLAY_INTERVIEW = """\
---
id: intro
mandatory: True
question: |
  Intro
fields:
  - Name: user_name
    default: Ada
---
id: optional one
mandatory: True
question: |
  Optional one
fields:
  - Value: optional_one
    default: one
---
id: optional two
mandatory: True
question: |
  Optional two
fields:
  - Value: optional_two
    default: two
---
id: required
mandatory: True
question: |
  Required value
fields:
  - Value: target_value
validation code: |
  if target_value == "bad":
    validation_error("bad is not available")
---
mandatory: True
question: |
  Done
"""

MULTI_AUTO_REPLAY_INTERVIEW = """\
---
id: saved
mandatory: True
question: |
  Saved screen
fields:
  - Value: saved_value
---
mandatory: True
code: |
  target_value = first_value + second_value
---
id: first automatic
mandatory: True
question: |
  First automatic screen
fields:
  - Value: first_value
    default: first
---
id: second automatic
mandatory: True
question: |
  Second automatic screen
fields:
  - Value: second_value
    default: second
validation code: |
  from pathlib import Path
  Path("__TRACE_REPLAY_MARKER__").write_text("visited")
---
mandatory: True
question: |
  Done
"""

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


def _record(journey, operation, record: Path, **fields):
    """Drive one worker operation that records its screen outcome to ``record``."""
    return journey.request(operation, record=str(record), **fields)


def _drive(interpreter, root: Path, record: Path, family) -> None:
    """Drive the scenario, keying every answer on the current screen identity.

    Recording goes through one persistent worker journey: the worker compiles
    the interview once and serves every branch step, while each request asks
    for its own ``record``/``phase`` exactly as the one-shot CLI commands did.
    """
    consumed: dict[str, int] = defaultdict(int)
    journey = WorkerJourney(
        interpreter,
        root,
        family=family,
        arguments=("--offline", "--seek-diagnostics", "off"),
    )
    try:

        def screen_for(payload):
            if payload.get("ok") and isinstance(payload.get("result"), dict):
                return payload["result"]
            assert payload["error"]["kind"] == "validation", payload
            # A failed answer leaves the active screen in place.
            status = journey.status()
            assert status["ok"], status
            return status["result"]

        payload = journey.request("start", record=str(record), phase="intake")
        assert payload["ok"], payload

        while True:
            screen = screen_for(payload)
            key = screen_identity(screen).key
            options = ANSWERS.get(key)
            if not options:
                return
            index = consumed[key]
            consumed[key] += 1
            if index >= len(options):
                return
            assignments = options[index]
            payload = journey.request(
                "answer",
                assignments=[f"{name}={value}" for name, value in assignments.items()],
                record=str(record),
                phase=PHASES.get(key, "intake"),
            )
    finally:
        journey.close()


def _record_base(interpreter, root: Path, record: Path, family) -> None:
    _write_interview(root)
    _drive(interpreter, root, record, family)


def _replay_relevant_entries(trace):
    """The recorded fields replay reads, normalized for a strict comparison.

    ``compare_traces`` reports identity and content differences, so it cannot
    catch a transport that records the wrong operation, intent, target,
    activation policy, submitted assignments, or error kind. Comparing these
    fields is the guard that a worker journey and the one-shot CLI really
    record the same scenario.
    """
    entries = []
    for entry in trace.entries:
        relevant = entry.as_dict()
        del relevant["screen"]
        del relevant["content"]
        relevant.pop("error", None)
        relevant["error_kind"] = (entry.error or {}).get("kind")
        entries.append(relevant)
    return entries


def test_trace_replay_round_trips_default_auto_failure_and_answer(
    family_python,  # noqa: F811
    tmp_path,
    worker_journey,  # noqa: F811
):
    interpreter = family_python.interpreter
    root = _write_interview(tmp_path / "replay", REPLAY_INTERVIEW)
    record = root / "scenario.trace.jsonl"
    journey = worker_journey(
        interpreter,
        root,
        family=family_python.probe,
        arguments=("--offline", "--seek-diagnostics", "off"),
    )

    assert _record(journey, "start", record)["ok"]
    assert _record(journey, "answer", record, defaults=True)["ok"]
    for variable in ("optional_one", "optional_two"):
        assert _record(
            journey, "seek", record, variable=variable, auto=True, activate=True
        )["ok"]
    assert _record(
        journey, "seek", record, variable="target_value", auto=True, activate=True
    )["ok"]
    assert not _record(journey, "answer", record, assignments=["target_value=bad"])[
        "ok"
    ]
    assert _record(journey, "answer", record, assignments=["target_value=good"])["ok"]

    golden = load_trace(record)
    assert any(entry.intent == "defaults" for entry in golden.entries)
    assert sum(entry.intent == "auto" for entry in golden.entries) >= 3
    assert any(entry.intent == "auto" and not entry.ok for entry in golden.entries)
    before = {
        path.name: path.read_bytes()
        for path in (root / ".simulator" / "sessions").glob("*.pkl")
    }

    code, replay = _command(interpreter, root, "trace", "replay", str(record))
    assert code == 0, replay
    assert replay["result"]["clean"] is True, replay
    assert replay["result"]["counts"]["matched"] == len(golden.entries)
    after = {
        path.name: path.read_bytes()
        for path in (root / ".simulator" / "sessions").glob("*.pkl")
    }
    assert after == before


def test_worker_journey_recording_matches_the_one_shot_cli(
    family_python,  # noqa: F811
    tmp_path,
    worker_journey,  # noqa: F811
):
    """A worker-recorded scenario equals the same scenario recorded by the CLI.

    The scenario exercises the three recording shapes the trace adapter owns:
    an expanded automatic walk with its trailing post-walk marker, a failed
    answer recorded against the unchanged active screen, and ``--phase``
    tagging.
    """
    interpreter = family_python.interpreter
    phases = ("intake", "close")

    worker_root = _write_interview(tmp_path / "worker", REPLAY_INTERVIEW)
    worker_record = worker_root / "scenario.trace.jsonl"
    journey = worker_journey(
        interpreter,
        worker_root,
        family=family_python.probe,
        arguments=("--offline", "--seek-diagnostics", "off"),
    )
    assert _record(journey, "start", worker_record, phase=phases[0])["ok"]
    assert _record(journey, "answer", worker_record, defaults=True, phase=phases[0])[
        "ok"
    ]
    for variable in ("optional_one", "optional_two", "target_value"):
        assert _record(
            journey,
            "seek",
            worker_record,
            variable=variable,
            auto=True,
            activate=True,
            phase=phases[1],
        )["ok"]
    assert not _record(
        journey,
        "answer",
        worker_record,
        assignments=["target_value=bad"],
        phase=phases[1],
    )["ok"]
    assert _record(
        journey,
        "answer",
        worker_record,
        assignments=["target_value=good"],
        phase=phases[1],
    )["ok"]

    cli_root = _write_interview(tmp_path / "cli", REPLAY_INTERVIEW)
    cli_record = cli_root / "scenario.trace.jsonl"
    steps = (
        (0, ("start", "--record", str(cli_record), "--phase", phases[0])),
        (
            0,
            ("answer", "--defaults", "--record", str(cli_record), "--phase", phases[0]),
        ),
        *(
            (
                0,
                (
                    "seek",
                    variable,
                    "--auto",
                    "--activate",
                    "--record",
                    str(cli_record),
                    "--phase",
                    phases[1],
                ),
            )
            for variable in ("optional_one", "optional_two", "target_value")
        ),
        (
            2,
            (
                "answer",
                "target_value=bad",
                "--record",
                str(cli_record),
                "--phase",
                phases[1],
            ),
        ),
        (
            0,
            (
                "answer",
                "target_value=good",
                "--record",
                str(cli_record),
                "--phase",
                phases[1],
            ),
        ),
    )
    for expected_code, arguments in steps:
        code, payload = _command(interpreter, cli_root, *arguments)
        assert code == expected_code, payload

    worker_trace = load_trace(worker_record)
    cli_trace = load_trace(cli_record)

    # Both recordings expand the automatic walk into one `auto` entry per
    # attempted screen, keep the trailing post-walk marker, and record the
    # rejected answer against the unchanged active screen.
    for trace in (worker_trace, cli_trace):
        automatic = [entry for entry in trace.entries if entry.intent == "auto"]
        assert len(automatic) >= 2, trace
        post_auto = next(entry for entry in trace.entries if entry.post_auto)
        assert post_auto.fresh is False, trace
        assert any(entry.ok is False for entry in automatic), trace
        assert any(entry.intent == "defaults" for entry in trace.entries), trace
    assert [entry.phase for entry in worker_trace.entries] == [
        entry.phase for entry in cli_trace.entries
    ]
    assert {entry.phase for entry in worker_trace.entries} == set(phases)

    ordered = compare_traces(cli_trace, worker_trace)
    assert ordered.matched is True, ordered.diffs
    phased = compare_traces(
        cli_trace, worker_trace, ComparePolicy(order="phased", phases=phases)
    )
    assert phased.matched is True, phased.diffs
    worker_entries = _replay_relevant_entries(worker_trace)
    cli_entries = _replay_relevant_entries(cli_trace)
    assert len(worker_entries) == len(cli_entries)
    for worker_entry, cli_entry in zip(worker_entries, cli_entries):
        assert worker_entry == cli_entry, (worker_entry, cli_entry)


def test_trace_replay_groups_a_recorded_multi_screen_nonactivated_walk(
    family_python,  # noqa: F811
    tmp_path,
    worker_journey,  # noqa: F811
):
    """A recorded auto,auto walk stays isolated from its Saved source screen."""
    interpreter = family_python.interpreter
    marker = tmp_path / "multi-auto.marker"
    interview = MULTI_AUTO_REPLAY_INTERVIEW.replace(
        "__TRACE_REPLAY_MARKER__", str(marker)
    )
    root = _write_interview(tmp_path / "multi-auto", interview)
    record = root / "scenario.trace.jsonl"
    journey = worker_journey(
        interpreter,
        root,
        family=family_python.probe,
        arguments=("--offline", "--seek-diagnostics", "off"),
    )

    assert _record(journey, "start", record)["ok"]
    assert _record(journey, "seek", record, variable="target_value", auto=True)["ok"]
    marker.unlink()
    # This answer must still target Saved screen A after the non-activated
    # walk temporarily traversed screens B and C.
    assert _record(journey, "answer", record, assignments=["saved_value=done"])["ok"]

    golden = load_trace(record)
    automatic = [entry for entry in golden.entries if entry.intent == "auto"]
    assert len(automatic) >= 2
    assert golden.entries[1].intent == "auto"
    assert golden.entries[2].intent == "auto"
    assert golden.entries[3].post_auto is True
    assert golden.entries[3].activate is False
    original = record.read_bytes()
    code, replay = _command(interpreter, root, "trace", "replay", str(record))
    assert code == 0, replay
    assert replay["result"]["clean"] is True, replay

    code, overridden = _command(
        interpreter,
        root,
        "trace",
        "replay",
        str(record),
        "--override",
        "2.first_value=changed",
    )
    assert code == 0, overridden
    assert overridden["result"]["clean"] is True, overridden
    assert record.read_bytes() == original

    code, diverged = _command(
        interpreter,
        root,
        "trace",
        "replay",
        str(record),
        "--override",
        "2.not_a_field=changed",
    )
    assert code == 2, diverged
    assert diverged["error"]["details"]["first_divergence"]["step"] == 2


def test_trace_replay_stops_before_later_automatic_effect_on_first_identity_divergence(
    family_python,  # noqa: F811
    tmp_path,
    worker_journey,  # noqa: F811
):
    interpreter = family_python.interpreter
    marker = tmp_path / "later-screen-validation.marker"
    interview = MULTI_AUTO_REPLAY_INTERVIEW.replace(
        "__TRACE_REPLAY_MARKER__", str(marker)
    )
    root = _write_interview(tmp_path / "first-divergence", interview)
    record = root / "scenario.trace.jsonl"
    journey = worker_journey(
        interpreter,
        root,
        family=family_python.probe,
        arguments=("--offline", "--seek-diagnostics", "off"),
    )

    assert _record(journey, "start", record)["ok"]
    assert _record(
        journey, "seek", record, variable="target_value", auto=True, activate=True
    )["ok"]
    assert marker.exists()
    marker.unlink()
    golden_before = record.read_bytes()
    session_files = {
        path.name: path.read_bytes()
        for path in (root / ".simulator" / "sessions").glob("*.pkl")
    }

    questions = root / "docassemble" / "tracefix" / "data" / "questions" / "main.yml"
    questions.write_text(
        questions.read_text().replace(
            "id: first automatic", "id: first automatic changed"
        )
    )
    code, diverged = _command(interpreter, root, "trace", "replay", str(record))
    assert code == 2, diverged
    first = diverged["error"]["details"]["first_divergence"]
    assert first["step"] == 2
    assert first["identity_match"] is False
    assert first["expected"]["identity"]["key"] == "id:first automatic"
    assert first["actual"]["identity"]["key"] == "id:first automatic changed"
    assert first["expected"]["submitted"] == {}
    assert first["actual"]["submitted"] == {}
    assert not marker.exists()
    assert record.read_bytes() == golden_before
    assert session_files == {
        path.name: path.read_bytes()
        for path in (root / ".simulator" / "sessions").glob("*.pkl")
    }


def test_trace_replay_groups_fresh_activated_multi_screen_walk(
    family_python,  # noqa: F811
    tmp_path,
    worker_journey,  # noqa: F811
):
    interpreter = family_python.interpreter
    marker = tmp_path / "fresh-multi-auto.marker"
    interview = MULTI_AUTO_REPLAY_INTERVIEW.replace(
        "__TRACE_REPLAY_MARKER__", str(marker)
    )
    root = _write_interview(tmp_path / "fresh-multi-auto", interview)
    record = root / "fresh.trace.jsonl"
    journey = worker_journey(
        interpreter,
        root,
        family=family_python.probe,
        arguments=("--offline", "--seek-diagnostics", "off"),
    )

    assert _record(
        journey,
        "seek",
        record,
        variable="target_value",
        auto=True,
        fresh=True,
        activate=True,
    )["ok"]
    marker.unlink()
    golden = load_trace(record)
    automatic = [entry for entry in golden.entries if entry.intent == "auto"]
    assert len(automatic) >= 2
    assert automatic[0].fresh is True
    assert next(entry for entry in golden.entries if entry.post_auto).activate is True

    code, replay = _command(interpreter, root, "trace", "replay", str(record))
    assert code == 0, replay
    assert replay["result"]["clean"] is True, replay


def test_trace_replay_preserves_nonactivated_seek_context(
    family_python,  # noqa: F811
    tmp_path,
    worker_journey,  # noqa: F811
):
    """A target walk must not answer the Saved screen it temporarily bypasses."""
    interpreter = family_python.interpreter
    root = _write_interview(tmp_path / "nonactivated", REPLAY_INTERVIEW)
    record = root / "scenario.trace.jsonl"
    journey = worker_journey(
        interpreter,
        root,
        family=family_python.probe,
        arguments=("--offline", "--seek-diagnostics", "off"),
    )

    assert _record(journey, "start", record)["ok"]
    # The Saved session remains on Intro.  The automatic walk seeks the later
    # target, accepts its optional predecessors, and rejects at Required.
    assert _record(journey, "seek", record, variable="target_value", auto=True)["ok"]
    assert _record(journey, "answer", record, assignments=["user_name=Jack"])["ok"]
    assert _record(journey, "answer", record, defaults=True)["ok"]
    assert _record(journey, "answer", record, defaults=True)["ok"]
    assert _record(journey, "answer", record, assignments=["target_value=good"])["ok"]

    golden = load_trace(record)
    automatic = [entry for entry in golden.entries if entry.intent == "auto"]
    assert automatic
    assert any(not entry.ok for entry in automatic)
    post_walk = next(entry for entry in golden.entries if entry.post_auto)
    assert post_walk.activate is False

    before = {
        path.name: path.read_bytes()
        for path in (root / ".simulator" / "sessions").glob("*.pkl")
    }
    code, replay = _command(interpreter, root, "trace", "replay", str(record))
    assert code == 0, replay
    assert replay["result"]["clean"] is True, replay
    after = {
        path.name: path.read_bytes()
        for path in (root / ".simulator" / "sessions").glob("*.pkl")
    }
    assert after == before


def test_screen_trace_acceptance(family_python, tmp_path):  # noqa: F811
    interpreter = family_python.interpreter

    # 1. Record and replay the base scenario.
    golden = tmp_path / "golden.trace.jsonl"
    replay = tmp_path / "replay.trace.jsonl"
    _record_base(interpreter, tmp_path / "golden", golden, family_python.probe)
    _record_base(interpreter, tmp_path / "replay", replay, family_python.probe)
    expected = load_trace(golden)
    actual = load_trace(replay)
    identities = [entry.identity.key for entry in expected.entries]

    assert expected.metadata.interview == INTERVIEW
    assert identities[0] == "id:intro"
    assert "fields:favorite_color" in identities
    assert identities.count("id:client names@clients[i].complete") == 2
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
    _drive(interpreter, inserted_root, inserted_trace, family_python.probe)
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
    _drive(interpreter, reordered_root, reordered_trace, family_python.probe)
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
    _drive(interpreter, renamed_root, renamed_trace, family_python.probe)
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


BOUNDED_AUTO_REPLAY_INTERVIEW = """\
---
id: saved
mandatory: True
question: |
  Saved screen
fields:
  - Value: saved_value
---
id: first automatic
mandatory: True
question: |
  First automatic screen
fields:
  - Value: first_value
    default: first
---
id: second automatic
mandatory: True
question: |
  Second automatic screen
fields:
  - Value: second_value
    default: second
---
mandatory: True
code: |
  target_value = first_value + second_value
---
mandatory: True
question: |
  Done
"""

_EXTRA_SCREEN_BLOCK = """\
---
id: extra automatic
mandatory: True
question: |
  Extra automatic screen
fields:
  - Value: third_value
    default: third
validation code: |
  from pathlib import Path
  Path("__TRACE_REPLAY_MARKER__").write_text("visited")
---
mandatory: True
code: |
  target_value = first_value + second_value + third_value
---
"""

_LATER_SCREEN_BLOCK = """\
---
id: later mandatory
mandatory: True
question: |
  Later mandatory screen
fields:
  - Value: later_value
    default: later
validation code: |
  from pathlib import Path
  Path("__TRACE_REPLAY_MARKER__").write_text("visited")
---
"""

# An unrecorded automatic screen the changed interview needs before the walk
# can resolve its target.
EXTRA_AUTO_SCREEN_INTERVIEW = BOUNDED_AUTO_REPLAY_INTERVIEW.replace(
    """\
---
mandatory: True
code: |
  target_value = first_value + second_value
---
""",
    _EXTRA_SCREEN_BLOCK,
)

# The target resolves after the first recorded attempt, so the walk ends before
# the second recorded attempt runs.
EARLY_END_INTERVIEW = BOUNDED_AUTO_REPLAY_INTERVIEW.replace(
    "target_value = first_value + second_value", "target_value = first_value"
)

# Every recorded submission still matches, but the interview now reaches a
# later mandatory screen instead of the recorded trailing outcome.
LATER_MANDATORY_INTERVIEW = BOUNDED_AUTO_REPLAY_INTERVIEW.replace(
    """\
---
mandatory: True
question: |
  Done
""",
    _LATER_SCREEN_BLOCK
    + """\
---
mandatory: True
question: |
  Done
""",
)

#: One recorded activated walk per interpreter, shared by the regressions below.
_BOUNDED_WALK_GOLDENS: dict[str, bytes] = {}


def _bounded_walk_golden(interpreter, tmp_path_factory, family) -> bytes:
    """Record the shared activated walk: a Saved answer plus two submissions."""
    key = str(interpreter)
    if key not in _BOUNDED_WALK_GOLDENS:
        root = _write_interview(
            tmp_path_factory.mktemp("bounded-walk"), BOUNDED_AUTO_REPLAY_INTERVIEW
        )
        record = root / "scenario.trace.jsonl"
        journey = WorkerJourney(
            interpreter,
            root,
            family=family,
            arguments=("--offline", "--seek-diagnostics", "off"),
        )
        try:
            assert _record(journey, "start", record)["ok"]
            assert _record(journey, "answer", record, assignments=["saved_value=done"])[
                "ok"
            ]
            assert _record(
                journey,
                "seek",
                record,
                variable="target_value",
                auto=True,
                activate=True,
            )["ok"]
        finally:
            journey.close()
        _BOUNDED_WALK_GOLDENS[key] = record.read_bytes()
    return _BOUNDED_WALK_GOLDENS[key]


def _bounded_walk_root(
    interpreter, tmp_path, tmp_path_factory, name, interview, family
):
    """Hold one replay interview variant beside a copy of the recorded golden."""
    marker = tmp_path / f"{name}.marker"
    root = _write_interview(
        tmp_path / name, interview.replace("__TRACE_REPLAY_MARKER__", str(marker))
    )
    record = root / "scenario.trace.jsonl"
    record.write_bytes(_bounded_walk_golden(interpreter, tmp_path_factory, family))
    return root, record, marker


def _saved_session_files(root):
    return {
        path.name: path.read_bytes()
        for path in (root / ".simulator" / "sessions").glob("*.pkl")
    }


def test_trace_replay_never_submits_an_unrecorded_automatic_screen(
    family_python,  # noqa: F811
    tmp_path,
    tmp_path_factory,
):
    """The recorded automatic group is a hard submission boundary."""
    interpreter = family_python.interpreter
    root, record, marker = _bounded_walk_root(
        interpreter,
        tmp_path,
        tmp_path_factory,
        "unrecorded-screen",
        EXTRA_AUTO_SCREEN_INTERVIEW,
        family_python.probe,
    )
    golden_before = record.read_bytes()
    sessions_before = _saved_session_files(root)

    code, replay = _command(interpreter, root, "trace", "replay", str(record))

    assert code == 2, replay
    details = replay["error"]["details"]
    # Both recorded attempts matched; the third automatic screen the changed
    # interview reaches was observed and reported, never submitted.
    assert [step["match"] for step in details["steps"]] == [
        True,
        True,
        True,
        True,
        False,
    ]
    divergence = details["first_divergence"]
    assert divergence["step"] == 5
    assert divergence["identity_match"] is False
    assert divergence["actual"]["identity"]["key"] == "id:extra automatic"
    assert divergence["actual"]["submitted"] == {}
    assert not marker.exists()
    assert record.read_bytes() == golden_before
    assert _saved_session_files(root) == sessions_before

    # A per-step override obeys the same boundary: the walk it produces is
    # still bounded, and the unrecorded screen is never submitted.
    code, overridden = _command(
        interpreter,
        root,
        "trace",
        "replay",
        str(record),
        "--override",
        "3.first_value=changed",
    )
    assert code == 2, overridden
    assert not marker.exists()


def test_trace_replay_reports_a_trailing_outcome_divergence_without_submitting(
    family_python,  # noqa: F811
    tmp_path,
    tmp_path_factory,
):
    """A trailing-only difference is reported without another submission."""
    interpreter = family_python.interpreter
    root, record, marker = _bounded_walk_root(
        interpreter,
        tmp_path,
        tmp_path_factory,
        "trailing-outcome",
        LATER_MANDATORY_INTERVIEW,
        family_python.probe,
    )
    auto_entries = [
        entry for entry in load_trace(record).entries if entry.intent == "auto"
    ]
    post = next(entry for entry in load_trace(record).entries if entry.post_auto)
    golden_before = record.read_bytes()
    sessions_before = _saved_session_files(root)

    code, replay = _command(interpreter, root, "trace", "replay", str(record))

    assert code == 2, replay
    details = replay["error"]["details"]
    assert [step["match"] for step in details["steps"]] == [
        True,
        True,
        True,
        True,
        False,
    ]
    divergence = details["first_divergence"]
    assert divergence["step"] == post.seq
    assert divergence["expected"]["identity"]["key"] == post.identity.key
    assert divergence["actual"]["identity"]["key"] == "id:later mandatory"
    assert divergence["actual"]["submitted"] == {}
    assert len(details["steps"]) == len(auto_entries) + 3
    assert not marker.exists()
    assert record.read_bytes() == golden_before
    assert _saved_session_files(root) == sessions_before


def test_trace_replay_reports_the_earliest_missing_expected_step(
    family_python,  # noqa: F811
    tmp_path,
    tmp_path_factory,
):
    """A walk that ends early is truncated, not a silent success."""
    interpreter = family_python.interpreter
    root, record, _marker = _bounded_walk_root(
        interpreter,
        tmp_path,
        tmp_path_factory,
        "early-end",
        EARLY_END_INTERVIEW,
        family_python.probe,
    )
    golden = load_trace(record)
    automatic = [entry for entry in golden.entries if entry.intent == "auto"]
    golden_before = record.read_bytes()
    sessions_before = _saved_session_files(root)

    code, replay = _command(interpreter, root, "trace", "replay", str(record))

    assert code == 2, replay
    details = replay["error"]["details"]
    divergence = details["first_divergence"]
    assert divergence["step"] == automatic[1].seq
    assert divergence["identity_match"] is False
    # The report carries the real walk-ending outcome (the target's `continue`
    # resolution), not a fabricated empty observation.
    actual = divergence["actual"]
    assert actual["identity"]["key"] == "screen:continue"
    assert actual["identity"]["rule"] == "category"
    assert actual["ok"] is True
    assert actual.get("error") is None
    assert actual["submitted"] == {}
    assert divergence["flow_match"] is True
    assert record.read_bytes() == golden_before
    assert _saved_session_files(root) == sessions_before


EXECUTION_SEAM_HARNESS = """\
import hashlib
import json
import sys
from pathlib import Path

from docassemble_simulator._runtime import SimulatorRuntime
from docassemble_simulator.bootstrap import bootstrap
from docassemble_simulator.execution import (
    Answer,
    InterviewExecution,
    Refresh,
    ReplayAutomatic,
    Start,
    Status,
    Variables,
)
from docassemble_simulator.preflight import ensure_importable
from docassemble_simulator.trace import (
    load_trace,
    replay_expectation,
    replay_identity_context,
)


def screen(outcome):
    if not outcome.ok:
        return {"ok": False, "error": str(outcome.error.kind)}
    result = outcome.result if isinstance(outcome.result, dict) else {}
    return {
        "ok": True,
        "kind": result.get("kind"),
        "question_name": result.get("question_name"),
    }


root = Path(sys.argv[1]).resolve()
golden = load_trace(sys.argv[2])
fresh = sys.argv[3] == "fresh"
session_directory = Path(sys.argv[4]).resolve()
# Read the override before bootstrap neutralizes sys.argv for interview code.
override = sys.argv[5] if len(sys.argv) > 5 else ""
entries = golden.entries
group = [entry for entry in entries if (entry.intent or entry.operation) == "auto"]
post = next(entry for entry in entries if entry.post_auto)

with SimulatorRuntime().activate(root):
    ensure_importable(root, offline=True)
    bootstrap()
    execution = InterviewExecution(
        root,
        golden.metadata.interview,
        config_fingerprint=golden.metadata.config_fingerprint,
        session_directory=session_directory,
    )
    if not fresh:
        # The recorded prefix: the Saved screen, then its recorded answer.
        assert execution.run(Start()).ok
        assert execution.run(Answer((("saved_value", "done"),))).ok

    def observe():
        session = execution.scope.session
        status = execution.run(Status())
        variables = execution.run(Variables(contains="value"))
        return {
            "session_exists": session.exists(),
            "session_digest": (
                hashlib.sha256(session.read_bytes()).hexdigest()[:12]
                if session.exists()
                else None
            ),
            "status": screen(status),
            "variables": {
                name: value
                for name, value in (variables.result or {}).items()
                if name.endswith("_value")
            },
        }

    before = observe()
    recorded = [dict(entry.submitted) for entry in group]
    if override:
        sequence, _, assignment = override.partition(".")
        name, _, value = assignment.partition("=")
        for entry, submitted in zip(group, recorded):
            if entry.seq == int(sequence):
                submitted[name] = value
    outcome = execution.run(
        ReplayAutomatic(
            group[0].target,
            submissions=tuple(tuple(submitted.items()) for submitted in recorded),
            fresh=fresh,
            activate=post.activate,
            expectations=tuple(replay_expectation(entry) for entry in group),
            identity_context=replay_identity_context(
                entries[: entries.index(group[0])]
            ),
            trailing=replay_expectation(post),
        )
    )
    observed = outcome.result if isinstance(outcome.result, dict) else {}
    if not observed and not outcome.ok and isinstance(outcome.error.details, dict):
        observed = outcome.error.details
    after = observe()
    refreshed = execution.run(Refresh()) if after["session_exists"] else None
    print(
        json.dumps(
            {
                "replay_ok": outcome.ok,
                "divergence": observed.get("replay_divergence"),
                "auto_stop": (observed.get("auto_stop") or {}).get("reason"),
                "auto_steps": len(observed.get("auto_trace") or []),
                "before": before,
                "after": after,
                "refreshed": screen(refreshed) if refreshed is not None else None,
            }
        )
    )
"""


def _execution_seam(
    interpreter, root, record, session_directory, *, fresh=False, override=""
):
    """Drive one isolated replay operation through the family interpreter."""
    completed = subprocess.run(
        [
            str(interpreter),
            "-c",
            EXECUTION_SEAM_HARNESS,
            str(root),
            str(record),
            "fresh" if fresh else "existing",
            str(session_directory),
            override,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_environment(),
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stderr == "", completed.stderr
    assert completed.stdout.strip(), completed.stdout
    return json.loads(completed.stdout)


def test_trace_replay_execution_keeps_divergent_activated_walks_out_of_the_session(
    family_python,  # noqa: F811
    tmp_path,
    tmp_path_factory,
):
    """Persistence and rollback observed through the isolated replay session."""
    interpreter = family_python.interpreter
    cases = (
        ("matched-walk", BOUNDED_AUTO_REPLAY_INTERVIEW, None, 2, ""),
        ("unrecorded-screen", EXTRA_AUTO_SCREEN_INTERVIEW, "trailing", 2, ""),
        ("early-end", EARLY_END_INTERVIEW, "early-end", 1, ""),
        ("trailing-outcome", LATER_MANDATORY_INTERVIEW, "trailing", 2, ""),
        (
            "override-divergence",
            BOUNDED_AUTO_REPLAY_INTERVIEW,
            "after-submission",
            1,
            "3.not_a_field=changed",
        ),
    )
    for name, interview, phase, submitted, override in cases:
        root, record, _marker = _bounded_walk_root(
            interpreter,
            tmp_path,
            tmp_path_factory,
            name,
            interview,
            family_python.probe,
        )
        golden = load_trace(record)
        post = next(entry for entry in golden.entries if entry.post_auto)
        automatic = [entry for entry in golden.entries if entry.intent == "auto"]

        observed = _execution_seam(
            interpreter,
            root,
            record,
            tmp_path / f"{name}-sessions",
            override=override,
        )

        assert observed["replay_ok"] is True, name
        assert observed["auto_steps"] == submitted, name
        if phase is None:
            # Control: a matching activated walk commits its namespace, Active
            # seek, and Saved outcome.
            assert observed["after"] != observed["before"], name
            assert (
                observed["after"]["session_digest"]
                != observed["before"]["session_digest"]
            )
            continue
        if phase == "after-submission":
            expected_seq = automatic[0].seq
        else:
            expected_seq = post.seq if phase == "trailing" else automatic[1].seq
        assert observed["divergence"]["phase"] == phase, name
        assert observed["divergence"]["seq"] == expected_seq, name
        if name == "unrecorded-screen":
            # The extra automatic screen is observed at the hard submission
            # boundary, not submitted.
            assert observed["auto_stop"] == "replay boundary", name
        # A divergent walk leaves the previous namespace, Saved outcome, and
        # Active seek unchanged in the isolated replay session.
        assert observed["after"] == observed["before"], name
        assert observed["refreshed"] == observed["before"]["status"], name

    # A divergent fresh walk creates no durable session when none existed.
    root, record, _marker = _bounded_walk_root(
        interpreter,
        tmp_path,
        tmp_path_factory,
        "fresh-trailing-outcome",
        LATER_MANDATORY_INTERVIEW,
        family_python.probe,
    )
    observed = _execution_seam(
        interpreter, root, record, tmp_path / "fresh-sessions", fresh=True
    )

    assert observed["replay_ok"] is True
    assert observed["divergence"]["phase"] == "trailing"
    assert observed["before"]["session_exists"] is False
    assert observed["after"]["session_exists"] is False

    # A fresh early-end walk creates no durable session either.
    root, record, _marker = _bounded_walk_root(
        interpreter,
        tmp_path,
        tmp_path_factory,
        "fresh-early-end",
        EARLY_END_INTERVIEW,
        family_python.probe,
    )
    observed = _execution_seam(
        interpreter, root, record, tmp_path / "fresh-early-end-sessions", fresh=True
    )

    assert observed["replay_ok"] is True
    assert observed["divergence"]["phase"] == "early-end"
    assert observed["before"]["session_exists"] is False
    assert observed["after"]["session_exists"] is False


def _record_replay_scenario(journey, record):
    """Record the linear default/auto/validation scenario both transports replay."""
    assert _record(journey, "start", record)["ok"]
    assert _record(journey, "answer", record, defaults=True)["ok"]
    for variable in ("optional_one", "optional_two"):
        assert _record(
            journey, "seek", record, variable=variable, auto=True, activate=True
        )["ok"]
    assert _record(
        journey, "seek", record, variable="target_value", auto=True, activate=True
    )["ok"]
    assert not _record(journey, "answer", record, assignments=["target_value=bad"])[
        "ok"
    ]
    assert _record(journey, "answer", record, assignments=["target_value=good"])["ok"]


def test_worker_trace_compare_and_replay_match_the_cli(
    family_python,  # noqa: F811
    tmp_path,
    worker_journey,  # noqa: F811
):
    """A warm worker returns the ``sim trace compare``/``replay --json`` report.

    Both reports are produced by the same seams as the one-shot CLI, so the
    parity assertion is exact JSON equality: a matching and a mismatching
    compare, a clean replay, an override divergence, and a metadata mismatch
    that stays a typed input error. The replay and its ``trace-replay`` request
    never touch the worker's Saved session or write the golden.
    """
    interpreter = family_python.interpreter
    root = _write_interview(tmp_path / "parity", REPLAY_INTERVIEW)
    record = root / "scenario.trace.jsonl"
    journey = worker_journey(
        interpreter,
        root,
        family=family_python.probe,
        arguments=("--offline", "--seek-diagnostics", "off"),
    )
    _record_replay_scenario(journey, record)
    golden = load_trace(record)
    lines = record.read_text(encoding="utf-8").splitlines()
    golden_before = record.read_bytes()
    sessions_before = _saved_session_files(root)

    # A matching compare is byte-for-byte the CLI's report.
    code, cli_compare = _command(
        interpreter, root, "trace", "compare", str(record), str(record)
    )
    assert code == 0, cli_compare
    worker_compare = journey.request(
        "trace-compare", expected=str(record), actual=str(record)
    )
    assert worker_compare["ok"] is True, worker_compare
    assert worker_compare["result"] == cli_compare["result"]

    # A truncated trace is the same typed mismatch through the worker.
    truncated = root / "truncated.trace.jsonl"
    truncated.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
    code, cli_compare_mismatch = _command(
        interpreter, root, "trace", "compare", str(record), str(truncated)
    )
    assert code == 2, cli_compare_mismatch
    worker_compare_mismatch = journey.request(
        "trace-compare", expected=str(record), actual=str(truncated)
    )
    assert worker_compare_mismatch["ok"] is False, worker_compare_mismatch
    assert worker_compare_mismatch["error"]["kind"] == "trace-mismatch"
    assert (
        worker_compare_mismatch["error"]["details"]
        == cli_compare_mismatch["error"]["details"]
    )

    # A clean replay through the warm worker is the CLI's report.
    code, cli_replay = _command(interpreter, root, "trace", "replay", str(record))
    assert code == 0, cli_replay
    worker_replay = journey.request("trace-replay", golden=str(record))
    assert worker_replay["ok"] is True, worker_replay
    assert worker_replay["result"] == cli_replay["result"]

    # An override divergence is the same typed replay report.
    good = next(
        entry
        for entry in golden.entries
        if entry.operation == "answer" and entry.submitted.get("target_value") == "good"
    )
    override = f"{good.seq}.target_value=bad"
    code, cli_divergence = _command(
        interpreter,
        root,
        "trace",
        "replay",
        str(record),
        "--override",
        override,
    )
    assert code == 2, cli_divergence
    worker_divergence = journey.request(
        "trace-replay", golden=str(record), overrides=[override]
    )
    assert worker_divergence["ok"] is False, worker_divergence
    assert worker_divergence["error"]["kind"] == "trace-mismatch"
    assert worker_divergence["error"]["details"] == cli_divergence["error"]["details"]

    # A metadata mismatch stays the same typed input error.
    mismatch = root / "mismatch.trace.jsonl"
    metadata = json.loads(lines[0])
    metadata["config_fingerprint"] = "not-the-recorded-config"
    mismatch.write_text(
        "\n".join([json.dumps(metadata, sort_keys=True), *lines[1:]]) + "\n",
        encoding="utf-8",
    )
    code, cli_input = _command(interpreter, root, "trace", "replay", str(mismatch))
    assert code == 1, cli_input
    worker_input = journey.request("trace-replay", golden=str(mismatch))
    assert worker_input["ok"] is False, worker_input
    assert worker_input["error"]["kind"] == "input"
    assert worker_input["error"]["message"] == cli_input["error"]["message"]

    # Neither the golden nor the worker's Saved session changed, and the
    # replay's own temporary session directory is gone afterwards.
    assert record.read_bytes() == golden_before
    assert _saved_session_files(root) == sessions_before
    assert list((root / ".simulator").glob("trace-replay-*")) == []
