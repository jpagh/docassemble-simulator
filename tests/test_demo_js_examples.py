"""Behavioral checks against the unmodified external demo conditional examples.

Unlike synthetic issue fixtures, these execute the complete checked-in YAML.
They do not claim support for authored scripts, check-in requests, or a DOM.
"""

import os
from pathlib import Path

import pytest
from test_real_runtime import family_python, real_python, real_python_19  # noqa: F401
from test_submission_acceptance import _execute

pytestmark = [pytest.mark.real_runtime, pytest.mark.corpus]


def _example(name):
    fixtures = Path(
        os.environ.get(
            "DASIMULATOR_DEMO_FIXTURES",
            Path(__file__).resolve().parents[2] / "docassemble-yaml/lsp/tests/fixtures",
        )
    )
    path = fixtures / "examples" / name
    if not path.is_file():
        pytest.skip(f"external demo fixture unavailable: {path}")
    return path.read_text()


def test_demo_jsshowif_complete_journeys(family_python, tmp_path):  # noqa: F811
    _execute(
        family_python,
        tmp_path,
        _example("jsshowif.yml"),
        """
        from itertools import product

        def fields(screen):
            return {f['variable']: f for f in screen['fields']}

        def reject(assignments, missing):
            before = execution.run(Status()).result
            rejected = execution.run(Answer(tuple(assignments)))
            assert not rejected.ok and rejected.error.kind == 'validation', rejected
            assert rejected.error.details['missing_required'] == missing, rejected
            assert execution.run(Status()).result == before

        # Cover both outcomes of all eight conditions, including every
        # Boolean combination of the chained radio and checkbox controllers.
        for fruit in ('apple', 'mango', 'pear'):
            started = execution.run(Start())
            assert started.ok, started
            initial = fields(started.result)
            assert initial['mushroom']['visible'] is False
            assert initial['spice']['visible'] is True
            execution = InterviewExecution(root, 'docassemble.acceptance:data/questions/main.yml')
            missing = (['mushroom', 'spice'] if fruit == 'apple' else
                       [] if fruit == 'mango' else ['spice'])
            assignments = [('fruit', fruit), ('vegetable', 'carrot')]
            if missing:
                reject(assignments, missing)
            assignments += [(name, 'test') for name in missing]
            accepted = execution.run(Answer(tuple(assignments)))
            assert accepted.ok, accepted
            assert value("'mushroom' in globals()") == str(fruit == 'apple')
            assert value("'spice' in globals()") == str(fruit != 'mango')
            cuisine_fields = fields(accepted.result)
            assert cuisine_fields['dish']['visible'] is False
            cuisine = 'Chinese food' if fruit == 'apple' else 'French food'
            assignments = [('cuisine', cuisine)]
            if cuisine == 'Chinese food':
                reject(assignments, ['dish'])
                assignments.append(('dish', 'noodles'))
            accepted = execution.run(Answer(tuple(assignments)))
            assert accepted.ok, accepted
            assert value("'dish' in globals()") == str(cuisine == 'Chinese food')
            media = fields(accepted.result)
            assert all(media[name]['visible'] is False for name in
                       ('tv_show', 'radio_station', 'favorite_modulation', 'favorite_antenna'))
            tv = fruit == 'apple'
            radio = fruit != 'pear'
            modulation = 'FM' if fruit == 'apple' else 'AM'
            assignments = [('watches_tv', str(tv).lower()),
                           ('listens_to_radio', str(radio).lower())]
            missing = (['tv_show'] if tv else []) + (
                ['radio_station', 'favorite_modulation'] if radio else [])
            if missing:
                reject(assignments, missing)
            if tv:
                assignments.append(('tv_show', 'news'))
            if radio:
                assignments += [('radio_station', 'station'), ('favorite_modulation', modulation)]
                if modulation == 'FM':
                    reject(assignments, ['favorite_antenna'])
                    assignments.append(('favorite_antenna', 'dipole'))
            accepted = execution.run(Answer(tuple(assignments)))
            assert accepted.ok, accepted
            assert value("'favorite_antenna' in globals()") == str(radio and modulation == 'FM')
            assert fields(accepted.result)['apple_plum_dish']['visible'] is False
            fruits = ['Apple', 'Plum'] if fruit == 'apple' else ['Peach']
            assignments = [('liked_fruits', json.dumps({name: name in fruits
                           for name in ('Apple', 'Peach', 'Pear', 'Plum')}))]
            if fruit == 'apple':
                reject(assignments, ['apple_plum_dish'])
                assignments.append(('apple_plum_dish', 'pie'))
            completed = execution.run(Answer(tuple(assignments)))
            assert completed.ok, completed
            assert 'Thank you' in completed.result['question_text'], completed
            assert value("'apple_plum_dish' in globals()") == str(fruit == 'apple')

        # Isolate the media screen to exhaust the controller truth table,
        # including a submitted FM value on a radio that is itself hidden.
        for tv, radio, modulation in product((False, True), (False, True), ('AM', 'FM')):
            sought = execution.run(Seek('watches_tv', fresh=True, activate=True))
            assert sought.ok, sought
            assignments = [('watches_tv', str(tv).lower()),
                           ('listens_to_radio', str(radio).lower()),
                           ('favorite_modulation', modulation)]
            missing = (['tv_show'] if tv else []) + (['radio_station'] if radio else [])
            if radio and modulation == 'FM':
                missing.append('favorite_antenna')
            if missing:
                reject(assignments, missing)
            accepted = execution.run(Answer(tuple(assignments + [(n, 'test') for n in missing])))
            assert accepted.ok, accepted
            assert value("'favorite_antenna' in globals()") == str(radio and modulation == 'FM')

        for apple, plum in product((False, True), repeat=2):
            sought = execution.run(Seek('liked_fruits', fresh=True, activate=True))
            assert sought.ok, sought
            choices = (['Apple'] if apple else []) + (['Plum'] if plum else [])
            # The neither branch explicitly submits the group's None choice.
            assignments = [('liked_fruits', json.dumps({name: name in choices
                           for name in ('Apple', 'Peach', 'Pear', 'Plum')}))]
            if apple and plum:
                reject(assignments, ['apple_plum_dish'])
                assignments.append(('apple_plum_dish', 'pie'))
            accepted = execution.run(Answer(tuple(assignments)))
            assert accepted.ok, accepted
            assert value("'apple_plum_dish' in globals()") == str(apple and plum)
        """,
    )


def test_demo_jsshowifmaybe_complete_journeys(family_python, tmp_path):  # noqa: F811
    _execute(
        family_python,
        tmp_path,
        _example("jsshowifmaybe.yml"),
        """
        for answer, dependent in [('true', 'mushroom'), ('false', None),
                                  ('null', 'reason_why_unsure')]:
            started = execution.run(Start())
            assert started.ok, started
            fields = {f['variable']: f for f in started.result['fields']}
            assert fields['mushroom']['visible'] is False
            assert fields['reason_why_unsure']['visible'] is False
            execution = InterviewExecution(root, 'docassemble.acceptance:data/questions/main.yml')
            assignments = [('fruit', 'apple'), ('vegetable', 'carrot'),
                           ('likes_mushrooms', answer)]
            if dependent:
                rejected = execution.run(Answer(tuple(assignments)))
                assert not rejected.ok and rejected.error.kind == 'validation', rejected
                assert rejected.error.details['missing_required'] == [dependent], rejected
                assert execution.run(Status()).result == started.result
                assert value("'likes_mushrooms' in globals()") == 'False'
                assignments.append((dependent, 'test'))
            completed = execution.run(Answer(tuple(assignments)))
            assert completed.ok, completed
            assert 'Thank you' in completed.result['question_text'], completed
            assert value('likes_mushrooms') == {'true': 'True', 'false': 'False', 'null': 'None'}[answer]
            for name in ('mushroom', 'reason_why_unsure'):
                assert value(f'{name!r} in globals()') == str(name == dependent)
        """,
    )


def test_demo_country_state_enablement_and_choices(family_python, tmp_path):  # noqa: F811
    _execute(
        family_python,
        tmp_path,
        _example("country-state.yml"),
        """
        started = execution.run(Start())
        assert started.ok, started
        fields = {f['variable']: f for f in started.result['fields']}
        assert fields['state']['visible'] is True
        assert fields['state']['enabled'] is False
        assert fields['state']['js_condition_state'] == 'evaluated'
        assert 'NY' in [c['value'] for c in fields['state']['choices']]
        execution = InterviewExecution(root, 'docassemble.acceptance:data/questions/main.yml')
        missing_country = execution.run(Answer((), defaults=True))
        assert not missing_country.ok, missing_country
        assert missing_country.error.details.get('missing_required') == ['country'], missing_country
        missing_state = execution.run(Answer((('country', 'US'),)))
        assert not missing_state.ok, missing_state
        assert missing_state.error.details['missing_required'] == ['state'], missing_state
        assert execution.run(Status()).result == started.result
        assert value("'country' in globals()") == 'False'
        completed = execution.run(Answer((('country', 'US'), ('state', 'NY'))))
        assert completed.ok, completed
        assert 'New York' in completed.result['question_text'], completed
        assert 'United States' in completed.result['question_text'], completed
        """,
    )
