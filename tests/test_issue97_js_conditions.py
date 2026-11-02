"""Separate enablement and settled submission eligibility on both runtimes (#97)."""

import pytest
from test_real_runtime import family_python, real_python, real_python_19  # noqa: F401
from test_submission_acceptance import _execute, _submission_interview

pytestmark = pytest.mark.real_runtime


@pytest.mark.parametrize("condition", ["enable", "disable"])
@pytest.mark.parametrize("enabled", [False, True])
def test_country_state_required_optional_default_constraint_matrix_and_reload(
    family_python,  # noqa: F811
    tmp_path,
    condition,
    enabled,
):
    country = "US" if enabled == (condition == "enable") else ""
    _execute(
        family_python,
        tmp_path,
        _submission_interview(f"""
        question: Country and state scalar controls
        fields:
          - Country: entry
            required: False
            choices:
              - US
              - CA
          - State: state
            js {condition} if: val('entry')
          - Optional: optional
            required: False
            js {condition} if: val('entry')
          - Default: prefilled
            default: seed
            js {condition} if: val('entry')
          - Length: length
            datatype: integer
            required: False
            min: 2
            js {condition} if: val('entry')
          - Unrelated: unrelated
        ---
        mandatory: True
        question: Accepted
        """),
        f"""
        started = execution.run(Start())
        assert started.ok, started
        fields = started.result['fields']
        assert all(f['visible'] is True for f in fields), fields
        initial = {condition!r} == 'disable'
        assert fields[1]['enabled'] is initial, fields
        assert fields[1]['submission_required'] is True
        execution = InterviewExecution(root, 'docassemble.acceptance:data/questions/main.yml')
        assert execution.run(Status()).result == started.result
        assignments = (('entry', {country!r}),)
        rejected = execution.run(Answer(assignments))
        assert not rejected.ok, rejected
        expected = ['state', 'unrelated'] if {enabled!r} else ['unrelated']
        assert rejected.error.details['missing_required'] == expected, rejected
        early = execution.run(Answer(assignments + (('wrong', 'x'),)))
        assert early.error.details['missing_required'] == expected, early
        assert execution.run(Status()).result == started.result
        assert value("('entry' in globals(), 'prefilled' in globals())") == '(False, False)'
        # Explicit disabled assignments remain authorized. Rendered constraints
        # still skip them; re-enabling restores those constraints.
        attempted = execution.run(Answer(assignments + (('state', 'MO'), ('unrelated', 'ok'), ('length', '1'))))
        if {enabled!r}:
            assert not attempted.ok and 'min constraint' in str(attempted.error), attempted
            assert value("'length' in globals()") == 'False'
            accepted = execution.run(Answer(assignments + (('state', 'MO'), ('unrelated', 'ok'), ('length', '2'))))
            assert accepted.ok, accepted
            assert value('(state, optional, prefilled, length)') == "('MO', '', 'seed', 2)"
        else:
            assert attempted.ok, attempted
            assert value('(state, length)') == "('MO', 1)"
            assert value("('optional' in globals(), 'prefilled' in globals())") == '(False, False)'
        """,
    )


@pytest.mark.parametrize("mode", ["answer", "seek", "replay"])
def test_enablement_default_reversal_ineligible_controllers_and_automatic_paths(
    family_python,  # noqa: F811
    tmp_path,
    mode,
):
    _execute(
        family_python,
        tmp_path,
        _submission_interview("""
        question: Enablement chain
        fields:
          - Country: entry
            required: False
          - State: state
            default: MO
            js disable if: val('later')
          - Dependent: dependent
            required: False
            js show if: val('state') === 'MO'
          - Missing: missing
            required: False
            js enable if: '!val("state") && !val("absent")'
          - Later: later
            datatype: yesno
            default: True
        ---
        mandatory: True
        question: Accepted
        """),
        f"""
        started = execution.run(Start())
        assert started.ok, started
        assert started.result['fields'][1]['visible'] is True
        assert started.result['fields'][1]['enabled'] is False
        assert started.result['fields'][2]['visible'] is False
        accepted = execution.run(select_operation(
            {mode!r}, answer=Answer((), defaults=True),
            seek=Seek('entry', auto=True, activate=True),
            replay=ReplayAutomatic('entry', submissions=((),), activate=True,
                expectations=(expectation(started.result),)),
        ))
        assert accepted.ok, accepted
        assert 'replay_divergence' not in accepted.result, accepted
        assert value("('state' in globals(), 'dependent' in globals(), missing, later)") == "(False, False, '', True)"
        """,
    )


def test_default_removed_when_disabled_but_saved_and_explicit_values_survive_worker(
    family_python,  # noqa: F811
    tmp_path,
):
    _execute(
        family_python,
        tmp_path,
        _submission_interview("""
        question: Reverse defaults
        fields:
          - Entry: entry
            required: False
          - Prefilled: prefilled
            default: seed
            js disable if: val('later')
          - Later: later
            datatype: yesno
            default: True
            js show if: val('reveal')
          - Reveal: reveal
            datatype: yesno
            default: True
            show if: show
          - Show: show
            datatype: yesno
            default: True
        ---
        mandatory: True
        question: Accepted
        """),
        """
        import subprocess
        started = execution.run(Start())
        assert started.ok and started.result['fields'][1]['enabled'] is False, started
        accepted = execution.run(Answer((), defaults=True))
        assert accepted.ok, accepted
        assert value("'prefilled' in globals()") == 'False'
        execution.run(Start())
        accepted = execution.run(Answer((('prefilled', 'explicit'),)))
        assert accepted.ok, accepted
        assert value('prefilled') == "'explicit'"
        execution.run(Seek('entry', fresh=False, activate=True))
        assert execution.run(Answer((), defaults=True)).ok
        assert value('prefilled') == "'explicit'"
        journey = root / 'journey.json'
        journey.write_text(json.dumps([
            {'id': 'start', 'operation': 'start'},
            {'id': 'answer', 'operation': 'answer', 'defaults': True},
            {'id': 'eval', 'operation': 'eval', 'expression': "'prefilled' in globals()"},
        ]))
        transported = subprocess.run([sys.executable, '-m', 'docassemble_simulator', '--root', str(root), '--interview', 'docassemble.acceptance:data/questions/main.yml', '--offline', 'run', '--journey', str(journey)], capture_output=True, text=True)
        assert transported.returncode == 0, (transported.stdout, transported.stderr)
        responses = [json.loads(line) for line in transported.stdout.splitlines()]
        assert all(response['ok'] for response in responses), responses
        assert responses[0]['result']['fields'][1]['enabled'] is False
        assert responses[-1]['result']['value'] == 'False'
        """,
    )


@pytest.mark.parametrize("condition", ["enable", "disable"])
def test_no_trigger_and_unsupported_enablement_keep_distinct_states(
    family_python,  # noqa: F811
    tmp_path,
    condition,
):
    _execute(
        family_python,
        tmp_path,
        _submission_interview(f"""
        question: Initial state
        fields:
          - Entry: entry
          - Constant: constant
            js {condition} if: 'false'
          - Missing: missing
            js {condition} if: val('absent')
        ---
        mandatory: True
        question: Accepted
        """),
        """
        started = execution.run(Start())
        assert started.ok, started
        for field in started.result['fields'][1:]:
            assert field['enabled'] is True and field['visible'] is True, field
            assert field['js_condition_state'] == 'no-trigger', field
        rejected = execution.run(Answer((('entry', 'ok'),)))
        assert not rejected.ok and rejected.error.details['missing_required'] == ['constant', 'missing'], rejected
        """,
    )


@pytest.mark.parametrize("condition", ["enable", "disable"])
def test_reverse_enablement_restores_required_and_constraint_gates(
    family_python,  # noqa: F811
    tmp_path,
    condition,
):
    expression = "val('entry')" if condition == "enable" else "!val('entry')"
    _execute(
        family_python,
        tmp_path,
        _submission_interview(f"""
        question: Restore gates
        fields:
          - Entry: entry
            datatype: yesno
          - State: state
            js {condition} if: "{expression}"
          - Length: length
            datatype: integer
            min: 2
            required: False
            js {condition} if: "{expression}"
        ---
        mandatory: True
        question: Accepted
        """),
        """
        started = execution.run(Start())
        assert started.ok and started.result['fields'][1]['enabled'] is False, started
        assert execution.run(Answer((('entry', 'false'), ('length', '1')))).ok
        sought = execution.run(Seek('entry', fresh=False, activate=True))
        assert sought.ok, sought
        rejected = execution.run(Answer((('entry', 'true'),)))
        assert not rejected.ok and rejected.error.details['missing_required'] == ['state'], rejected
        assert value('entry') == 'False'
        rejected = execution.run(Answer((('entry', 'true'), ('state', 'MO'))))
        assert not rejected.ok and 'min constraint' in str(rejected.error), rejected
        assert value('entry') == 'False'
        accepted = execution.run(Answer((('entry', 'true'), ('state', 'MO'), ('length', '2'))))
        assert accepted.ok, accepted
        """,
    )


@pytest.mark.parametrize(
    "flags", [{}, {"partial": True}, {"strict": True}, {"validate": False}]
)
def test_unsupported_enablement_is_capability_rejection_under_all_answer_policies(
    family_python,  # noqa: F811
    tmp_path,
    flags,
):
    _execute(
        family_python,
        tmp_path,
        _submission_interview("""
        question: Unsupported condition
        fields:
          - Entry: entry
          - State: state
            js enable if: custom(val('entry'))
        """),
        f"""
        started = execution.run(Start())
        assert started.ok, started
        field = started.result['fields'][1]
        assert field['visible'] is True and field['enabled'] is None, field
        assert field['js_condition_diagnostic']['code'] == 'call', field
        rejected = execution.run(Answer((('entry', 'US'),), **{flags!r}))
        assert not rejected.ok and rejected.error.kind == 'js-condition-unsupported', rejected
        assert execution.run(Status()).result == started.result
        assert value("'entry' in globals()") == 'False'
        """,
    )


@pytest.mark.parametrize("condition", ["enable", "disable"])
@pytest.mark.parametrize("expression", ["false", "val('absent')"])
def test_app_js_no_trigger_enabled_control_is_unavailable_to_dependent_lookup(
    family_python,  # noqa: F811
    tmp_path,
    condition,
    expression,
):
    _execute(
        family_python,
        tmp_path,
        _submission_interview(f"""
        question: Uninitialized enablement wrapper
        fields:
          - Entry: entry
          - Controller: controller
            default: seed
            js {condition} if: "{expression}"
          - Dependent: dependent
            js show if: val('controller') === 'seed'
          - Null controller: null_controller
            js show if: val('controller') === null
        ---
        mandatory: True
        question: Accepted
        """),
        """
        started = execution.run(Start())
        assert started.ok, started
        fields = started.result['fields']
        assert fields[1]['enabled'] is True and fields[1]['required'] is True, fields
        assert fields[1]['js_condition_state'] == 'no-trigger', fields
        assert fields[2]['visible'] is False and fields[3]['visible'] is True, fields
        execution = InterviewExecution(root, 'docassemble.acceptance:data/questions/main.yml')
        rejected = execution.run(Answer((('entry', 'ok'),)))
        assert not rejected.ok and rejected.error.details['missing_required'] == ['null_controller'], rejected
        assert execution.run(Status()).result == started.result
        assert value("'controller' in globals()") == 'False'
        accepted = execution.run(Answer((('entry', 'ok'), ('null_controller', 'yes'))))
        assert accepted.ok, accepted
        assert value('controller') == "'seed'"
        assert value("'dependent' in globals()") == 'False'
        """,
    )
