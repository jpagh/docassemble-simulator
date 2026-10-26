"""Row controls through the public describe and transport seams."""

import base64
import json
from types import SimpleNamespace
from urllib.parse import urlencode

from docassemble_simulator.describe import describe_question_result


def action_link(action, **arguments):
    encoded = (
        base64.b64encode(
            json.dumps({"action": action, "arguments": arguments}).encode()
        )
        .decode()
        .rstrip("=")
    )
    return '<a href="/interview?' + urlencode({"action": encoded}) + '">button</a>'


def test_screen_describes_rendered_add_delete_and_edit_controls():
    question = SimpleNamespace(question_type="review", fields=[], name="people")
    table = action_link("_da_list_remove", list="people", item="0")
    add = action_link("_da_list_add", list="people")
    described = describe_question_result(
        {
            "question": question,
            "subquestion_text": table
            + add
            + add
            + action_link(
                "_da_list_edit",
                items=[{"follow up": ["people[0].name"]}],
            ),
        },
        {},
    )
    assert described["row_actions"] == [
        {"kind": "delete", "target": "people", "row": 0},
        {"kind": "add", "target": "people"},
        {"kind": "edit", "target": "people", "row": 0},
    ]
    assert describe_question_result({"question": question}, {})["row_actions"] == []


def test_nested_controls_use_the_native_list_anchor_and_ignore_malformed_links():
    question = SimpleNamespace(question_type="review", fields=[], name="children")
    described = describe_question_result(
        {
            "question": question,
            "subquestion_text": action_link(
                "_da_list_add", list="children[0].grandchildren"
            )
            + action_link(
                "_da_list_edit",
                items=[
                    {"follow up": ["children[0].grandchildren[0].aliases[1].name"]},
                    {
                        "action": "_da_list_ensure_complete",
                        "arguments": {"group": "children[0].grandchildren"},
                    },
                ],
            )
            + action_link("_da_list_edit", items=[]),
        },
        {},
    )
    assert described["row_actions"] == [
        {"kind": "add", "target": "children[0].grandchildren"},
        {"kind": "edit", "target": "children[0].grandchildren", "row": 0},
    ]


def test_screen_describes_native_action_sequences_from_rendered_notes():
    question = SimpleNamespace(
        question_type="fields",
        name="trust",
        fields=[SimpleNamespace(number=0, datatype="note")],
    )
    sequence = action_link(
        "_da_force_ask",
        variables=[
            {
                "action": "people.new_item_parameters",
                "arguments": {"new_item_entity": "trust"},
            },
            {"action": "_da_list_add", "arguments": {"list": "people"}},
        ],
    )
    result = {"question": question, "extras": {"note": {0: sequence}}}
    described = describe_question_result(result, {})
    action = described["actions"][0]
    assert action["label"] == "button"
    assert action["source"] == "field:0:note"
    assert action["payload"] == {
        "action": "_da_force_ask",
        "arguments": {
            "variables": [
                {
                    "action": "people.new_item_parameters",
                    "arguments": {"new_item_entity": "trust"},
                },
                {"action": "_da_list_add", "arguments": {"list": "people"}},
            ]
        },
    }
    assert action["id"] == describe_question_result(result, {})["actions"][0]["id"]


def test_screen_actions_exclude_hidden_notes_and_distinguish_creation_parameters():
    question = SimpleNamespace(
        question_type="fields",
        name="trust",
        fields=[SimpleNamespace(number=n, datatype="note") for n in range(3)],
    )
    described = describe_question_result(
        {
            "question": question,
            "extras": {
                "note": {
                    n: action_link("people.new_item_parameters", new_item_entity=entity)
                    for n, entity in enumerate(("trust", "individual", "deceased"))
                },
                "ok": {0: True, 1: True, 2: False},
            },
        },
        {},
    )
    assert [a["payload"]["arguments"] for a in described["actions"]] == [
        {"new_item_entity": "trust"},
        {"new_item_entity": "individual"},
    ]
    assert len({a["id"] for a in described["actions"]}) == 2
    other = describe_question_result(
        {
            "question": SimpleNamespace(
                question_type="fields", name="different", fields=question.fields
            ),
            "extras": {
                "note": {
                    0: action_link(
                        "people.new_item_parameters", new_item_entity="trust"
                    )
                }
            },
        },
        {},
    )
    assert described["actions"][0]["id"] != other["actions"][0]["id"]


def test_screen_actions_report_unsupported_links_without_accepting_arbitrary_urls():
    question = SimpleNamespace(question_type="fields", fields=[], name="links")
    text = (
        action_link("event", value=1).replace(
            "/interview?", "https://example.com/interview?"
        )
        + '<a href="javascript:skip()">Skip</a>'
        + '<a href="/interview?action=not-base64">Broken</a>'
    )
    actions = describe_question_result(
        {"question": question, "question_text": text}, {}
    )["actions"]
    assert len(actions) == 3
    assert all("unsupported" in a and "payload" not in a for a in actions)


def test_screen_actions_reject_cross_interview_and_extra_navigation_parameters():
    question = SimpleNamespace(question_type="fields", fields=[], name="links")
    link = action_link("event", value=1)
    text = link.replace("/interview?", "/interview?i=other.yml&amp;") + link.replace(
        "/interview?", "/interview?reset=1&amp;"
    )
    described = describe_question_result(
        {
            "question": question,
            "interview": "main.yml",
            "question_text": text,
        },
        {},
    )
    assert all("unsupported" in a for a in described["actions"])


def test_screen_action_cli_worker_operation_parity():
    from docassemble_simulator import cli
    from docassemble_simulator.execution import ScreenAction
    from docassemble_simulator.worker import operation_from_request

    args = cli.build_parser().parse_args(["action", "control-id"])
    assert args.func is cli.cmd_execution
    assert args.action_id == "control-id"
    assert operation_from_request(
        {"operation": "action", "action_id": "control-id"}
    ) == ScreenAction("control-id")


def test_row_action_cli_worker_operation_parity():
    from docassemble_simulator import cli
    from docassemble_simulator.execution import RowAction
    from docassemble_simulator.worker import operation_from_request

    args = cli.build_parser().parse_args(["row-action", "edit", "people", "--row", "0"])
    assert args.func is cli.cmd_execution
    assert (args.kind, args.target, args.row) == ("edit", "people", 0)
    assert operation_from_request(
        {"operation": "row-action", "kind": "edit", "target": "people", "row": 0}
    ) == RowAction("edit", "people", 0)


def test_row_action_cli_and_worker_return_same_payload(tmp_path, monkeypatch, capsys):
    from docassemble_simulator import cli
    from docassemble_simulator._outcomes import Outcome
    from docassemble_simulator.execution import ExecutionScope, RowAction
    from docassemble_simulator.worker import Worker

    class Execution:
        scope = ExecutionScope(
            tmp_path, "main.yml", "", tmp_path, tmp_path / "state.pkl"
        )

        def run(self, operation):
            assert operation == RowAction("add", "people")
            return Outcome(
                True,
                {"kind": "question", "question_text": "New row", "row_actions": []},
            )

    execution = Execution()
    monkeypatch.setattr(cli, "_execution", lambda args, root: execution)
    args = cli.build_parser().parse_args(["row-action", "add", "people", "--json"])
    assert cli.cmd_execution(args, tmp_path) == 0
    one_shot = json.loads(capsys.readouterr().out)
    persistent = Worker(tmp_path, execution=execution).handle(
        {"operation": "row-action", "kind": "add", "target": "people"}
    )
    assert one_shot["ok"] == persistent["ok"]
    assert one_shot["result"] == persistent["result"]


def test_worker_rejects_malformed_row_action_as_input(tmp_path):
    from test_worker import FakeExecution

    from docassemble_simulator.worker import Worker

    worker = Worker(tmp_path, execution=FakeExecution(tmp_path))
    response = worker.handle(
        {"operation": "row-action", "kind": {}, "target": "people"}
    )
    assert not response["ok"]
    assert response["error"]["kind"] == "input"
