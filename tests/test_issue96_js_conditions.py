"""Settled browser-shaped chained visibility through execution (#96)."""

import textwrap

import pytest
from test_real_runtime import family_python, real_python, real_python_19  # noqa: F401
from test_submission_acceptance import _execute, _submission_interview

pytestmark = pytest.mark.real_runtime


@pytest.mark.parametrize("mode", ["answer", "seek", "replay"])
def test_radio_antenna_chain_defaults_required_blanks_constraints_and_reload(
    family_python,  # noqa: F811
    tmp_path,
    mode,
):
    _execute(
        family_python,
        tmp_path,
        _submission_interview("""
        question: Radio
        fields:
          - Listen: entry
            datatype: yesnoradio
            default: True
          - Modulation: modulation
            default: AM
            js show if: val("entry")
          - Antenna: antenna
            js show if: val("modulation") === "AM"
          - Length: length
            datatype: integer
            default: 3
            min: 2
            js show if: val("modulation") === "AM"
          - Optional: optional
            required: False
            js show if: val("modulation") === "AM"
        ---
        mandatory: True
        question: Accepted
        """),
        f"""
        started = execution.run(Start())
        assert started.ok, started
        assert all(f['visible'] is True for f in started.result['fields']), started
        execution = InterviewExecution(root, 'docassemble.acceptance:data/questions/main.yml')
        rejected = execution.run(Answer((), defaults=True))
        assert not rejected.ok and rejected.error.details.get('missing_required') == ['antenna'], rejected
        assert execution.run(Status()).result == started.result
        assert value("('modulation' in globals(), 'optional' in globals())") == '(False, False)'
        rejected = execution.run(Answer((('antenna', 'wire'), ('length', '1'))))
        assert not rejected.ok and 'min constraint' in str(rejected.error), rejected
        assert execution.run(Status()).result == started.result
        early = execution.run(Answer((('wrong', 'x'),)))
        assert early.error.details['missing_required'] == ['antenna'], early
        assignments = (('antenna', 'wire'),)
        accepted = execution.run(select_operation(
            {mode!r}, answer=Answer(assignments),
            seek=Seek('entry', auto=True, activate=True),
            replay=ReplayAutomatic('entry', submissions=(assignments,), activate=True,
                expectations=(expectation(started.result),)),
        ))
        assert accepted.ok, accepted
        if {mode!r} == 'seek':
            assert accepted.result['auto_stop']['missing_required'] == ['antenna'], accepted
            assert value("'modulation' in globals()") == 'False'
        else:
            assert value('(entry, modulation, antenna, length, optional)') == "(True, 'AM', 'wire', 3, '')"
        """,
    )


@pytest.mark.parametrize("preexisting", [False, True])
def test_hidden_controller_null_reverse_transition_and_explicit_saved_precedence(
    family_python,  # noqa: F811
    tmp_path,
    preexisting,
):
    prefix = (
        textwrap.dedent("""
    ---
    mandatory: True
    code: |
      modulation = 'AM'
      antenna = 'saved'
    """)
        if preexisting
        else ""
    )
    _execute(
        family_python,
        tmp_path,
        prefix
        + _submission_interview("""
        question: Reverse radio
        fields:
          - Listen: entry
            datatype: yesnoradio
            default: True
          - Modulation: modulation
            default: AM
            js show if: val("entry")
          - Antenna: antenna
            js show if: val("modulation") === "AM"
          - Missing controller note: missing
            required: False
            js show if: '!val("modulation") && !val("absent")'
          - No trigger: no_trigger
            js hide if: '!val("absent")'
        ---
        mandatory: True
        question: Accepted
        """),
        f"""
        started = execution.run(Start())
        assert started.ok, started
        assert started.result['fields'][2]['visible'] is True, started
        hidden = execution.run(Answer((('entry', 'false'),)))
        assert hidden.ok, hidden
        assert value('missing') == "''"
        assert value("'no_trigger' in globals()") == 'False'
        assert value("'modulation' in globals()") == {str(preexisting)!r}
        assert value("'antenna' in globals()") == {str(preexisting)!r}
        if {preexisting!r}:
            assert value('(modulation, antenna)') == "('AM', 'saved')"
        started = execution.run(Start())
        explicit = execution.run(Answer((('entry', 'false'), ('modulation', 'AM'), ('antenna', 'explicit'))))
        assert explicit.ok, explicit
        assert value('(modulation, antenna)') == "('AM', 'explicit')"
        # Same saved values become eligible again when the controller is shown.
        shown = execution.run(Seek('entry', fresh=False, activate=True))
        assert shown.ok, shown
        accepted = execution.run(Answer((('entry', 'true'),)))
        assert accepted.ok, accepted
        assert value('(modulation, antenna)') == "('AM', 'explicit')"
        """,
    )


@pytest.mark.parametrize("mode", ["answer", "seek", "replay"])
def test_contradictory_js_visibility_is_bounded_actionable_and_transactional(
    family_python,  # noqa: F811
    tmp_path,
    mode,
):
    _execute(
        family_python,
        tmp_path,
        _submission_interview("""
        question: Contradictory wrapper
        fields:
          - Entry: entry
            datatype: yesno
            default: True
            js show if: '!val("entry")'
        ---
        mandatory: True
        question: Accepted
        """),
        f"""
        started = execution.run(Start())
        assert started.ok, started
        field = started.result['fields'][0]
        assert field['visible'] is None, field
        assert field['js_condition_diagnostic']['code'] == 'nonconvergence', field
        assert 'entry' in field['visibility_note'] and 'review' in field['visibility_note']
        rejected = execution.run(select_operation(
            {mode!r}, answer=Answer((), defaults=True),
            seek=Seek('entry', auto=True, activate=True),
            replay=ReplayAutomatic('entry', submissions=((),), activate=True,
                expectations=(expectation(started.result, ok=False, error_kind='js-condition-unsupported'),)),
        ))
        if {mode!r} == 'answer':
            assert not rejected.ok and rejected.error.kind == 'js-condition-unsupported', rejected
            assert rejected.error.details['diagnostic']['code'] == 'nonconvergence'
        else:
            assert rejected.ok and rejected.result['auto_stop']['submitted'] is False, rejected
            assert rejected.result['auto_trace'][0]['error']['details']['diagnostic']['code'] == 'nonconvergence', rejected
            assert 'replay_divergence' not in rejected.result, rejected
        saved = execution.run(Status()).result
        if {mode!r} != 'answer':
            # Activated walks retain target/attempt metadata, not POST facts.
            metadata = {{'sought_variable', 'auto_submitted', 'auto_stop', 'auto_trace'}}
            assert {{k: v for k, v in saved.items() if k not in metadata}} == started.result, saved
            assert saved['auto_submitted'] == [], saved
            assert saved['auto_trace'][0]['screen']['fields'] == started.result['fields']
        else:
            assert saved == started.result, saved
        assert value("'entry' in globals()") == 'False'
        # A new assignment can resolve a presentation-time contradiction.
        resolved = execution.run(Answer((('entry', 'false'),)))
        assert resolved.ok, resolved
        """,
    )


def test_default_visibility_nonconvergence_reports_fields_and_recovery_without_saving(
    family_python,  # noqa: F811
    tmp_path,
):
    _execute(
        family_python,
        tmp_path,
        _submission_interview("""
        question: Unstable defaults with JS dependent
        fields:
          - Entry: entry
            datatype: yesno
            default: False
            show if:
              code: globals().get('entry', True)
          - Dependent: dependent
            required: False
            js show if: '!val("entry")'
        """),
        """
        started = execution.run(Start())
        assert started.ok, started
        rejected = execution.run(Answer((), defaults=True))
        assert not rejected.ok and rejected.error.kind == 'validation', rejected
        diagnostic = rejected.error.details['diagnostic']
        assert diagnostic['code'] == 'nonconvergence'
        assert diagnostic['fields'] == ['entry', 'dependent']
        assert 'Review defaults' in diagnostic['message']
        assert execution.run(Status()).result == started.result
        assert value("('entry' in globals(), 'dependent' in globals())") == '(False, False)'
        """,
    )


def test_native_default_reversal_removes_only_operation_defaults_and_worker_uses_settled_facts(
    family_python,  # noqa: F811
    tmp_path,
):
    _execute(
        family_python,
        tmp_path,
        _submission_interview("""
        question: Native default reverses chain
        fields:
          - Listen: entry
            datatype: yesnoradio
            default: True
          - Modulation: modulation
            default: AM
            show if:
              code: globals().get('keep', True)
          - Antenna: antenna
            default: wire
            js show if: val("modulation") === "AM"
          - Keep: keep
            datatype: yesno
            default: False
          - Optional: optional
            required: False
            js show if: val("modulation") === "AM"
        ---
        mandatory: True
        question: Accepted
        """),
        """
        import subprocess
        started = execution.run(Start())
        assert started.ok, started
        assert started.result['fields'][2]['visible'] is True, started
        accepted = execution.run(Answer((), defaults=True))
        assert accepted.ok, accepted
        assert value("('modulation' in globals(), 'antenna' in globals(), 'optional' in globals())") == '(False, False, False)'
        # Execute the same transaction through the persistent worker transport.
        journey = root / 'journey.json'
        journey.write_text(json.dumps([
            {'id': 'start', 'operation': 'start'},
            {'id': 'answer', 'operation': 'answer', 'defaults': True},
            {'id': 'eval', 'operation': 'eval', 'expression': "('modulation' in globals(), 'antenna' in globals(), 'optional' in globals())"},
        ]))
        transported = subprocess.run([sys.executable, '-m', 'docassemble_simulator', '--root', str(root), '--interview', 'docassemble.acceptance:data/questions/main.yml', '--offline', 'run', '--journey', str(journey)], capture_output=True, text=True)
        assert transported.returncode == 0, (transported.stdout, transported.stderr)
        responses = [json.loads(line) for line in transported.stdout.splitlines()]
        assert all(response['ok'] for response in responses), responses
        assert responses[-1]['result']['value'] == '(False, False, False)', responses
        """,
    )
