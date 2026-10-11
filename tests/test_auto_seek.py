"""Default submission and auto-seek reporting contracts."""

from types import SimpleNamespace

from docassemble_simulator import cli, execution


def _screen(name, variable):
    return {
        "kind": "question",
        "question_name": name,
        "sought": variable,
        "orig_sought": variable,
        "fields": [{"variable": variable, "type": "text", "required": False}],
    }


def test_auto_seek_reports_every_submitted_screen_until_resolution(monkeypatch):
    screens = iter(
        [
            _screen("first", "first"),
            _screen("second", "second"),
            {"kind": "continue", "message": "resolved"},
        ]
    )
    submitted = []

    def seek(*_args):
        return next(screens)

    def submit(_interview, _namespace, _status, screen):
        submitted.append(screen["question_name"])
        return {"kind": "question", "question_name": "assembled"}, None

    monkeypatch.setattr(execution, "_seek", seek)
    monkeypatch.setattr(execution, "_auto_submit_screen", submit)

    result = execution._seek_auto(object(), {}, object(), "target", False)

    assert submitted == ["first", "second"]
    assert [item["question_name"] for item in result["auto_submitted"]] == [
        "first",
        "second",
    ]
    assert result["auto_stop"] == {"reason": "sought variable resolved"}
    assert result["kind"] == "continue"


def test_auto_seek_stops_before_submitting_a_required_screen(monkeypatch):
    screen = _screen("needs-input", "name")
    monkeypatch.setattr(execution, "_seek", lambda *_args: screen)
    monkeypatch.setattr(
        execution,
        "_auto_submit_screen",
        lambda *_args: (
            None,
            {
                "reason": "required fields missing",
                "submitted": False,
                "missing_required": ["name"],
                "screen": screen,
            },
        ),
    )

    result = execution._seek_auto(object(), {}, object(), "target", False)

    assert result["auto_submitted"] == []
    assert result["auto_stop"]["reason"] == "required fields missing"
    assert result["auto_stop"]["missing_required"] == ["name"]
    assert result["question_name"] == "needs-input"


def test_recording_auto_seek_appends_each_step(monkeypatch, tmp_path):
    recorded = []
    final = _screen("final", "final")
    steps = [_screen("first", "first"), _screen("second", "second")]
    args = SimpleNamespace(
        record=str(tmp_path / "run.trace.jsonl"),
        command="seek",
        interview=None,
        phase=None,
        _resolved_configuration=None,
    )
    operation = SimpleNamespace(assignments=())
    payload = {
        "ok": True,
        "result": {
            **final,
            "auto_trace": [
                {"screen": steps[0], "submitted": {}},
                {"screen": steps[1], "submitted": {}},
            ],
        },
    }

    monkeypatch.setattr(cli, "_trace_metadata", lambda *_args: object())
    monkeypatch.setattr(
        "docassemble_simulator.trace.append_trace",
        lambda _path, _metadata, **kwargs: recorded.append(kwargs),
    )

    cli._record_execution(args, tmp_path, object(), operation, payload)

    assert [entry["screen"]["question_name"] for entry in recorded] == [
        "first",
        "second",
        "final",
    ]
