"""Submission acceptance through typed execution operations on both runtimes (#67)."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest
from test_real_runtime import (  # noqa: F401 - fixtures registered by import
    RuntimeFamily,
    _environment,
    family_python,
    real_python,
    real_python_19,
)

pytestmark = pytest.mark.real_runtime

EXECUTION_SETUP = """\
import json
import sys
from pathlib import Path

from docassemble_simulator._runtime import SimulatorRuntime
from docassemble_simulator.bootstrap import bootstrap
from docassemble_simulator.execution import (
    Answer, Evaluate, InterviewExecution, ReplayAutomatic, Seek, Start, Status,
)
from docassemble_simulator.preflight import ensure_importable
from docassemble_simulator.trace import (
    TraceEntry, replay_expectation, screen_content, screen_identity,
)

root = Path(sys.argv[1]).resolve()
with SimulatorRuntime().activate(root):
    ensure_importable(root, offline=True)
    bootstrap()
    execution = InterviewExecution(root, 'docassemble.acceptance:data/questions/main.yml')

    def value(expression):
        observed = execution.run(Evaluate(expression))
        assert observed.ok, observed
        return observed.result['value']

    def select_operation(mode, *, answer, seek, replay):
        # One lookup keeps the three parametrized dispatches identical.
        return {'answer': answer, 'seek': seek, 'replay': replay}[mode]

    def expectation(screen, *, ok=True, error_kind=None, seq=1):
        # Replay facts come from the trace-owned serializer; the entry's
        # operation label is never part of a replay expectation.
        return replay_expectation(TraceEntry(
            seq=seq,
            operation='answer',
            phase=None,
            submitted={},
            ok=ok,
            screen=screen,
            identity=screen_identity(screen),
            content=screen_content(screen),
            error=None if error_kind is None else {'kind': error_kind},
        ))
"""


#: Shared fixture prefix: an ``entry`` code block plus the ``id: submission``
#: screen that most acceptance scenarios answer first.
SUBMISSION_FIXTURE = """\
---
mandatory: True
code: |
  entry
---
id: submission
"""


def _submission_interview(body: str) -> str:
    """Prefix the shared submission fixture onto a dedented interview body."""
    return SUBMISSION_FIXTURE + textwrap.dedent(body)


def _execute(family: RuntimeFamily, root: Path, interview: str, scenario: str) -> None:
    package = root / "docassemble" / "acceptance"
    questions = package / "data" / "questions"
    questions.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (questions / "main.yml").write_text(textwrap.dedent(interview))
    completed = subprocess.run(
        [
            str(family.interpreter),
            "-c",
            EXECUTION_SETUP + textwrap.indent(textwrap.dedent(scenario), "    "),
            str(root),
        ],
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stderr == "", completed.stderr


@pytest.mark.parametrize("datatype", ["integer", "number", "text", "yesnoradio"])
def test_required_explicit_blank_answer_and_replay_override(
    family_python,  # noqa: F811 - fixture registered by import
    tmp_path,
    datatype,
):
    """Replay accepts a raw empty numeric that ordinary Answer rejects.

    Observed matrix for the raw submitted value of a visible required field:

    - ``''`` on an integer/number field is rejected by ordinary ``Answer`` as
      an explicit empty required value, but a replay override accepts it after
      browser-style coercion to ``0``/``0.0``.
    - The literal ``None`` (submitted as ``'None'``) rejects in both modes.
    - Empty text and empty yesnoradio reject in both modes.

    Replay losing the explicit-blank intent is a possible acceptance defect;
    aligning it with ordinary ``Answer`` (or sanctioning the difference) is a
    separate decision.
    """
    _execute(
        family_python,
        tmp_path,
        _submission_interview(f"""
        question: Required input
        fields:
          - Entry: entry
            datatype: {datatype}
        validation code: |
          validation_ran = True
        ---
        mandatory: True
        question: Accepted
        """),
        f"""
        numeric = {datatype in ("integer", "number")!r}
        for raw in ('', 'None'):
            # Only empty raw numeric input coerces to zero; literal None stays None.
            replay_accepts = numeric and raw == ''
            started = execution.run(Start())
            assert started.ok, started
            before = execution.run(Status()).result
            rejected = execution.run(Answer((('entry', raw),)))
            assert not rejected.ok and rejected.error.kind == 'validation', rejected
            assert rejected.error.details['missing_required'] == ['entry'], rejected
            assert 'is empty' in rejected.error.details['errors'][0], rejected
            assert execution.run(Status()).result == before
            assert value("('entry' in globals(), 'validation_ran' in globals())") == '(False, False)'

            replay = execution.run(ReplayAutomatic(
                'entry', submissions=((('entry', raw),),), activate=True,
                expectations=(expectation(started.result, ok=replay_accepts,
                    error_kind=None if replay_accepts else 'validation'),),
            ))
            assert replay.ok, replay
            assert 'replay_divergence' not in replay.result, replay
            assert replay.result['auto_trace'][0]['ok'] is replay_accepts, replay
            if replay_accepts:
                assert value('entry') == {("0" if datatype == "integer" else "0.0")!r}
                assert value('validation_ran') == 'True'
                assert execution.run(Status()).result['question_text'] == 'Accepted'
            else:
                assert replay.result['auto_stop']['missing_required'] == ['entry'], replay
                assert value("('entry' in globals(), 'validation_ran' in globals())") == '(False, False)'
        """,
    )


@pytest.mark.parametrize("mode", ["answer", "seek", "replay"])
def test_submission_settles_defaults_and_visibility_before_validation(
    family_python,  # noqa: F811 - fixture registered by import
    tmp_path,
    mode,
):
    _execute(
        family_python,
        tmp_path,
        _submission_interview("""
        question: Settling defaults
        fields:
          - Earlier default: removed_default
            default: remove me
            maxlength: 2
            show if:
              code: globals().get('show_old', True)
          - Hide earlier default: show_old
            datatype: yesno
            default: False
          - Reveal later default: show_more
            datatype: yesno
            default: True
          - Entry: entry
            default: prefilled
            show if: show_more
          - Optional: optional
            required: False
          - Hidden required: hidden_required
            show if: show_old
        validation code: |
          validated_values = (entry, optional, defined('removed_default'), defined('hidden_required'))
        ---
        mandatory: True
        question: Accepted
        """),
        f"""
        mode = {mode!r}
        for assignments in ((), (('entry', 'explicit'), ('show_more', 'False'))):
            if mode == 'seek' and assignments:
                continue  # Seek has no assignment override mode.
            started = execution.run(Start())
            assert started.ok, started
            accepted = execution.run(select_operation(
                mode,
                answer=Answer(assignments, defaults=True),
                seek=Seek('entry', auto=True, activate=True),
                replay=ReplayAutomatic('entry', submissions=(assignments,), activate=True,
                    expectations=(expectation(started.result),)),
            ))
            assert accepted.ok, accepted
            assert 'replay_divergence' not in accepted.result, accepted
            assert execution.run(Status()).result['question_text'] == 'Accepted'
            expected_entry = 'explicit' if assignments else 'prefilled'
            assert value('validated_values') == repr((expected_entry, '', False, False))
            # An explicit value survives even when its controller hides it.
            assert value('entry') == repr(expected_entry)
            assert value("(show_old, show_more)") == repr((False, not bool(assignments)))
        """,
    )


@pytest.mark.parametrize("mode", ["answer", "seek", "replay"])
def test_submission_rejects_unstable_default_visibility_without_saving(
    family_python,  # noqa: F811 - fixture registered by import
    tmp_path,
    mode,
):
    _execute(
        family_python,
        tmp_path,
        _submission_interview("""
        question: Unstable default
        fields:
          - Entry: entry
            datatype: yesno
            default: False
            show if:
              code: globals().get('entry', True)
        validation code: |
          validation_ran = True
        """),
        f"""
        started = execution.run(Start())
        assert started.ok, started
        before = execution.run(Status()).result
        rejected = execution.run(select_operation(
            {mode!r},
            answer=Answer((), defaults=True),
            seek=Seek('entry', auto=True, activate=True),
            replay=ReplayAutomatic('entry', submissions=((),), activate=True,
                expectations=(expectation(started.result),)),
        ))
        assert not rejected.ok and rejected.error.kind == 'validation', rejected
        assert rejected.error.message == 'rendered defaults have unstable visibility', rejected
        assert execution.run(Status()).result == before
        assert value("('entry' in globals(), 'validation_ran' in globals())") == '(False, False)'
        """,
    )


def test_early_input_rejection_uses_assignments_and_defaults_but_not_blanks_or_validation(
    family_python,  # noqa: F811 - fixture registered by import
    tmp_path,
):
    _execute(
        family_python,
        tmp_path,
        _submission_interview(f"""
        question: Early guidance
        fields:
          - Entry: entry
          - Filing date: filing_date
            datatype: date
            default: '2026-01-15'
          - Optional date: optional_date
            datatype: date
            required: False
          - Optional controller: gate
            datatype: yesno
            required: False
          - Dependent: dependent
            show if:
              code: globals().get('gate', True)
          - Hidden by assignment: hidden_required
            show if:
              code: globals().get('entry') != 'Ada'
        validation code: |
          with open({str(tmp_path / "authored-validation")!r}, 'w') as marker:
            marker.write('ran')
          validation_error('Authored rejection')
        """),
        """
        started = execution.run(Start())
        assert started.ok, started
        before = execution.run(Status()).result
        assignments = (('entry', 'Ada'), ('enrty', 'typo'), ('optional_date', 'bad-date'))
        rejected = execution.run(Answer(assignments))
        assert not rejected.ok and rejected.error.kind == 'answer-input', rejected
        details = rejected.error.details
        assert [item['disposition'] for item in details['assignments']] == [
            'applied', 'unknown field', 'parse error',
        ], details
        # A synthesized gate=False would incorrectly hide this missing field.
        assert details['missing_required'] == ['dependent'], details
        assert details['closest_matches'] == {'enrty': 'entry'}, details
        assert 'filing_date' in details['active_fields'], details
        assert not (root / 'authored-validation').exists()
        assert execution.run(Status()).result == before
        assert value("any(name in globals() for name in ('entry', 'filing_date', 'gate', 'optional_date'))") == 'False'

        # Replay reports dispositions but intentionally has no ordinary-answer
        # missing-required guidance on assignment rejection.
        replay = execution.run(ReplayAutomatic(
            'entry', submissions=(assignments,), activate=True,
            expectations=(expectation(started.result, ok=False, error_kind='answer-input'),),
        ))
        assert replay.ok and 'replay_divergence' not in replay.result, replay
        stop = replay.result['auto_stop']
        assert stop['reason'] == 'answer failed' and stop['submitted'] is False, stop
        assert [item['disposition'] for item in stop['assignments']] == [
            'applied', 'unknown field', 'parse error',
        ], stop
        assert 'missing_required' not in stop
        assert not (root / 'authored-validation').exists()
        assert value("any(name in globals() for name in ('entry', 'filing_date', 'gate', 'optional_date'))") == 'False'
        assert execution.run(Status()).result['question_name'] == 'ID submission'
        """,
    )


@pytest.mark.parametrize("mode", ["answer", "seek", "replay"])
@pytest.mark.parametrize("gate", ["required", "constraint", "authored", "accepted"])
def test_submission_checks_required_then_constraints_then_authored_validation(
    family_python,  # noqa: F811 - fixture registered by import
    tmp_path,
    mode,
    gate,
):
    entry_default = "" if gate == "required" else f"    default: {gate}\n"
    amount_default = 1 if gate in {"required", "constraint"} else 2
    interview = _submission_interview(
        "question: Ordered gates\nfields:\n"
        "  - Entry: entry\n"
        + entry_default
        + "  - Amount: amount\n    datatype: integer\n    min: 2\n"
        f"    default: {amount_default}\n"
        "  - Optional: optional\n    required: False\n"
        "validation code: |\n"
        f"  with open({str(tmp_path / 'authored-validation')!r}, 'w') as marker:\n"
        "    marker.write('ran')\n"
        "  validation_observed = (entry, amount, optional)\n"
        "  if entry != 'accepted':\n"
        "    validation_error('Authored rejection')\n"
        "---\nmandatory: True\nquestion: Accepted\n"
    )
    _execute(
        family_python,
        tmp_path,
        interview,
        f"""
        mode, gate = {mode!r}, {gate!r}
        started = execution.run(Start())
        assert started.ok, started
        before = execution.run(Status()).result
        outcome = execution.run(select_operation(
            mode,
            answer=Answer((), defaults=True),
            seek=Seek('entry', auto=True, activate=True),
            replay=ReplayAutomatic('entry', submissions=((),), activate=True,
                expectations=(expectation(started.result, ok=gate == 'accepted',
                    error_kind=None if gate == 'accepted' else 'validation'),)),
        ))
        assert (root / 'authored-validation').exists() is (gate in ('authored', 'accepted'))
        if gate == 'accepted':
            assert outcome.ok, outcome
            assert 'replay_divergence' not in outcome.result, outcome
            assert value('validation_observed') == "('accepted', 2, '')"
            assert execution.run(Status()).result['question_text'] == 'Accepted'
        else:
            if mode == 'answer':
                assert not outcome.ok and outcome.error.kind == 'validation', outcome
                details = outcome.error.details
                assert execution.run(Status()).result == before
            else:
                assert outcome.ok and 'replay_divergence' not in outcome.result, outcome
                assert outcome.result['auto_submitted'] == [], outcome
                details = outcome.result['auto_stop']
                assert details['submitted'] is False, details
                assert details['reason'] == (
                    'required fields missing' if gate == 'required' else 'validation failed'
                ), details
            expected_errors = {{
                'required': ["required field 'entry' is undefined (required while visible at submission)"],
                'constraint': ["field 'amount' violates min constraint (2)"],
                'authored': ['Authored rejection'],
            }}
            assert details['errors'] == expected_errors[gate], details
            assert value("any(name in globals() for name in ('entry', 'amount', 'optional', 'validation_observed'))") == 'False'
            assert execution.run(Status()).result['question_name'] == 'ID submission'
        """,
    )


def test_answer_partial_strict_and_no_validate_keep_distinct_acceptance_rules(
    family_python,  # noqa: F811 - fixture registered by import
    tmp_path,
):
    _execute(
        family_python,
        tmp_path,
        _submission_interview("""
        question: Mode differences
        fields:
          - Entry: entry
          - Amount: amount
            datatype: integer
            default: 2
            min: 2
            required: False
          - Optional: optional
            required: False
        validation code: |
          validation_ran = True
          if defined('entry') and entry == 'bad':
            validation_error('Authored rejection')
        ---
        mandatory: True
        question: Accepted
        """),
        """
        assert execution.run(Start()).ok
        before = execution.run(Status()).result
        strict = execution.run(Answer((('amount', '1'),), partial=True, strict=True))
        assert not strict.ok and strict.error.kind == 'validation', strict
        assert strict.error.details['errors'] == [
            "required field 'entry' is undefined (required while visible at submission)"
        ], strict
        assert strict.error.details['warnings'] == [], strict
        assert execution.run(Status()).result == before
        assert value("('amount' in globals(), 'validation_ran' in globals())") == '(False, False)'

        partial = execution.run(Answer((('amount', '1'),), partial=True))
        assert partial.ok, partial
        assert partial.result['warnings'] == strict.error.details['errors'], partial
        assert value("(amount, 'optional' in globals(), validation_ran)") == '(1, False, True)'
        assert execution.run(Status()).result['question_name'] == 'ID submission'

        assert execution.run(Start()).ok
        partial_unvalidated = execution.run(Answer(
            (('amount', '1'),), partial=True, strict=True, validate=False,
        ))
        assert partial_unvalidated.ok and 'warnings' not in partial_unvalidated.result, partial_unvalidated
        assert value("(amount, 'optional' in globals(), 'validation_ran' in globals())") == '(1, False, False)'

        assert execution.run(Start()).ok
        missing = execution.run(Answer((), defaults=True, validate=False))
        assert not missing.ok and missing.error.details['missing_required'] == ['entry'], missing
        constrained = execution.run(Answer((('entry', 'bad'), ('amount', '1')), validate=False))
        assert not constrained.ok and constrained.error.details['errors'] == [
            "field 'amount' violates min constraint (2)"
        ], constrained
        authored = execution.run(Answer((('entry', 'bad'),)))
        assert not authored.ok and authored.error.details['errors'] == ['Authored rejection'], authored
        accepted = execution.run(Answer((('entry', 'bad'),), validate=False))
        assert accepted.ok, accepted
        assert value("(entry, amount, optional, 'validation_ran' in globals())") == "('bad', 2, '', False)"
        """,
    )


@pytest.mark.parametrize("datatype", ["object_checkboxes", "object_multiselect"])
def test_replay_object_group_updates_only_rendered_choices(
    family_python,  # noqa: F811 - fixture registered by import
    tmp_path,
    datatype,
):
    _execute(
        family_python,
        tmp_path,
        f"""
        ---
        objects:
          - alice: DAObject
          - bob: DAObject
          - carol: DAObject
        ---
        mandatory: True
        code: |
          if not defined('initialized'):
            alice.name = 'Alice'
            bob.name = 'Bob'
            carol.name = 'Carol'
            shortlist = DAList('shortlist', auto_gather=False)
            shortlist.append(bob)
            shortlist.append(carol)
            initialized = True
          entry
        ---
        id: submission
        question: Shortlist
        fields:
          - Entry: entry
            default: done
          - Shortlist: shortlist
            datatype: {datatype}
            choices: |
              [alice, carol]
        ---
        mandatory: True
        question: Accepted
        """,
        """
        started = execution.run(Start())
        assert started.ok, started
        assert value('[person.name for person in shortlist.elements]') == "['Bob', 'Carol']"
        choices = started.result['fields'][1]['choices']
        alice_choice = next(choice['value'] for choice in choices if choice['label'] == 'Alice')
        assignments = (('shortlist', json.dumps([alice_choice])),)
        accepted = execution.run(ReplayAutomatic(
            'entry', submissions=(assignments,), activate=True,
            expectations=(expectation(started.result),),
        ))
        assert accepted.ok and 'replay_divergence' not in accepted.result, accepted
        members = value('[person.name for person in shortlist.elements]')
        assert members == "['Bob', 'Alice']", (members, choices, accepted)
        assert value('shortlist.gathered') == 'True'
        assert execution.run(Status()).result['question_text'] == 'Accepted'

        # Unknown object choices reject the entire attempted selection/default.
        started = execution.run(Start())
        rejected = execution.run(ReplayAutomatic(
            'entry', submissions=((('shortlist', '["not-a-choice"]'),),), activate=True,
            expectations=(expectation(started.result, ok=False, error_kind='answer-input'),),
        ))
        assert rejected.ok and 'replay_divergence' not in rejected.result, rejected
        stop = rejected.result['auto_stop']
        assert stop['reason'] == 'answer failed', stop
        assert 'unknown object choice' in stop['errors'][0], stop
        assert value('[person.name for person in shortlist.elements]') == "['Bob', 'Carol']"
        assert value("'entry' in globals()") == 'False'
        """,
    )


@pytest.mark.parametrize("datatype", ["object_checkboxes", "object_multiselect"])
def test_automatic_submission_gathers_required_object_groups_without_choices(
    family_python,  # noqa: F811 - fixture registered by import
    tmp_path,
    datatype,
):
    _execute(
        family_python,
        tmp_path,
        _submission_interview(f"""
        question: No choices
        fields:
          - Entry: entry
            default: done
          - Shortlist: shortlist
            datatype: {datatype}
            choices: |
              []
        ---
        mandatory: True
        question: Accepted
        """),
        """
        for mode in ('seek', 'replay'):
            started = execution.run(Start())
            assert started.ok, started
            operation = (
                Seek('entry', auto=True, activate=True) if mode == 'seek' else
                ReplayAutomatic('entry', submissions=((),), activate=True,
                    expectations=(expectation(started.result),))
            )
            accepted = execution.run(operation)
            assert accepted.ok and 'replay_divergence' not in accepted.result, accepted
            assert value('(len(shortlist.elements), shortlist.gathered)') == '(0, True)'
            assert execution.run(Status()).result['question_text'] == 'Accepted'
        """,
    )


def test_replay_commits_all_accepted_steps_only_when_activated(
    family_python,  # noqa: F811 - fixture registered by import
    tmp_path,
):
    _execute(
        family_python,
        tmp_path,
        """
        ---
        id: saved
        mandatory: True
        question: Saved screen
        fields:
          - Value: saved_value
        ---
        id: first
        mandatory: True
        question: First automatic screen
        fields:
          - Value: first_value
            default: first
        ---
        id: second
        mandatory: True
        question: Second automatic screen
        fields:
          - Value: second_value
            default: second
        ---
        mandatory: True
        code: |
          target_value = first_value + second_value
        ---
        mandatory: True
        question: Done
        """,
        """
        assert execution.run(Start()).ok
        before = execution.run(Status()).result
        recorded = execution.run(Seek('target_value', auto=True))
        assert recorded.ok, recorded
        steps = recorded.result['auto_trace']
        assert [step['screen']['question_name'] for step in steps] == ['ID first', 'ID second'], recorded
        assert all(step['ok'] for step in steps), recorded
        expectations = tuple(expectation(step['screen'], seq=index + 1) for index, step in enumerate(steps))
        trailing = expectation(recorded.result, seq=3)
        assert execution.run(Status()).result == before
        assert value("any(name in globals() for name in ('first_value', 'second_value', 'target_value'))") == 'False'

        for activate in (False, True):
            # The first default is accepted before the second override rejects.
            # Even an activated walk cannot save that accepted prefix when its
            # expected acceptance changes to a required-field rejection.
            rejected = execution.run(ReplayAutomatic(
                'target_value', submissions=((), (('second_value', ''),)),
                activate=activate, expectations=expectations, trailing=trailing,
            ))
            assert rejected.ok, rejected
            divergence = rejected.result['replay_divergence']
            assert (divergence['phase'], divergence['seq']) == ('after-submission', 2), rejected
            assert divergence['observation']['error']['kind'] == 'validation', rejected
            assert [step['ok'] for step in rejected.result['auto_trace']] == [True, False], rejected
            assert execution.run(Status()).result == before
            assert value("any(name in globals() for name in ('first_value', 'second_value', 'target_value'))") == 'False'

        for activate in (False, True):
            accepted = execution.run(ReplayAutomatic(
                'target_value', submissions=((), ()), activate=activate,
                expectations=expectations, trailing=trailing,
            ))
            assert accepted.ok and 'replay_divergence' not in accepted.result, accepted
            assert [step['ok'] for step in accepted.result['auto_trace']] == [True, True], accepted
            if activate:
                assert execution.run(Status()).result == accepted.result
                assert value('(first_value, second_value, target_value)') == "('first', 'second', 'firstsecond')"
            else:
                assert execution.run(Status()).result == before
                assert value("any(name in globals() for name in ('first_value', 'second_value', 'target_value'))") == 'False'
        """,
    )


def test_rejected_answer_preserves_accepted_saved_session(
    family_python,  # noqa: F811 - fixture registered by import
    tmp_path,
):
    """An accepted screen survives a rejected answer on the following screen."""
    _execute(
        family_python,
        tmp_path,
        _submission_interview("""
        question: First screen
        fields:
          - Entry: entry
            default: first
        ---
        mandatory: True
        question: Second screen
        fields:
          - Required: second_value
        ---
        mandatory: True
        question: Done
        """),
        """
        assert execution.run(Start()).ok
        accepted = execution.run(Answer((), defaults=True))
        assert accepted.ok, accepted
        before = execution.run(Status()).result
        assert before['question_text'] == 'Second screen', before
        assert value('entry') == "'first'"

        rejected = execution.run(Answer((('second_value', ''),)))
        assert not rejected.ok and rejected.error.kind == 'validation', rejected
        assert rejected.error.details['missing_required'] == ['second_value'], rejected
        assert execution.run(Status()).result == before
        assert value('entry') == "'first'"
        assert value("'second_value' in globals()") == 'False'
        """,
    )
