"""Speed tests for screen identity, trace records, and trace comparison."""

import json
from types import SimpleNamespace

import pytest

from docassemble_simulator import cli
from docassemble_simulator._outcomes import ErrorKind, Failure
from docassemble_simulator.trace import (
    ComparePolicy,
    ScreenIdentity,
    ScreenTrace,
    TraceEntry,
    TraceError,
    TraceException,
    TraceMetadata,
    append_trace,
    compare_replay_entries,
    compare_traces,
    error_identity,
    load_exceptions,
    load_trace,
    screen_content,
    screen_identity,
    write_trace,
)


def test_explicit_id_wins_over_target_pair():
    screen = {
        "kind": "question",
        "question_name": "ID client identity",
        "sought": "clients[i].name.first",
        "orig_sought": "clients[0].name.first",
        "fields": [{"variable": "clients[i].name.first"}],
    }

    assert screen_identity(screen) == ScreenIdentity(
        "id:client identity@clients[i].name.first", "explicit-id-list-target"
    )


def test_explicit_id_keeps_generic_instances_distinct():
    first = {
        "kind": "question",
        "question_name": "ID contact name",
        "sought": "x.name",
        "orig_sought": "contacts[0].name",
        "fields": [{"variable": "x.name"}],
    }
    second = {**first, "orig_sought": "contacts[1].name"}

    assert screen_identity(first).key == "id:contact name@contacts[0].name"
    assert screen_identity(second).key == "id:contact name@contacts[1].name"
    assert screen_identity(first).key != screen_identity(second).key


def test_explicit_id_keeps_non_generic_identity():
    screen = _screen(
        question_name="ID intro",
        sought="user_name",
        orig_sought="user_name",
    )

    assert screen_identity(screen) == ScreenIdentity("id:intro", "explicit-id")


def test_explicit_id_anchors_list_target_identity():
    screen = {
        "kind": "question",
        "question_name": "ID client status",
        "sought": "clients[i].complete",
        "orig_sought": "clients[0].complete",
        "fields": [{"variable": "clients[i].complete"}],
    }

    assert screen_identity(screen) == ScreenIdentity(
        "id:client status@clients[i].complete", "explicit-id-list-target"
    )


def test_targeted_variable_uses_the_sought_path():
    screen = {
        "kind": "question",
        "question_name": "Question_2",
        "sought": "marital_status",
        "orig_sought": "marital_status",
        "fields": [{"variable": "marital_status"}],
    }

    assert screen_identity(screen) == ScreenIdentity(
        "var:marital_status", "targeted-variable"
    )


def test_generic_object_anchors_the_placeholder_on_the_resolved_root():
    screen = {
        "kind": "question",
        "question_name": "Question_2",
        "sought": "x.name",
        "orig_sought": "clients[0].name",
        "fields": [{"variable": "x.name"}],
    }

    assert screen_identity(screen) == ScreenIdentity(
        "generic:clients.name", "generic-object"
    )


def test_indexed_list_target_keeps_the_placeholder_index():
    screen = {
        "kind": "question",
        "question_name": "Question_3",
        "sought": "clients[i].complete",
        "orig_sought": "clients[0].complete",
        "fields": [{"variable": "clients[i].complete"}],
    }

    assert screen_identity(screen) == ScreenIdentity(
        "list:clients[i].complete", "list-target"
    )


def test_field_tuple_fallback_sorts_canonical_variables():
    screen = {
        "kind": "question",
        "question_type": "fields",
        "question_name": "Question_7",
        "sought": None,
        "orig_sought": None,
        "fields": [
            {"variable": "zeta"},
            {"variable": "alpha[0]"},
            {"variable": "beta"},
        ],
    }

    assert screen_identity(screen) == ScreenIdentity(
        "fields:alpha[i],beta,zeta", "field-tuple"
    )


def test_category_fallback_never_uses_the_generated_name():
    screen = {
        "kind": "question",
        "question_type": "deadend",
        "question_name": "Question_5",
        "sought": None,
        "orig_sought": None,
        "fields": [],
    }

    assert screen_identity(screen) == ScreenIdentity(
        "screen:question/deadend", "category"
    )


def test_terminal_outcome_without_question_type_uses_the_kind():
    assert screen_identity({"kind": "finished", "message": "done"}) == ScreenIdentity(
        "screen:finished", "category"
    )


def test_generated_instance_name_never_enters_identity():
    screen = {
        "kind": "question",
        "question_type": "fields",
        "question_name": "Question_4",
        "sought": "CMWHWUrsOoma['will_trust']",
        "orig_sought": "CMWHWUrsOoma['will_trust']",
        "fields": [{"variable": "CMWHWUrsOoma['will_trust']"}],
    }

    identity = screen_identity(screen)

    assert identity == ScreenIdentity("screen:question/fields", "category")
    assert "CMWHWUrsOoma" not in identity.key


def test_screen_content_is_order_insensitive_and_excludes_volatile_text():
    screen = {
        "kind": "question",
        "question_name": "ID review",
        "question_text": "Volatile question text",
        "subquestion_text": "Volatile subquestion text",
        "fields": [
            {
                "variable": "review_ok",
                "type": "boolean",
                "visible": True,
                "required": False,
                "label": "OK?",
            },
            {
                "variable": "amount",
                "type": "currency",
                "visible": True,
                "required": True,
                "choices": [
                    {"value": "b", "label": "B"},
                    {"value": "a", "label": "A"},
                ],
            },
        ],
    }

    content = screen_content(screen)

    assert [field["variable"] for field in content] == ["amount", "review_ok"]
    assert content[0] == {
        "variable": "amount",
        "type": "currency",
        "visible": True,
        "required": True,
        "choices": ["a", "b"],
    }
    assert content[1] == {
        "variable": "review_ok",
        "type": "boolean",
        "visible": True,
        "required": False,
    }


def _metadata(**overrides):
    values = {
        "interview": "docassemble.probe:data/questions/main.yml",
        "config_fingerprint": "fingerprint-1",
        "docassemble": "1.10.10",
        "assemblyline": "4.8.0",
        "simulator": "26.9.0",
    }
    values.update(overrides)
    return TraceMetadata(**values)


def _screen(**overrides):
    screen = {
        "kind": "question",
        "question_type": "fields",
        "question_name": "ID intro",
        "question_text": "Intro",
        "subquestion_text": "",
        "sought": None,
        "orig_sought": None,
        "fields": [
            {"variable": "user_name", "type": "text", "visible": True, "required": True}
        ],
    }
    screen.update(overrides)
    return screen


def test_row_actions_remain_debug_only_screen_facts():
    before = _screen(
        row_actions=[{"kind": "add", "target": "children[0].grandchildren"}]
    )
    after = _screen(
        row_actions=[{"kind": "edit", "target": "children[0].grandchildren", "row": 0}]
    )
    assert screen_identity(before) == screen_identity(after)
    assert screen_content(before) == screen_content(after)


def test_replay_verdict_compares_identity_facts_flow_and_error_in_trace_module():
    expected_screen = _screen(question_name="ID expected")
    actual_screen = _screen(question_name="ID actual")
    expected = TraceEntry(
        1,
        "answer",
        None,
        {"user_name": "Alice"},
        True,
        expected_screen,
        ScreenIdentity("id:expected", "explicit-id"),
        screen_content(expected_screen),
    )
    actual = TraceEntry(
        1,
        "answer",
        None,
        {"user_name": "Alice"},
        True,
        actual_screen,
        ScreenIdentity("id:actual", "explicit-id"),
        screen_content(actual_screen),
    )

    verdict = compare_replay_entries(expected, actual)

    assert verdict.identity_match is False
    assert verdict.field_facts_match is True
    assert verdict.flow_match is True
    assert verdict.error_match is True
    assert verdict.matched is False


def test_trace_replay_is_clean_with_failed_step_and_override(
    tmp_path, monkeypatch, capsys
):
    intro = _screen(question_name="ID intro")
    next_screen = _screen(question_name="ID next", variable="next_value", label="Next")
    done = _screen(question_name="ID done", variable="done", label="Done")
    trace = _trace(
        TraceEntry(
            1,
            "start",
            None,
            {},
            True,
            intro,
            ScreenIdentity("id:intro", "explicit-id"),
            screen_content(intro),
        ),
        TraceEntry(
            2,
            "answer",
            None,
            {"user_name": "Alice"},
            True,
            next_screen,
            ScreenIdentity("id:next", "explicit-id"),
            screen_content(next_screen),
        ),
        TraceEntry(
            3,
            "answer",
            None,
            {"user_name": "bad"},
            False,
            next_screen,
            ScreenIdentity("id:next", "explicit-id"),
            screen_content(next_screen),
            {"kind": "input", "message": "rejected", "details": {}},
        ),
        TraceEntry(
            4,
            "seek",
            None,
            {"done": "yes"},
            True,
            done,
            ScreenIdentity("id:done", "explicit-id"),
            screen_content(done),
        ),
    )
    golden = tmp_path / "golden.trace.jsonl"
    write_trace(golden, trace)
    original = golden.read_bytes()
    expected_metadata = trace.metadata
    operations = []

    class FakeExecution:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, operation):
            from docassemble_simulator.execution import Answer, Status

            operations.append(operation)
            if operation.__class__.__name__ == "Start":
                return SimpleNamespace(ok=True, result=intro, error=None)
            if isinstance(operation, Answer):
                values = dict(operation.assignments)
                if values.get("user_name") == "bad":
                    return SimpleNamespace(
                        ok=False,
                        result=None,
                        error=Failure(ErrorKind.INPUT, "rejected", {}),
                    )
                if "done" in values:
                    return SimpleNamespace(ok=True, result=done, error=None)
                return SimpleNamespace(ok=True, result=next_screen, error=None)
            if isinstance(operation, Status):
                return SimpleNamespace(ok=True, result=next_screen, error=None)
            raise AssertionError(operation)

    monkeypatch.setattr(
        "docassemble_simulator.execution.InterviewExecution", FakeExecution
    )
    monkeypatch.setattr(
        cli,
        "_trace_metadata",
        lambda root, interview, resolved_configuration=None: expected_metadata,
    )
    args = SimpleNamespace(
        golden=str(golden),
        overrides=["2.user_name=Bob"],
        interview=None,
        json=True,
        _resolved_configuration=None,
    )

    assert cli.cmd_trace_replay(args, tmp_path) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["clean"] is True
    assert payload["result"]["counts"]["matched"] == 4
    assert dict(operations[1].assignments)["user_name"] == "Bob"
    assert golden.read_bytes() == original


def test_trace_replay_applies_automatic_submission_before_next_seek(
    tmp_path, monkeypatch, capsys
):
    intro = _screen(question_name="ID intro")
    next_screen = _screen(question_name="ID next")
    trace = _trace(
        TraceEntry(
            1,
            "start",
            None,
            {},
            True,
            intro,
            ScreenIdentity("id:intro", "explicit-id"),
            screen_content(intro),
            intent="start",
        ),
        TraceEntry(
            2,
            "seek",
            None,
            {},
            True,
            intro,
            ScreenIdentity("id:intro", "explicit-id"),
            screen_content(intro),
            intent="auto",
            target="next_value",
        ),
        TraceEntry(
            3,
            "seek",
            None,
            {},
            True,
            next_screen,
            ScreenIdentity("id:next", "explicit-id"),
            screen_content(next_screen),
            intent="seek",
            target="next_value",
        ),
    )
    golden = tmp_path / "golden.trace.jsonl"
    write_trace(golden, trace)
    operations = []

    class FakeExecution:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, operation):
            from docassemble_simulator.execution import Answer, Seek, Start, Status

            operations.append(operation)
            if isinstance(operation, Start):
                return SimpleNamespace(ok=True, result=intro, error=None)
            if isinstance(operation, Status):
                return SimpleNamespace(ok=True, result=intro, error=None)
            if isinstance(operation, Answer):
                assert operation.defaults is True
                return SimpleNamespace(ok=True, result=next_screen, error=None)
            if isinstance(operation, Seek):
                return SimpleNamespace(ok=True, result=next_screen, error=None)
            raise AssertionError(operation)

    monkeypatch.setattr(
        "docassemble_simulator.execution.InterviewExecution", FakeExecution
    )
    monkeypatch.setattr(
        cli,
        "_trace_metadata",
        lambda root, interview, resolved_configuration=None: trace.metadata,
    )
    args = SimpleNamespace(
        golden=str(golden),
        overrides=[],
        interview=None,
        json=True,
        _resolved_configuration=None,
    )

    assert cli.cmd_trace_replay(args, tmp_path) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["clean"] is True
    assert [type(operation).__name__ for operation in operations] == [
        "Start",
        "Status",
        "Answer",
        "Seek",
    ]


def test_trace_replay_observes_a_recorded_status_step(tmp_path, monkeypatch, capsys):
    """A recorded status response replays as a read-only screen observation (#51)."""
    intro = _screen(question_name="ID intro")
    next_screen = _screen(question_name="ID next", variable="next_value")
    trace = _trace(
        TraceEntry(
            1,
            "start",
            None,
            {},
            True,
            intro,
            ScreenIdentity("id:intro", "explicit-id"),
            screen_content(intro),
            intent="start",
        ),
        TraceEntry(
            2,
            "status",
            None,
            {},
            True,
            intro,
            ScreenIdentity("id:intro", "explicit-id"),
            screen_content(intro),
            intent="status",
        ),
        TraceEntry(
            3,
            "answer",
            None,
            {"user_name": "Alice"},
            True,
            next_screen,
            ScreenIdentity("id:next", "explicit-id"),
            screen_content(next_screen),
            intent="answer",
        ),
    )
    golden = tmp_path / "golden.trace.jsonl"
    write_trace(golden, trace)
    operations = []

    class FakeExecution:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, operation):
            from docassemble_simulator.execution import Answer, Start, Status

            operations.append(type(operation).__name__)
            if isinstance(operation, Start):
                return SimpleNamespace(ok=True, result=intro, error=None)
            if isinstance(operation, Status):
                return SimpleNamespace(ok=True, result=intro, error=None)
            if isinstance(operation, Answer):
                return SimpleNamespace(ok=True, result=next_screen, error=None)
            raise AssertionError(operation)

    monkeypatch.setattr(
        "docassemble_simulator.execution.InterviewExecution", FakeExecution
    )
    monkeypatch.setattr(
        cli,
        "_trace_metadata",
        lambda root, interview, resolved_configuration=None: trace.metadata,
    )
    args = SimpleNamespace(
        golden=str(golden),
        overrides=[],
        interview=None,
        json=True,
        _resolved_configuration=None,
    )

    assert cli.cmd_trace_replay(args, tmp_path) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["clean"] is True
    assert payload["result"]["counts"]["matched"] == 3
    assert operations == ["Start", "Status", "Answer"]


@pytest.mark.parametrize("operation", ["eval", "vars", "seed", "exec"])
def test_trace_replay_rejects_a_compare_only_record(
    tmp_path, monkeypatch, capsys, operation
):
    """eval/vars/seed/exec carry no runnable input, so replay rejects them (#51)."""
    intro = _screen(question_name="ID intro")
    trace = _trace(
        TraceEntry(
            1,
            "start",
            None,
            {},
            True,
            intro,
            ScreenIdentity("id:intro", "explicit-id"),
            screen_content(intro),
            intent="start",
        ),
        TraceEntry(
            2,
            operation,
            None,
            {},
            True,
            intro,
            ScreenIdentity("id:intro", "explicit-id"),
            screen_content(intro),
            intent=operation,
        ),
    )
    golden = tmp_path / "golden.trace.jsonl"
    write_trace(golden, trace)
    operations = []

    class FakeExecution:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, run_operation):
            from docassemble_simulator.execution import Start

            operations.append(type(run_operation).__name__)
            if isinstance(run_operation, Start):
                return SimpleNamespace(ok=True, result=intro, error=None)
            raise AssertionError(f"replay must not run {run_operation!r}")

    monkeypatch.setattr(
        "docassemble_simulator.execution.InterviewExecution", FakeExecution
    )
    monkeypatch.setattr(
        cli,
        "_trace_metadata",
        lambda root, interview, resolved_configuration=None: trace.metadata,
    )
    args = SimpleNamespace(
        golden=str(golden),
        overrides=[],
        interview=None,
        json=True,
        _resolved_configuration=None,
    )

    assert cli.cmd_trace_replay(args, tmp_path) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["kind"] == "input"
    assert operation in payload["error"]["message"]
    assert "compare-only" in payload["error"]["message"]
    assert operations == ["Start"]


def test_trace_replay_stops_at_first_identity_divergence(tmp_path, monkeypatch, capsys):
    intro = _screen(question_name="ID intro")
    expected = _trace(
        TraceEntry(
            1,
            "start",
            None,
            {},
            True,
            intro,
            ScreenIdentity("id:intro", "explicit-id"),
            screen_content(intro),
        )
    )
    golden = tmp_path / "golden.trace.jsonl"
    write_trace(golden, expected)
    actual = _screen(question_name="ID changed")

    class FakeExecution:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, operation):
            return SimpleNamespace(ok=True, result=actual, error=None)

    monkeypatch.setattr(
        "docassemble_simulator.execution.InterviewExecution", FakeExecution
    )
    monkeypatch.setattr(
        cli,
        "_trace_metadata",
        lambda root, interview, resolved_configuration=None: expected.metadata,
    )
    args = SimpleNamespace(
        golden=str(golden),
        overrides=[],
        interview=None,
        json=True,
        _resolved_configuration=None,
    )

    assert cli.cmd_trace_replay(args, tmp_path) == 2
    payload = json.loads(capsys.readouterr().out)
    divergence = payload["error"]["details"]["first_divergence"]
    assert divergence["step"] == 1
    assert divergence["identity_match"] is False
    assert payload["error"]["details"]["counts"]["replayed"] == 1


def _automatic_group_trace():
    """A recorded two-submission walk with its trailing post-walk record."""
    intro = _screen()
    first = _screen(question_name="ID first automatic")
    second = _screen(question_name="ID second automatic")
    trailing = _screen(
        question_type="deadend",
        question_name="Question_4",
        fields=[],
        sought=None,
        orig_sought=None,
    )
    return (
        _trace(
            TraceEntry(
                1,
                "start",
                None,
                {},
                True,
                intro,
                ScreenIdentity("id:intro", "explicit-id"),
                screen_content(intro),
                intent="start",
            ),
            TraceEntry(
                2,
                "seek",
                None,
                {},
                True,
                first,
                ScreenIdentity("id:first automatic", "explicit-id"),
                screen_content(first),
                intent="auto",
                target="target_value",
                activate=True,
            ),
            TraceEntry(
                3,
                "seek",
                None,
                {},
                True,
                second,
                ScreenIdentity("id:second automatic", "explicit-id"),
                screen_content(second),
                intent="auto",
                target="target_value",
                activate=True,
            ),
            TraceEntry(
                4,
                "seek",
                None,
                {},
                True,
                trailing,
                ScreenIdentity("screen:question/deadend", "category"),
                (),
                intent="seek",
                target="target_value",
                activate=True,
                post_auto=True,
            ),
        ),
        (intro, first, second, trailing),
    )


def test_trace_replay_automatic_group_carries_the_trailing_expectation(
    tmp_path, monkeypatch, capsys
):
    trace, screens = _automatic_group_trace()
    golden = tmp_path / "golden.trace.jsonl"
    write_trace(golden, trace)
    observed = _screen(question_name="ID extra automatic")
    operations = []

    class FakeExecution:
        def __init__(self, *arguments, **keywords):
            pass

        def run(self, operation):
            from docassemble_simulator.execution import ReplayAutomatic, Start

            operations.append(operation)
            if isinstance(operation, Start):
                return SimpleNamespace(ok=True, result=screens[0], error=None)
            if isinstance(operation, ReplayAutomatic):
                return SimpleNamespace(
                    ok=True,
                    result={
                        **observed,
                        "auto_trace": [
                            {
                                "screen": screens[1],
                                "submitted": {},
                                "ok": True,
                                "error": None,
                            },
                            {
                                "screen": screens[2],
                                "submitted": {},
                                "ok": True,
                                "error": None,
                            },
                        ],
                        "replay_divergence": {
                            "phase": "trailing",
                            "seq": 4,
                            "observation": {
                                "screen": observed,
                                "error": None,
                                "ok": True,
                            },
                            "submitted": {},
                            "verdict": {
                                "identity_match": False,
                                "field_facts_match": False,
                                "flow_match": True,
                                "error_match": True,
                                "match": False,
                            },
                        },
                    },
                    error=None,
                )
            raise AssertionError(operation)

    monkeypatch.setattr(
        "docassemble_simulator.execution.InterviewExecution", FakeExecution
    )
    monkeypatch.setattr(
        cli,
        "_trace_metadata",
        lambda root, interview, resolved_configuration=None: trace.metadata,
    )
    args = SimpleNamespace(
        golden=str(golden),
        overrides=[],
        interview=None,
        json=True,
        _resolved_configuration=None,
    )

    assert cli.cmd_trace_replay(args, tmp_path) == 2
    payload = json.loads(capsys.readouterr().out)
    replay_operation = operations[-1]
    assert [item.seq for item in replay_operation.expectations] == [2, 3]
    # The trailing post-walk record is execution's own boundary expectation.
    assert replay_operation.trailing.seq == 4
    details = payload["error"]["details"]
    assert [step["match"] for step in details["steps"]] == [True, True, True, False]
    divergence = details["first_divergence"]
    assert divergence["step"] == 4
    assert divergence["identity_match"] is False
    # The recorded trailing identity mismatch is not a flow divergence; the
    # report keeps execution's own verdict instead of inventing one.
    assert divergence["flow_match"] is True
    assert divergence["error_match"] is True
    assert divergence["actual"]["identity"]["key"] == "id:extra automatic"
    assert divergence["actual"]["submitted"] == {}


def test_trace_replay_renders_executions_early_end_observation(
    tmp_path, monkeypatch, capsys
):
    """An early-end report shows the walk-ending outcome, not an empty one."""
    trace, screens = _automatic_group_trace()
    golden = tmp_path / "golden.trace.jsonl"
    write_trace(golden, trace)
    ending = _screen(
        kind="continue",
        question_type=None,
        question_name=None,
        fields=[],
        sought=None,
        orig_sought=None,
    )
    operations = []

    class FakeExecution:
        def __init__(self, *arguments, **keywords):
            pass

        def run(self, operation):
            from docassemble_simulator.execution import ReplayAutomatic, Start

            operations.append(operation)
            if isinstance(operation, Start):
                return SimpleNamespace(ok=True, result=screens[0], error=None)
            if isinstance(operation, ReplayAutomatic):
                return SimpleNamespace(
                    ok=True,
                    result={
                        **ending,
                        "auto_trace": [
                            {
                                "screen": screens[1],
                                "submitted": {},
                                "ok": True,
                                "error": None,
                            }
                        ],
                        "replay_divergence": {
                            "phase": "early-end",
                            "seq": 3,
                            "observation": {
                                "screen": ending,
                                "error": None,
                                "ok": True,
                            },
                            "submitted": {},
                            "verdict": {
                                "identity_match": False,
                                "field_facts_match": False,
                                "flow_match": True,
                                "error_match": True,
                                "match": False,
                            },
                        },
                    },
                    error=None,
                )
            raise AssertionError(operation)

    monkeypatch.setattr(
        "docassemble_simulator.execution.InterviewExecution", FakeExecution
    )
    monkeypatch.setattr(
        cli,
        "_trace_metadata",
        lambda root, interview, resolved_configuration=None: trace.metadata,
    )
    args = SimpleNamespace(
        golden=str(golden),
        overrides=[],
        interview=None,
        json=True,
        _resolved_configuration=None,
    )

    assert cli.cmd_trace_replay(args, tmp_path) == 2
    payload = json.loads(capsys.readouterr().out)
    divergence = payload["error"]["details"]["first_divergence"]
    assert divergence["step"] == 3
    assert divergence["actual"]["identity"]["key"] == "screen:continue"
    assert divergence["actual"]["ok"] is True
    assert divergence["actual"].get("error") is None
    assert divergence["identity_match"] is False
    assert divergence["flow_match"] is True


def test_trace_replay_reports_no_submission_for_a_screen_it_only_observed(
    tmp_path, monkeypatch, capsys
):
    trace, screens = _automatic_group_trace()
    golden = tmp_path / "golden.trace.jsonl"
    write_trace(golden, trace)
    changed = _screen(question_name="ID first automatic changed")
    operations = []

    class FakeExecution:
        def __init__(self, *arguments, **keywords):
            pass

        def run(self, operation):
            from docassemble_simulator.execution import ReplayAutomatic, Start

            operations.append(operation)
            if isinstance(operation, Start):
                return SimpleNamespace(ok=True, result=screens[0], error=None)
            if isinstance(operation, ReplayAutomatic):
                return SimpleNamespace(
                    ok=True,
                    result={
                        **changed,
                        "auto_trace": [
                            {
                                "screen": changed,
                                "submitted": {},
                                "ok": True,
                                "error": None,
                            }
                        ],
                        "replay_divergence": {
                            "phase": "before-submission",
                            "seq": 2,
                            "observation": {
                                "screen": changed,
                                "error": None,
                                "ok": True,
                            },
                            "submitted": {},
                            "verdict": {
                                "identity_match": False,
                                "field_facts_match": False,
                                "flow_match": True,
                                "error_match": True,
                                "match": False,
                            },
                        },
                    },
                    error=None,
                )
            raise AssertionError(operation)

    monkeypatch.setattr(
        "docassemble_simulator.execution.InterviewExecution", FakeExecution
    )
    monkeypatch.setattr(
        cli,
        "_trace_metadata",
        lambda root, interview, resolved_configuration=None: trace.metadata,
    )
    args = SimpleNamespace(
        golden=str(golden),
        overrides=["2.first_value=changed"],
        interview=None,
        json=True,
        _resolved_configuration=None,
    )

    assert cli.cmd_trace_replay(args, tmp_path) == 2
    payload = json.loads(capsys.readouterr().out)
    divergence = payload["error"]["details"]["first_divergence"]
    assert divergence["step"] == 2
    assert divergence["submitted"] == {"first_value": "changed"}
    # Nothing was submitted for the observed screen, even though a per-step
    # override was supplied for the recorded attempt.
    assert divergence["actual"]["submitted"] == {}


def test_trace_replay_rejects_metadata_mismatch_without_running(
    tmp_path, monkeypatch, capsys
):
    trace = _trace(_entry(1, "id:intro"))
    golden = tmp_path / "golden.trace.jsonl"
    write_trace(golden, trace)
    mismatched = _metadata(config_fingerprint="different")
    monkeypatch.setattr(
        cli,
        "_trace_metadata",
        lambda root, interview, resolved_configuration=None: mismatched,
    )
    args = SimpleNamespace(
        golden=str(golden),
        overrides=[],
        interview=None,
        json=True,
        _resolved_configuration=None,
    )

    assert cli.cmd_trace_replay(args, tmp_path) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["kind"] == "input"
    assert "config fingerprint" in payload["error"]["message"]


def test_trace_jsonl_round_trips_metadata_and_entries(tmp_path):
    path = tmp_path / "run.trace.jsonl"
    trace = ScreenTrace(
        metadata=_metadata(),
        entries=(
            TraceEntry(
                seq=1,
                operation="start",
                phase="intake",
                submitted={},
                ok=True,
                screen=_screen(),
                identity=ScreenIdentity("id:intro", "explicit-id"),
                content=screen_content(_screen()),
            ),
        ),
    )

    write_trace(path, trace)

    assert load_trace(path) == trace


def test_append_trace_computes_occurrence_and_sequence(tmp_path):
    path = tmp_path / "run.trace.jsonl"

    first = append_trace(
        path,
        _metadata(),
        operation="answer",
        ok=True,
        screen=_screen(),
        submitted={"user_name": "Alice"},
        phase="intake",
    )
    second = append_trace(
        path,
        _metadata(),
        operation="answer",
        ok=True,
        screen=_screen(),
        submitted={"user_name": "Bob"},
        phase="intake",
    )

    assert first.seq == 1
    assert second.seq == 2
    assert first.identity.occurrence == 1
    assert second.identity.occurrence == 2
    assert load_trace(path).entries == (first, second)


def test_append_trace_disambiguates_category_collisions(tmp_path):
    path = tmp_path / "run.trace.jsonl"
    terminal = _screen(
        question_type="deadend",
        question_name="Question_5",
        fields=[],
    )

    first = append_trace(path, _metadata(), operation="start", ok=True, screen=terminal)
    second = append_trace(
        path, _metadata(), operation="start", ok=True, screen=terminal
    )

    assert first.identity.key == "screen:question/deadend"
    assert second.identity.key == "screen:question/deadend#2"


def test_append_trace_rejects_mismatched_metadata(tmp_path):
    path = tmp_path / "run.trace.jsonl"
    append_trace(path, _metadata(), operation="start", ok=True, screen=_screen())

    try:
        append_trace(
            path,
            _metadata(config_fingerprint="fingerprint-2"),
            operation="answer",
            ok=True,
            screen=_screen(),
        )
    except TraceError as error:
        assert "fingerprint" in str(error)
    else:
        raise AssertionError("mismatched metadata was accepted")


def _entry(seq, key, *, rule="explicit-id", phase=None, screen=None, ok=True):
    screen = screen if screen is not None else _screen()
    return TraceEntry(
        seq=seq,
        operation="answer",
        phase=phase,
        submitted={},
        ok=ok,
        screen=screen,
        identity=ScreenIdentity(key, rule),
        content=screen_content(screen),
    )


def _trace(*entries, metadata=None):
    return ScreenTrace(metadata or _metadata(), tuple(entries))


def test_ordered_comparison_matches_identical_traces():
    trace = _trace(_entry(1, "id:intro"), _entry(2, "id:done"))

    comparison = compare_traces(trace, trace)

    assert comparison.matched is True
    assert comparison.matched_count == 2
    assert comparison.missing_count == 0
    assert comparison.extra_count == 0


def test_ordered_comparison_reports_order_without_losing_coverage():
    expected = _trace(_entry(1, "id:a"), _entry(2, "id:b"))
    actual = _trace(_entry(1, "id:b"), _entry(2, "id:a"))

    comparison = compare_traces(expected, actual)

    assert comparison.matched is False
    assert comparison.matched_count == 2
    assert comparison.missing_count == 0
    assert [diff.kind for diff in comparison.diffs if diff.kind == "order"]


def test_unordered_comparison_tolerates_reordering():
    expected = _trace(_entry(1, "id:a"), _entry(2, "id:b"))
    actual = _trace(_entry(1, "id:b"), _entry(2, "id:a"))

    comparison = compare_traces(expected, actual, ComparePolicy(order="unordered"))

    assert comparison.matched is True


def test_missing_entries_fail_strict_and_pass_when_allowed_but_stay_reported():
    expected = _trace(_entry(1, "id:a"), _entry(2, "id:b"))
    actual = _trace(_entry(1, "id:a"))

    strict = compare_traces(expected, actual, ComparePolicy(order="unordered"))
    allowed = compare_traces(
        expected, actual, ComparePolicy(order="unordered", missing="allow")
    )

    assert strict.matched is False
    assert strict.missing_count == 1
    assert allowed.matched is True
    assert allowed.missing_count == 1
    assert any(diff.kind == "missing" for diff in allowed.diffs)


def test_extra_entries_fail_strict_and_pass_when_allowed():
    expected = _trace(_entry(1, "id:a"))
    actual = _trace(_entry(1, "id:a"), _entry(2, "id:surprise"))

    strict = compare_traces(expected, actual, ComparePolicy(order="unordered"))
    allowed = compare_traces(
        expected, actual, ComparePolicy(order="unordered", extra="allow")
    )

    assert strict.matched is False
    assert strict.extra_count == 1
    assert allowed.matched is True
    assert allowed.extra_count == 1
    assert any(diff.kind == "extra" for diff in allowed.diffs)


def test_multiplicity_is_not_a_set():
    expected = _trace(_entry(1, "id:loop"), _entry(2, "id:loop"))
    actual = _trace(_entry(1, "id:loop"))

    comparison = compare_traces(
        expected, actual, ComparePolicy(order="unordered", missing="allow")
    )

    assert comparison.matched is True
    assert comparison.matched_count == 1
    assert comparison.missing_count == 1
    assert comparison.duplicate_count == 1


def test_content_mismatch_is_reported_separately_from_missing():
    expected_screen = _screen(
        fields=[{"variable": "a", "type": "text", "visible": True, "required": True}]
    )
    actual_screen = _screen(
        fields=[{"variable": "a", "type": "text", "visible": True, "required": False}]
    )

    comparison = compare_traces(
        _trace(_entry(1, "id:x", screen=expected_screen)),
        _trace(_entry(1, "id:x", screen=actual_screen)),
    )

    assert comparison.matched is False
    assert comparison.missing_count == 0
    assert comparison.extra_count == 0
    assert [diff.kind for diff in comparison.diffs] == ["content-mismatch"]


def test_full_text_comparison_is_opt_in():
    expected = _entry(1, "id:x", screen=_screen(question_text="One"))
    actual = _entry(1, "id:x", screen=_screen(question_text="Two"))

    assert compare_traces(_trace(expected), _trace(actual)).matched is True
    assert (
        compare_traces(
            _trace(expected), _trace(actual), ComparePolicy(full_text=True)
        ).matched
        is False
    )


def test_phased_comparison_orders_phases_but_not_entries_within_them():
    expected = _trace(
        _entry(1, "id:a", phase="intake"),
        _entry(2, "id:b", phase="intake"),
        _entry(3, "id:c", phase="download"),
    )
    actual = _trace(
        _entry(1, "id:b", phase="intake"),
        _entry(2, "id:a", phase="intake"),
        _entry(3, "id:c", phase="download"),
    )

    comparison = compare_traces(
        expected, actual, ComparePolicy(order="phased", phases=("intake", "download"))
    )

    assert comparison.matched is True
    assert [phase.phase for phase in comparison.phases] == ["intake", "download"]
    assert comparison.phases[0].expected_count == 2


def test_phased_comparison_reports_phase_order_violations():
    expected = _trace(
        _entry(1, "id:a", phase="intake"),
        _entry(2, "id:c", phase="download"),
    )
    actual = _trace(
        _entry(1, "id:c", phase="download"),
        _entry(2, "id:a", phase="intake"),
    )

    comparison = compare_traces(
        expected, actual, ComparePolicy(order="phased", phases=("intake", "download"))
    )

    assert comparison.matched is False
    assert any(diff.kind == "phase-order" for diff in comparison.diffs)


def test_phased_comparison_declares_phases_from_the_golden():
    expected = _trace(
        _entry(1, "id:a", phase="intake"),
        _entry(2, "id:b", phase="intake"),
        _entry(3, "id:c", phase="download"),
    )
    actual = _trace(
        _entry(1, "id:b", phase="intake"),
        _entry(2, "id:a", phase="intake"),
        _entry(3, "id:c", phase="download"),
    )

    comparison = compare_traces(expected, actual, ComparePolicy(order="phased"))

    assert comparison.matched is True
    assert [phase.phase for phase in comparison.phases] == ["intake", "download"]
    assert comparison.phases[0].expected_count == 2


def test_phased_comparison_honours_a_recorded_phase_order():
    expected = _trace(
        _entry(1, "id:a", phase="intake"),
        _entry(2, "id:b", phase="download"),
        metadata=_metadata(phase_order=("intake", "download")),
    )
    actual = _trace(
        _entry(1, "id:b", phase="download"),
        _entry(2, "id:a", phase="intake"),
    )

    comparison = compare_traces(expected, actual, ComparePolicy(order="phased"))

    assert comparison.matched is False
    assert any(diff.kind == "phase-order" for diff in comparison.diffs)


def test_explicit_phase_declaration_overrides_the_golden():
    expected = _trace(
        _entry(1, "id:a", phase="intake"),
        _entry(2, "id:b", phase="download"),
        metadata=_metadata(phase_order=("intake", "download")),
    )
    actual = _trace(
        _entry(1, "id:a", phase="intake"),
        _entry(2, "id:b", phase="download"),
    )

    comparison = compare_traces(
        expected, actual, ComparePolicy(order="phased", phases=("download", "intake"))
    )

    assert comparison.matched is False
    assert any(diff.kind == "phase-order" for diff in comparison.diffs)


def test_phased_comparison_requires_a_declaration_when_nothing_declares_phases():
    trace = _trace(_entry(1, "id:a"))

    with pytest.raises(TraceError, match="--phases"):
        compare_traces(trace, trace, ComparePolicy(order="phased"))


def test_a_phase_only_in_the_run_is_a_violation_with_extra_coverage():
    expected = _trace(
        _entry(1, "id:a", phase="intake"),
        _entry(2, "id:b", phase="download"),
    )
    actual = _trace(
        _entry(1, "id:a", phase="intake"),
        _entry(2, "id:b", phase="download"),
        _entry(3, "id:post", phase="post"),
    )

    comparison = compare_traces(expected, actual, ComparePolicy(order="phased"))

    assert comparison.matched is False
    assert any(diff.kind == "phase-order" for diff in comparison.diffs)
    assert comparison.extra_count == 1


def test_compare_rejects_mismatched_metadata():
    expected = _trace(_entry(1, "id:a"))
    actual = _trace(
        _entry(1, "id:a"), metadata=_metadata(interview="docassemble.other:main.yml")
    )

    with pytest.raises(TraceError, match="interview"):
        compare_traces(expected, actual)


def test_exception_marks_a_known_difference_and_keeps_counts():
    expected = _trace(_entry(1, "id:a"))
    actual = _trace()
    exception = TraceException(
        interview=_metadata().interview,
        phase="*",
        identity="id:a",
        category="capability-boundary",
        reason="known boundary",
    )

    comparison = compare_traces(
        expected,
        actual,
        ComparePolicy(order="unordered"),
        exceptions=(exception,),
    )

    assert comparison.matched is True
    assert comparison.missing_count == 1
    diff = next(item for item in comparison.diffs if item.kind == "missing")
    assert diff.excepted == "known boundary"


def test_unused_exception_is_an_error():
    trace = _trace(_entry(1, "id:a"))
    exception = TraceException(
        interview=_metadata().interview,
        phase="*",
        identity="id:never-seen",
        category="capability-boundary",
        reason="stale",
    )

    with pytest.raises(TraceError, match="unused"):
        compare_traces(trace, trace, exceptions=(exception,))


def test_exceptions_do_not_excuse_order_violations():
    expected = _trace(_entry(1, "id:a"), _entry(2, "id:b"))
    actual = _trace(_entry(1, "id:b"), _entry(2, "id:a"))
    exceptions = tuple(
        TraceException(
            interview=_metadata().interview,
            phase="*",
            identity=identity,
            category="capability-boundary",
            reason="known",
        )
        for identity in ("id:a", "id:b")
    )

    comparison = compare_traces(expected, actual, exceptions=exceptions)

    assert comparison.matched is False
    assert any(diff.kind == "order" for diff in comparison.diffs)


def test_load_exceptions_reads_reviewed_entries(tmp_path):
    path = tmp_path / "exceptions.toml"
    path.write_text(
        "[[exceptions]]\n"
        'interview = "docassemble.probe:data/questions/main.yml"\n'
        'phase = "intake"\n'
        'identity = "id:legacy"\n'
        'category = "capability-boundary"\n'
        'reason = "legacy screen retired in this interview"\n',
        encoding="utf-8",
    )

    exceptions = load_exceptions(path)

    assert exceptions == (
        TraceException(
            interview="docassemble.probe:data/questions/main.yml",
            phase="intake",
            identity="id:legacy",
            category="capability-boundary",
            reason="legacy screen retired in this interview",
        ),
    )


def test_load_exceptions_rejects_duplicates_and_fault_masking(tmp_path):
    path = tmp_path / "exceptions.toml"
    path.write_text(
        "[[exceptions]]\n"
        'interview = "docassemble.probe:data/questions/main.yml"\n'
        'phase = "intake"\n'
        'identity = "id:legacy"\n'
        'category = "capability-boundary"\n'
        'reason = "first"\n'
        "[[exceptions]]\n"
        'interview = "docassemble.probe:data/questions/main.yml"\n'
        'phase = "intake"\n'
        'identity = "id:legacy"\n'
        'category = "deliberate-error"\n'
        'reason = "second"\n',
        encoding="utf-8",
    )
    with pytest.raises(TraceError, match="overlapping"):
        load_exceptions(path)

    path.write_text(
        "[[exceptions]]\n"
        'interview = "docassemble.probe:data/questions/main.yml"\n'
        'phase = "*"\n'
        'identity = "error:fault"\n'
        'category = "capability-boundary"\n'
        'reason = "nope"\n',
        encoding="utf-8",
    )
    with pytest.raises(TraceError, match="fault"):
        load_exceptions(path)


def test_trace_schema_versions_the_explicit_generic_anchor_rule():
    assert _metadata().as_dict()["trace"] == 4


def test_load_trace_rejects_an_unsupported_schema(tmp_path):
    path = tmp_path / "run.trace.jsonl"
    path.write_text(
        '{"trace": 99, "interview": "docassemble.probe:main.yml",'
        ' "config_fingerprint": "f1"}\n',
        encoding="utf-8",
    )

    with pytest.raises(TraceError, match="schema"):
        load_trace(path)


def test_load_trace_rejects_a_malformed_entry(tmp_path):
    path = tmp_path / "run.trace.jsonl"
    path.write_text(
        '{"trace": 2, "interview": "docassemble.probe:main.yml",'
        ' "config_fingerprint": "f1"}\nnot json\n',
        encoding="utf-8",
    )

    with pytest.raises(TraceError, match="line 2"):
        load_trace(path)


def test_error_identity_never_emits_a_generated_instance_name():
    identity = error_identity(
        {
            "kind": "unresolved-variable",
            "details": {
                "failure_kind": "unresolved-variable",
                "sought_variable": "CMWHWUrsOoma['will_trust']",
            },
        }
    )

    assert identity == ScreenIdentity("error:unresolved", "error")


def test_screen_identity_never_invents_a_screen_for_an_error_outcome():
    identity = screen_identity(
        {
            "kind": "error",
            "failure_kind": "unresolved-variable",
            "sought_variable": "clients[0].name",
        }
    )

    assert identity == ScreenIdentity("error:unresolved:clients[i].name", "error")


def test_identity_derivation_does_not_require_a_kind_field():
    assert screen_identity(
        {"sought": "user_name", "orig_sought": "user_name"}
    ) == ScreenIdentity("var:user_name", "targeted-variable")


def test_error_identity_reads_a_bare_error_outcome():
    identity = error_identity(
        {
            "kind": "error",
            "failure_kind": "unresolved-variable",
            "sought_variable": "clients[0].name",
        }
    )

    assert identity == ScreenIdentity("error:unresolved:clients[i].name", "error")


def test_append_trace_records_an_error_outcome_as_a_failure(tmp_path):
    path = tmp_path / "run.trace.jsonl"
    outcome = {
        "kind": "error",
        "failure_kind": "unresolved-variable",
        "sought_variable": "clients[0].name",
        "message": "name is undefined",
    }

    entry = append_trace(path, _metadata(), operation="start", ok=False, screen=outcome)

    assert entry.identity == ScreenIdentity("error:unresolved:clients[i].name", "error")
    assert entry.screen is None
    assert entry.error == outcome


def test_rendered_constraints_are_recorded_but_debug_only_for_comparison(tmp_path):
    old_screen = _screen()
    new_screen = _screen(
        fields=[{**old_screen["fields"][0], "constraints": {"maxlength": 4}}]
    )
    old = _trace(_entry(1, "id:intro", screen=old_screen))
    new = _trace(_entry(1, "id:intro", screen=new_screen))
    path = tmp_path / "constraints.jsonl"
    write_trace(path, new)
    loaded = load_trace(path)
    assert loaded.entries[0].screen["fields"][0]["constraints"] == {"maxlength": 4}
    assert compare_traces(old, loaded).matched
