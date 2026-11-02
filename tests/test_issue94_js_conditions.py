"""Issue #93 count-switch through the shared Answer/automatic acceptance seam."""

import pytest
from test_real_runtime import (  # noqa: F401
    family_python,
    real_python,
    real_python_19,
)
from test_submission_acceptance import _execute, _submission_interview

pytestmark = pytest.mark.real_runtime

COUNT_SCREEN = """
question: Children
fields:
  - Count: entry
    datatype: integer
    min: 1
  - Singular: singular
    datatype: yesnoradio
    js show if: val("entry") == 1
  - Plural: plural
    datatype: yesnoradio
    js show if: val("entry") > 1
  - Unrelated: unrelated
---
mandatory: True
question: Accepted
"""


@pytest.mark.parametrize(
    "count, selected, hidden", [(1, "singular", "plural"), (2, "plural", "singular")]
)
@pytest.mark.parametrize("mode", ["answer", "replay"])
def test_parse_standardformatter_app_js_count_switch_submission_and_rollback(
    family_python,  # noqa: F811
    tmp_path,
    count,
    selected,
    hidden,
    mode,
):
    _execute(
        family_python,
        tmp_path,
        _submission_interview(COUNT_SCREEN),
        f"""
        started = execution.run(Start())
        assert started.ok, started
        before = execution.run(Status()).result
        assert before['fields'][1]['visible'] is False
        assert before['fields'][2]['visible'] is False
        execution = InterviewExecution(root, 'docassemble.acceptance:data/questions/main.yml')
        rejected = execution.run(Answer((('entry', '{count}'),)))
        assert not rejected.ok and rejected.error.kind == 'validation', rejected
        assert rejected.error.details['missing_required'] == [{selected!r}, 'unrelated'], rejected
        assert execution.run(Status()).result == before
        assert value("'entry' in globals()") == 'False'
        assignments = (('entry', '{count}'), ({selected!r}, 'true'), ('unrelated', 'ok'))
        if {mode!r} == 'answer':
            accepted = execution.run(Answer(assignments))
            assert accepted.ok, accepted
        else:
            accepted = execution.run(ReplayAutomatic(
                'entry', submissions=(assignments,), activate=True,
                expectations=(expectation(started.result),),
            ))
            assert accepted.ok and 'replay_divergence' not in accepted.result, accepted
            assert accepted.result['auto_trace'][0]['ok'] is True, accepted
        assert value({selected!r}) == 'True'
        assert value("'{hidden}' in globals()") == 'False'
    """,
    )


@pytest.mark.parametrize(
    "options", ["validate=False", "partial=True", "partial=True, strict=True", ""]
)
def test_unsupported_dead_branch_is_typed_capability_failure_not_validation_bypass(
    family_python,  # noqa: F811
    tmp_path,
    options,
):
    body = COUNT_SCREEN.replace('val("entry") == 1', 'true || arbitrary(val("entry"))')
    _execute(
        family_python,
        tmp_path,
        _submission_interview(body),
        f"""
        started = execution.run(Start())
        assert started.ok, started
        field = started.result['fields'][1]
        assert field['visible'] is None
        assert field['js_condition_diagnostic']['code'] == 'call'
        before = execution.run(Status()).result
        rejected = execution.run(Answer((('entry', '2'),), {options}))
        assert not rejected.ok and rejected.error.kind == 'js-condition-unsupported', rejected
        assert execution.run(Status()).result == before
        assert value("'entry' in globals()") == 'False'
        auto = execution.run(ReplayAutomatic(
            'entry', submissions=((('entry', '2'),),), activate=True,
            expectations=(expectation(started.result, ok=False, error_kind='js-condition-unsupported'),),
        ))
        assert auto.ok and 'replay_divergence' not in auto.result, auto
        attempt = auto.result['auto_trace'][0]
        assert attempt['ok'] is False
        assert attempt['submitted'] == {{'entry': '2'}}
        assert attempt['error']['kind'] == 'js-condition-unsupported'
        assert attempt['error']['details']['diagnostic'] == rejected.error.details['diagnostic']
        assert attempt['screen']['fields'] == before['fields']
        assert auto.result['auto_stop']['reason'] == 'unsupported JS condition'
        assert auto.result['auto_stop']['submitted'] is False
        assert value("'entry' in globals()") == 'False'
        assert execution.run(Status()).result['fields'] == before['fields']
        # Untouched automatic seeking also records the rejected attempt, without saving.
        saved = execution.run(Status()).result
        walked = execution.run(Seek('entry', auto=True))
        assert walked.ok, walked
        assert walked.result['auto_trace'][0]['error']['kind'] == 'js-condition-unsupported'
        assert walked.result['auto_trace'][0]['ok'] is False
        assert execution.run(Status()).result == saved
    """,
    )


def test_ask_time_mako_expression_and_trigger_vars_survive_saved_reload(
    family_python,  # noqa: F811
    tmp_path,
):
    interview = """
    ---
    initial: True
    code: |
      render_count = globals().get('render_count', 0) + 1
      threshold = render_count
    ---
    mandatory: True
    code: |
      entry
    ---
    question: Frozen
    fields:
      - Count: entry
        datatype: integer
      - Answer: dependent
        js show if: val("entry") == ${threshold}
      - Threshold: threshold
        datatype: integer
    ---
    mandatory: True
    question: Accepted
    """
    _execute(
        family_python,
        tmp_path,
        interview,
        """
        started = execution.run(Start())
        assert started.ok, started
        captured = started.result['fields'][1]['js_condition']
        assert captured['expression'] == 'val("entry") == 1', captured
        assert captured['vars'] == ['entry']
        execution = InterviewExecution(root, 'docassemble.acceptance:data/questions/main.yml')
        # Changing a separate rendered Mako input must not rerender the source.
        rejected = execution.run(Answer((('entry', '1'), ('threshold', '2'))))
        assert not rejected.ok and rejected.error.details['missing_required'] == ['dependent'], rejected
        assert value('threshold') == '1'
        assert execution.run(Status()).result['fields'][1]['js_condition'] == captured
        accepted = execution.run(Answer((('entry', '1'), ('threshold', '2'), ('dependent', 'ok'))))
        assert accepted.ok, accepted
    """,
    )


def test_defaults_optional_blanks_and_constraints_share_js_visibility(
    family_python,  # noqa: F811
    tmp_path,
):
    body = """
    question: Defaults
    fields:
      - Count: entry
        datatype: integer
        default: 2
      - Singular: singular
        js show if: val("entry") == 1
      - Plural: plural
        default: all
        js show if: val("entry") > 1
      - Optional: optional
        required: False
        js show if: val("entry") > 1
      - Constrained: constrained
        required: False
        maxlength: 2
        js show if: val("entry") == 1
    ---
    mandatory: True
    question: Accepted
    """
    _execute(
        family_python,
        tmp_path,
        _submission_interview(body),
        """
        for mode in ('answer', 'seek'):
            started = execution.run(Start())
            assert started.ok, started
            assert started.result['fields'][1]['visible'] is False
            assert started.result['fields'][2]['visible'] is True
            operation = Answer((), defaults=True) if mode == 'answer' else Seek('entry', auto=True, activate=True)
            accepted = execution.run(operation)
            assert accepted.ok, accepted
            if mode == 'seek':
                assert accepted.result['auto_trace'][0]['ok'] is True, accepted
            assert value('entry') == '2'
            assert value('plural') == "'all'"
            assert value('optional') == "''"
            assert value("('singular' in globals(), 'constrained' in globals())") == '(False, False)'
        # Explicit hidden assignments retain ADR-0013 precedence and skip constraints.
        assert execution.run(Start()).ok
        accepted = execution.run(Answer((('entry', '2'), ('constrained', 'too long'))))
        assert accepted.ok, accepted
        assert value('constrained') == "'too long'"
        assert execution.run(Start()).ok
        rejected = execution.run(Answer((('entry', '1'), ('singular', 'one'), ('constrained', 'too long'))))
        assert not rejected.ok and rejected.error.kind == 'validation', rejected
    """,
    )


def test_standardformatter_no_trigger_hide_if_and_missing_controller(
    family_python,  # noqa: F811
    tmp_path,
):
    body = """
    question: Triggers
    fields:
      - Entry: entry
      - Constant show: constant_show
        js show if: "true"
      - Constant hide: constant_hide
        js hide if: "false"
      - Namespace only: namespace_only
        js hide if: val("namespace_value") == 1
      - Missing second controller: dependent
        js show if: val("entry") == "yes" && !val("namespace_value")
    ---
    mandatory: True
    question: Accepted
    """
    interview = (
        "---\ninitial: True\ncode: |\n  namespace_value = 1\n"
        + _submission_interview(body)
    )
    _execute(
        family_python,
        tmp_path,
        interview,
        """
        started = execution.run(Start())
        assert started.ok, started
        for field in started.result['fields'][1:4]:
            assert field['visible'] is False and field['js_condition_state'] == 'no-trigger', field
        rejected = execution.run(Answer((('entry', 'yes'),)))
        assert not rejected.ok and rejected.error.details['missing_required'] == ['dependent'], rejected
        accepted = execution.run(Answer((('entry', 'yes'), ('dependent', 'ok'))))
        assert accepted.ok, accepted
    """,
    )


def test_numeric_spelling_is_browser_string_not_python_number(
    family_python,  # noqa: F811
    tmp_path,
):
    body = """
    question: Numeric string
    fields:
      - Count: entry
        datatype: number
      - Answer: dependent
        js show if: val("entry") === "1.0"
    ---
    mandatory: True
    question: Accepted
    """
    _execute(
        family_python,
        tmp_path,
        _submission_interview(body),
        """
        started = execution.run(Start())
        assert started.ok, started
        rejected = execution.run(Answer((('entry', '1.0'),)))
        assert not rejected.ok and rejected.error.details['missing_required'] == ['dependent'], rejected
        accepted = execution.run(Answer((('entry', '1.0'), ('dependent', 'ok'))))
        assert accepted.ok, accepted
    """,
    )


def test_legacy_saved_screen_without_js_metadata_remains_loadable(
    family_python,  # noqa: F811
    tmp_path,
):
    _execute(
        family_python,
        tmp_path,
        _submission_interview(COUNT_SCREEN),
        """
        from docassemble_simulator.execution import StateStore, Refresh
        assert execution.run(Start()).ok
        store = StateStore(root, execution.scope.interview)
        payload = store.load()
        legacy = payload['outcome']
        for field in legacy['fields']:
            for key in list(field):
                if key.startswith('js_condition') or key == 'submission_required':
                    del field[key]
            # Legacy simulator treated all JS fields as visible and required.
            field['visible'] = True
            field['required'] = True
        store.save(payload['namespace'], legacy)
        execution = InterviewExecution(root, 'docassemble.acceptance:data/questions/main.yml')
        assert execution.run(Status()).result == legacy
        refreshed = execution.run(Refresh())
        assert refreshed.ok, refreshed
        assert 'js_condition' in refreshed.result['fields'][1]
    """,
    )


def test_app_js_radio_controller_boolean_is_not_python_numeric_equality(
    family_python,  # noqa: F811
    tmp_path,
):
    body = """
    question: Radio controller
    fields:
      - Entry: entry
        datatype: yesnoradio
      - Boolean: boolean_answer
        js show if: val("entry") === true
      - Loose: loose_answer
        js show if: val("entry") == 1
      - Strict numeric: numeric_answer
        js show if: val("entry") === 1
    ---
    mandatory: True
    question: Accepted
    """
    _execute(
        family_python,
        tmp_path,
        _submission_interview(body),
        """
        assert execution.run(Start()).ok
        rejected = execution.run(Answer((('entry', 'true'),)))
        assert not rejected.ok and rejected.error.details['missing_required'] == ['boolean_answer', 'loose_answer'], rejected
        accepted = execution.run(Answer((('entry', 'true'), ('boolean_answer', 'yes'), ('loose_answer', 'yes'))))
        assert accepted.ok, accepted
        assert value("'numeric_answer' in globals()") == 'False'
    """,
    )


def test_parse_rejects_authored_native_and_js_conditions_on_same_field(
    family_python,  # noqa: F811
    tmp_path,
):
    body = """
    question: Forbidden condition mix
    fields:
      - Count: entry
        datatype: integer
      - Answer: dependent
        show if:
          variable: native_control
          is: "yes"
        js show if: val("entry") == 1
    """
    _execute(
        family_python,
        tmp_path,
        _submission_interview(body),
        """
        started = execution.run(Start())
        assert not started.ok and started.error.kind == 'compile', started
        assert 'You cannot mix js show if and non-js show if' in started.error.message
    """,
    )


@pytest.mark.parametrize("condition", ["show", "enable"])
def test_unsupported_js_guidance_preserves_original_assignment_rejection(
    family_python,  # noqa: F811
    tmp_path,
    condition,
):
    _execute(
        family_python,
        tmp_path,
        _submission_interview(f"""
        question: Combined input and capability errors
        fields:
          - Entry: entry
          - Date: filing_date
            datatype: date
          - Dependent: dependent
            js {condition} if: custom(val('entry'))
        validation code: |
          validation_ran = True
        """),
        """
        started = execution.run(Start())
        assert started.ok, started
        rejected = execution.run(Answer((('entry', 'ok'), ('enrty', 'typo'), ('filing_date', 'bad-date'))))
        assert not rejected.ok and rejected.error.kind == 'answer-input', rejected
        details = rejected.error.details
        assert [item['disposition'] for item in details['assignments']] == ['applied', 'unknown field', 'parse error'], details
        assert len(details['errors']) == 2, details
        assert details['active_fields'] == ['entry', 'filing_date', 'dependent'], details
        assert details['active_field_details'] == started.result['fields'], details
        assert details['closest_matches'] == {'enrty': 'entry'}, details
        assert details['diagnostic']['kind'] == 'js-condition-unsupported', details
        assert details['diagnostic']['code'] == 'call', details
        assert 'missing_required' not in details, details
        assert execution.run(Status()).result == started.result
        assert value("any(name in globals() for name in ('entry', 'filing_date', 'validation_ran'))") == 'False'
        """,
    )
