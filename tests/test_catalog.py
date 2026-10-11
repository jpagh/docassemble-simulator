"""Catalog provenance stays useful without exposing runtime code objects."""

from types import SimpleNamespace

from docassemble_simulator.catalog import InterviewCatalog, _provenance


def _question(
    tmp_path, *, line, variable, guard=None, explicit_id=None, source_code=True
):
    source_path = tmp_path / "main.yml"
    source_path.write_text(
        "---\n"
        + (f"id: {explicit_id}\n" if explicit_id else "")
        + "question: Example\n"
        + (f"if: {guard}\n" if isinstance(guard, str) else "")
        + (
            "if:\n" + "".join(f"  - {item}\n" for item in guard)
            if isinstance(guard, list)
            else ""
        )
        + f"fields:\n  - Example: {variable}\n"
    )
    debug = {"if": guard} if guard is not None else {}
    return SimpleNamespace(
        line_number=line,
        from_source=SimpleNamespace(
            path="docassemble.pkg:data/questions/main.yml",
            filepath=str(source_path),
        ),
        data_for_debug=debug,
        source_code=source_path.read_text() if source_code else None,
        id=explicit_id,
        name=explicit_id or f"Question_{line}",
        question_type="fields",
        fields=[SimpleNamespace(saveas=variable)],
        is_mandatory=False,
    )


def test_questions_and_index_expose_guarded_block_provenance(tmp_path, monkeypatch):
    package = tmp_path / "docassemble" / "pkg" / "data" / "questions"
    package.mkdir(parents=True)
    (package.parent.parent / "__init__.py").write_text("")
    first = _question(
        tmp_path,
        line=1,
        variable="shared",
        guard="user.kind == 'a'",
        explicit_id="first",
    )
    second = _question(
        tmp_path,
        line=8,
        variable="shared",
        guard=["user.kind == 'b'", "user.kind == 'c'"],
    )
    unguarded = _question(tmp_path, line=15, variable="plain")
    interview = SimpleNamespace(
        questions_list=[first, second, unguarded],
        questions={
            "shared": {"*": [first, second]},
            "plain": {"*": [unguarded]},
        },
    )
    catalog = InterviewCatalog(tmp_path, "docassemble.pkg:data/questions/main.yml")
    monkeypatch.setattr(catalog, "_compile_for_inspection", lambda: interview)

    blocks = catalog.questions().result["blocks"]
    assert [block["source_file"] for block in blocks] == [
        "docassemble.pkg:data/questions/main.yml"
    ] * 3
    assert blocks[0]["location"].endswith(":1")
    assert blocks[0]["id"] == "first"
    assert blocks[0]["guards"] == ["user.kind == 'a'"]
    assert blocks[1]["guards"] == ["user.kind == 'b'", "user.kind == 'c'"]
    assert "<code object" not in repr(blocks)
    assert blocks[2]["guard"] == "none"

    indexed = catalog.index("shared").result["index"]["shared"]
    assert [entry["guards"] for entry in indexed] == [
        ["user.kind == 'a'"],
        ["user.kind == 'b'", "user.kind == 'c'"],
    ]
    assert indexed[0]["id"] == "first"
    assert all(entry["source_file"] for entry in indexed)


def test_why_uses_last_loaded_order_and_reports_guard_conflicts(tmp_path, monkeypatch):
    package = tmp_path / "docassemble" / "pkg" / "data" / "questions"
    package.mkdir(parents=True)
    (package.parent.parent / "__init__.py").write_text("")
    first = _question(
        tmp_path,
        line=1,
        variable="shared",
        guard="user.kind == 'a'",
        explicit_id="first",
    )
    second = _question(
        tmp_path,
        line=8,
        variable="shared",
        guard="user.kind == 'b'",
        explicit_id="second",
    )
    interview = SimpleNamespace(
        questions={"shared": {"*": [first, second]}},
        questions_list=[first, second],
    )
    catalog = InterviewCatalog(tmp_path, "docassemble.pkg:data/questions/main.yml")
    monkeypatch.setattr(catalog, "_compile_for_inspection", lambda: interview)

    report = catalog.why("shared").result

    assert [item["id"] for item in report["candidates"]] == ["second", "first"]
    assert report["candidates"][0]["winner"] is True
    assert report["candidates"][0]["guards"] == ["user.kind == 'b'"]
    assert report["conflict"] is False

    unguarded = _question(tmp_path, line=15, variable="shared")
    interview.questions["shared"]["*"] = [first, unguarded]
    report = catalog.why("shared", {"user": SimpleNamespace(kind="a")}).result
    assert report["conflict"] is True
    assert report["conflict_details"]["kind"] == "resolution-conflict"
    assert report["candidates"][0]["live"] is True


def test_check_reports_duplicate_ids_and_separates_overrides(tmp_path, monkeypatch):
    package = tmp_path / "docassemble" / "pkg" / "data" / "questions"
    package.mkdir(parents=True)
    (package.parent.parent / "__init__.py").write_text("")
    first = _question(
        tmp_path,
        line=1,
        variable="first",
        explicit_id="shared",
    )
    second = _question(
        tmp_path,
        line=8,
        variable="second",
        explicit_id="shared",
    )
    deliberate = _question(
        tmp_path,
        line=15,
        variable="third",
        explicit_id="deliberate",
    )
    deliberate.override = True
    deliberate_later = _question(
        tmp_path,
        line=22,
        variable="fourth",
        explicit_id="deliberate",
    )
    deliberate_later.supersedes = "deliberate"
    interview = SimpleNamespace(
        questions_list=[first, second, deliberate, deliberate_later]
    )
    catalog = InterviewCatalog(tmp_path, "docassemble.pkg:data/questions/main.yml")
    monkeypatch.setattr(catalog, "_compile", lambda identity: interview)

    result = catalog.check().result

    assert result["totals"]["duplicate_ids"] == 1
    assert {item["kind"] for item in result["duplicate_ids"]} == {
        "duplicate-id",
        "deliberate-override",
    }
    assert len(result["duplicate_ids"][0]["blocks"]) == 2
    assert result["totals"]["checked"] == 1


def test_why_uses_runtime_ordering_and_current_language(tmp_path, monkeypatch):
    package = tmp_path / "docassemble" / "pkg" / "data" / "questions"
    package.mkdir(parents=True)
    (package.parent.parent / "__init__.py").write_text("")
    english = _question(
        tmp_path,
        line=1,
        variable="shared",
        explicit_id="english",
    )
    french = _question(
        tmp_path,
        line=8,
        variable="shared",
        explicit_id="french",
    )

    class RuntimeInterview(SimpleNamespace):
        def sort_with_orderings(self, questions):
            return iter(reversed(questions))

    interview = RuntimeInterview(
        questions={"shared": {"en": [english], "fr": [english, french]}},
        questions_list=[english, french],
    )
    catalog = InterviewCatalog(tmp_path, "docassemble.pkg:data/questions/main.yml")
    monkeypatch.setattr(catalog, "_compile_for_inspection", lambda: interview)

    report = catalog.why("shared", language="fr").result

    assert [item["id"] for item in report["candidates"]] == ["french", "english"]
    assert report["candidates"][0]["language"] == "fr"
    assert report["winner"]["id"] == "french"


def test_unrelated_supersedes_does_not_exempt_duplicate_id(tmp_path, monkeypatch):
    package = tmp_path / "docassemble" / "pkg" / "data" / "questions"
    package.mkdir(parents=True)
    (package.parent.parent / "__init__.py").write_text("")
    first = _question(tmp_path, line=1, variable="first", explicit_id="shared")
    second = _question(tmp_path, line=8, variable="second", explicit_id="shared")
    second.supersedes = "unrelated"
    interview = SimpleNamespace(questions_list=[first, second])
    catalog = InterviewCatalog(tmp_path, "docassemble.pkg:data/questions/main.yml")
    monkeypatch.setattr(catalog, "_compile", lambda identity: interview)

    result = catalog.check().result

    assert result["totals"]["duplicate_ids"] == 1
    assert result["duplicate_ids"][0]["kind"] == "duplicate-id"
    assert [block["id"] for block in result["duplicate_ids"][0]["blocks"]] == [
        "shared",
        "shared",
    ]


def test_mixed_duplicate_group_reports_valid_relationship_and_accidental_collision(
    tmp_path, monkeypatch
):
    package = tmp_path / "docassemble" / "pkg" / "data" / "questions"
    package.mkdir(parents=True)
    (package.parent.parent / "__init__.py").write_text("")
    first = _question(tmp_path, line=1, variable="first", explicit_id="mixed")
    second = _question(tmp_path, line=8, variable="second", explicit_id="mixed")
    second.supersedes = "mixed"
    accidental = _question(
        tmp_path, line=15, variable="accidental", explicit_id="mixed"
    )
    interview = SimpleNamespace(questions_list=[first, second, accidental])
    catalog = InterviewCatalog(tmp_path, "docassemble.pkg:data/questions/main.yml")
    monkeypatch.setattr(catalog, "_compile", lambda identity: interview)

    result = catalog.check().result

    assert result["totals"]["duplicate_ids"] == 1
    assert {item["kind"] for item in result["duplicate_ids"]} == {
        "duplicate-id",
        "deliberate-override",
    }
    deliberate = next(
        item
        for item in result["duplicate_ids"]
        if item["kind"] == "deliberate-override"
    )
    assert len(deliberate["blocks"]) == 2
    assert deliberate["blocks"][1]["relationship"] == "mixed"


def test_check_reports_complete_totals(tmp_path, monkeypatch):
    package = tmp_path / "docassemble" / "pkg" / "data" / "questions"
    package.mkdir(parents=True)
    (package.parent.parent / "__init__.py").write_text("")
    catalog = InterviewCatalog(tmp_path, "docassemble.pkg:data/questions/main.yml")
    monkeypatch.setattr(
        catalog,
        "_compile",
        lambda identity: SimpleNamespace(questions_list=[]),
    )

    result = catalog.check().result

    assert result["totals"] == {
        "checked": 1,
        "ok": 1,
        "failed": 0,
        "duplicate_ids": 0,
    }


def test_provenance_reads_authored_guard_when_runtime_drops_block_text(tmp_path):
    question = _question(
        tmp_path,
        line=1,
        variable="value",
        guard="user.ready",
        source_code=False,
    )
    question.data_for_debug = {}
    question.from_source.filepath = str(tmp_path / "fallback.yml")
    (tmp_path / "fallback.yml").write_text(
        "---\nquestion: Example\nif: user.ready\nfields:\n  - Value: value\n"
    )

    provenance = _provenance(question)

    assert provenance["guards"] == ["user.ready"]
    assert provenance["location"].endswith(":1")
