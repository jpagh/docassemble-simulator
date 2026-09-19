"""Speed tests for screen identity, trace records, and trace comparison."""

import pytest

from docassemble_simulator.trace import (
    ComparePolicy,
    ScreenIdentity,
    ScreenTrace,
    TraceEntry,
    TraceError,
    TraceException,
    TraceMetadata,
    append_trace,
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
        "id:client identity", "explicit-id"
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
        '{"trace": 1, "interview": "docassemble.probe:main.yml",'
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
