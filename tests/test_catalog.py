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


def test_check_reports_complete_totals(tmp_path, monkeypatch):
    package = tmp_path / "docassemble" / "pkg" / "data" / "questions"
    package.mkdir(parents=True)
    (package.parent.parent / "__init__.py").write_text("")
    catalog = InterviewCatalog(
        tmp_path, "docassemble.pkg:data/questions/main.yml"
    )
    monkeypatch.setattr(
        catalog,
        "_compile",
        lambda identity: SimpleNamespace(questions_list=[]),
    )

    result = catalog.check().result

    assert result["totals"] == {"checked": 1, "ok": 1, "failed": 0}


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
