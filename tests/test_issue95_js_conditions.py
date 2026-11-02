"""Indexed corpus and grouped browser bindings through execution (#95)."""

import pytest
from test_real_runtime import family_python, real_python, real_python_19  # noqa: F401
from test_submission_acceptance import _execute, _submission_interview

pytestmark = pytest.mark.real_runtime


def test_app_js_indexed_signature_absent_replacement_and_frozen_generated_fields(
    family_python,  # noqa: F811
    tmp_path,
):
    _execute(
        family_python,
        tmp_path,
        """
    ---
    objects:
      - attorneys: DAList.using(object_type=DAObject)
    ---
    initial: True
    code: |
      i = 0
      attorneys.appendObject()
    ---
    mandatory: True
    code: |
      attorneys[i].signature_yn
    ---
    question: Indexed signature
    fields:
      - Signature requested: attorneys[i].signature_yn
        datatype: yesno
      - Replacement: attorneys[i].signature_replace
        datatype: yesno
        show if:
          code: "False"
      - code: |
          [{'field': 'attorneys[i].signature', 'label': 'Signature', 'js show if': 'val("attorneys[i].signature_yn") && !val("attorneys[i].signature_replace")'}]
    ---
    mandatory: True
    question: Accepted
    """,
        """
    started = execution.run(Start())
    assert started.ok, started
    captured = started.result['fields'][2]['js_condition']
    assert captured['bindings']['attorneys[i].signature_yn']['target'] == 'attorneys[0].signature_yn', captured
    execution = InterviewExecution(root, 'docassemble.acceptance:data/questions/main.yml')
    rejected = execution.run(Answer((('attorneys[i].signature_yn', 'true'),)))
    assert not rejected.ok and rejected.error.details['missing_required'] == ['attorneys[i].signature'], rejected
    assert execution.run(Status()).result == started.result
    accepted = execution.run(Answer((('attorneys[i].signature_yn', 'true'), ('attorneys[i].signature', 'signed'))))
    assert accepted.ok, accepted
    """,
    )


@pytest.mark.parametrize("mode", ["answer", "replay"])
def test_app_js_checkbox_options_nullable_radio_notes_and_rollback(
    family_python,  # noqa: F811
    tmp_path,
    mode,
):
    _execute(
        family_python,
        tmp_path,
        _submission_interview("""
    question: Demo controls
    fields:
      - Fruits: entry
        datatype: checkboxes
        choices:
          - Apple
          - Plum
      - Mushrooms: mushrooms
        datatype: yesnomaybe
      - Both fruits: both
        js show if: val("entry['Apple']") && val("entry['Plum']")
      - Maybe: maybe
        required: False
        js show if: val("mushrooms") == "None"
      - note: Not Missouri
        js show if: val("mushrooms") !== "MO"
    ---
    mandatory: True
    question: Accepted
    """),
        f"""
    started = execution.run(Start())
    assert started.ok, started
    assert started.result['fields'][4]['visible'] is True, started.result
    assignments = (('entry', '{{"Apple": true, "Plum": true}}'), ('mushrooms', 'None'))
    rejected = execution.run(Answer(assignments))
    assert not rejected.ok and rejected.error.details['missing_required'] == ['both'], rejected
    assert execution.run(Status()).result == started.result
    assignments += (('both', 'yes'),)
    if {mode!r} == 'answer':
        accepted = execution.run(Answer(assignments))
    else:
        accepted = execution.run(ReplayAutomatic('entry', submissions=(assignments,), activate=True, expectations=(expectation(started.result),)))
        assert 'replay_divergence' not in accepted.result, accepted
    assert accepted.ok, accepted
    assert value('maybe') == "''"
    """,
    )


@pytest.mark.parametrize("interpolation", ["boolean", "string"])
def test_parse_mako_json_frozen_generic_binding_worker_transport(
    family_python,  # noqa: F811
    tmp_path,
    interpolation,
):
    expression = (
        'val("x.type") !== ${json.dumps(marker)}'
        if interpolation == "string"
        else 'val("x.type") !== "deed" && ${json.dumps(flag)}'
    )
    _execute(
        family_python,
        tmp_path,
        """
    ---
    objects:
      - creditor: DAObject
    ---
    initial: True
    code: |
      import json
      marker = 'deed'
      flag = True
    ---
    mandatory: True
    code: |
      creditor.type
    ---
    generic object: DAObject
    question: Generic creditor
    fields:
      - Type: x.type
      - Marker: marker
        default: deed
      - Flag: flag
        datatype: yesno
      - Details: x.details
        js show if: """
        + expression
        + """
      - note: Not Missouri
        js show if: val("x.type") !== "MO"
    ---
    mandatory: True
    question: Accepted
    """,
        """
    import subprocess
    started = execution.run(Start())
    assert started.ok, started
    captured = started.result['fields'][3]['js_condition']
    assert captured['bindings']['x.type']['target'] == 'creditor.type', captured
    assert '${' not in captured['expression']
    execution = InterviewExecution(root, 'docassemble.acceptance:data/questions/main.yml')
    rejected = execution.run(Answer((('creditor.type', 'other'), ('marker', 'other'), ('flag', 'false'))))
    assert not rejected.ok and rejected.error.details['missing_required'] == ['x.details'], rejected
    assert execution.run(Status()).result == started.result
    journey = root / 'journey.json'
    journey.write_text(json.dumps([{'id': 'start', 'operation': 'start'}, {'id': 'status', 'operation': 'status'}]))
    transported = subprocess.run([sys.executable, '-m', 'docassemble_simulator', '--root', str(root), '--interview', 'docassemble.acceptance:data/questions/main.yml', '--offline', 'run', '--journey', str(journey)], capture_output=True, text=True)
    assert transported.returncode == 0, (transported.stdout, transported.stderr)
    response = json.loads(transported.stdout.splitlines()[-1])
    assert response['result']['fields'][3]['js_condition'] == captured, response
    accepted = execution.run(Answer((('creditor.type', 'other'), ('marker', 'other'), ('flag', 'false'), ('creditor.details', 'ok'))))
    assert accepted.ok, accepted
    """,
    )


@pytest.mark.parametrize("corpus", ["state", "creditor"])
def test_automatedpleading_strict_negative_paths_and_automatic_seek(
    family_python,  # noqa: F811
    tmp_path,
    corpus,
):
    if corpus == "state":
        objects = """
      - M: DAObject
      - M.dept_of_revenue: DAObject
      - M.dept_of_revenue.dlr: DAObject
      - M.dept_of_revenue.dlr.address: DAObject"""
        path = "M.dept_of_revenue.dlr.address.state"
        excluded = "MO"
        initial = ""
    else:
        objects = """
      - M: DAObject
      - M.foreclosure: DAObject
      - M.foreclosure.deed: DAObject
      - M.foreclosure.deed.creditors: DAList.using(object_type=DAObject)"""
        path = "M.foreclosure.deed.creditors[i].type"
        excluded = "deed"
        initial = "  i = 0\n  M.foreclosure.deed.creditors.appendObject()\n"
    interview = "\n---\nobjects:" + objects + "\n"
    if initial:
        interview += "---\ninitial: True\ncode: |\n" + initial
    interview += f'''---
mandatory: True
code: |
  {path}
---
question: Corpus path
fields:
  - Controller: {path}
    default: other
  - Details: details
    default: shown
    js show if: val("{path}") !== "{excluded}"
  - Optional: optional
    required: False
    js show if: val("{path}") !== "{excluded}"
  - note: Nonmatching controller
    js show if: val("{path}") !== "{excluded}"
---
mandatory: True
question: Accepted
'''
    _execute(
        family_python,
        tmp_path,
        interview,
        f"""
    started = execution.run(Start())
    assert started.ok, started
    assert all(f['visible'] is True for f in started.result['fields']), started
    accepted = execution.run(Seek({path!r}, auto=True, activate=True))
    assert accepted.ok, accepted
    assert accepted.result['auto_trace'][0]['ok'] is True, accepted
    assert value('details') == "'shown'"
    assert value('optional') == "''"
    """,
    )


@pytest.mark.parametrize("binding", ["generic", "indexed"])
@pytest.mark.parametrize("address", ["authored", "resolved"])
def test_submitted_numeric_spelling_survives_rendered_alias(
    family_python,  # noqa: F811
    tmp_path,
    binding,
    address,
):
    if binding == "generic":
        setup = """
        ---
        objects:
          - person: DAObject
        ---
        mandatory: True
        code: |
          person.count
        ---
        generic object: DAObject
        """
        authored, resolved = "x.count", "person.count"
    else:
        setup = """
        ---
        objects:
          - people: DAList.using(object_type=DAObject)
        ---
        initial: True
        code: |
          i = 0
          people.appendObject()
        ---
        mandatory: True
        code: |
          people[i].count
        ---
        """
        authored, resolved = "people[i].count", "people[0].count"
    _execute(
        family_python,
        tmp_path,
        setup
        + f"""question: Numeric alias
        fields:
          - Count: {authored}
            datatype: number
          - Details: details
            js show if: val("{authored}") === "1.0"
        ---
        mandatory: True
        question: Accepted
        """,
        f"""
        started = execution.run(Start())
        assert started.ok, started
        submitted = {authored if address == "authored" else resolved!r}
        rejected = execution.run(Answer(((submitted, '1.0'),)))
        assert not rejected.ok and rejected.error.details['missing_required'] == ['details'], rejected
        assert execution.run(Status()).result == started.result
        accepted = execution.run(Answer(((submitted, '1.0'), ('details', 'ok'))))
        assert accepted.ok, accepted
        """,
    )


@pytest.mark.parametrize("datatype", ["checkboxes", "multiselect"])
def test_app_js_grouped_val_calls_return_fresh_arrays(
    family_python,  # noqa: F811
    tmp_path,
    datatype,
):
    _execute(
        family_python,
        tmp_path,
        _submission_interview(f"""
        question: Group identity
        fields:
          - Group: entry
            datatype: {datatype}
            required: False
            choices:
              - Apple
              - Plum
          - Same: same
            js show if: val("entry") === val("entry")
          - Different: different
            js show if: val("entry") !== val("entry")
        ---
        mandatory: True
        question: Accepted
        """),
        """
        started = execution.run(Start())
        assert started.ok, started
        assert started.result['fields'][1]['visible'] is False, started
        assert started.result['fields'][2]['visible'] is True, started
        rejected = execution.run(Answer((('entry', '{"Apple": true}'),)))
        assert not rejected.ok and rejected.error.details['missing_required'] == ['different'], rejected
        assert execution.run(Status()).result == started.result
        accepted = execution.run(Answer((('entry', '{"Apple": true}'), ('different', 'ok'))))
        assert accepted.ok, accepted
        assert value("'same' in globals()") == 'False'
        """,
    )
