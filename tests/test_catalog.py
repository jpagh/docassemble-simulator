"""Catalog provenance stays useful without exposing runtime code objects."""

import json
import os
import sys
import types
from types import SimpleNamespace

from docassemble_simulator import catalog as catalog_module
from docassemble_simulator import cli
from docassemble_simulator._outcomes import ErrorKind
from docassemble_simulator.catalog import InterviewCatalog


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
    assert report["conflict_details"]["kind"] is ErrorKind.RESOLUTION_CONFLICT
    assert report["candidates"][0]["live"] is True


def test_why_keeps_unknown_guards_conservative_without_overstating_winners(
    tmp_path, monkeypatch
):
    package = tmp_path / "docassemble" / "pkg" / "data" / "questions"
    package.mkdir(parents=True)
    (package.parent.parent / "__init__.py").write_text("")
    catalog = InterviewCatalog(tmp_path, "docassemble.pkg:data/questions/main.yml")
    interview = SimpleNamespace(questions={"shared": {"*": []}}, questions_list=[])
    monkeypatch.setattr(catalog, "_compile_for_inspection", lambda: interview)
    namespace = {"user": SimpleNamespace(kind="a")}

    def explain(guards):
        candidates = [
            _question(
                tmp_path,
                line=index,
                variable="shared",
                guard=guard,
                explicit_id=f"candidate-{index}",
            )
            for index, guard in enumerate(guards, start=1)
        ]
        interview.questions["shared"]["*"] = candidates
        return catalog.why("shared", namespace).result

    unknown_unknown = explain(["missing == 'a'", "missing == 'a'"])
    assert unknown_unknown["conflict"] is True
    assert unknown_unknown["winner"] is None
    assert [candidate["state"] for candidate in unknown_unknown["candidates"]] == [
        "unknown",
        "unknown",
    ]
    assert all(
        "NameError" in candidate["guard_error"]
        for candidate in unknown_unknown["candidates"]
    )

    live_unknown = explain(["user.kind == 'a'", "missing == 'a'"])
    assert live_unknown["conflict"] is True
    assert live_unknown["winner"] is None
    assert [candidate["state"] for candidate in live_unknown["candidates"]] == [
        "unknown",
        "live",
    ]

    false_unknown = explain(["user.kind == 'never'", "missing == 'a'"])
    assert false_unknown["conflict"] is False
    assert false_unknown["winner"] is None
    assert {candidate["state"] for candidate in false_unknown["candidates"]} == {
        "inactive",
        "unknown",
    }

    unguarded_duplicates = explain([None, None])
    assert unguarded_duplicates["conflict"] is True
    assert unguarded_duplicates["winner"]["state"] == "live"

    provably_disjoint = explain(["missing == 'a'", "missing == 'b'"])
    assert provably_disjoint["conflict"] is False
    assert provably_disjoint["winner"] is None


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
        explicit_id="deliberate-base",
    )
    deliberate_later = _question(
        tmp_path,
        line=22,
        variable="fourth",
        explicit_id="deliberate-replacement",
    )
    deliberate_later.supersedes = "deliberate-base"
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
    assert result["duplicate_ids"][0]["kind"] is ErrorKind.DUPLICATE_ID
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
    first = _question(tmp_path, line=1, variable="first", explicit_id="mixed-base")
    second = _question(
        tmp_path, line=8, variable="second", explicit_id="mixed-replacement"
    )
    second.supersedes = "mixed-base"
    accidental = _question(
        tmp_path,
        line=15,
        variable="accidental",
        explicit_id="mixed-replacement",
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
    assert [block["id"] for block in deliberate["blocks"]] == [
        "mixed-base",
        "mixed-replacement",
    ]
    assert deliberate["blocks"][1]["relationship"] == "mixed-base"


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


def test_public_catalog_reports_authored_provenance_without_runtime_block_text(
    tmp_path, monkeypatch, capsys
):
    package = tmp_path / "docassemble" / "pkg" / "data" / "questions"
    package.mkdir(parents=True)
    (package.parent.parent / "__init__.py").write_text("")
    fallback = tmp_path / "fallback.yml"
    fallback.write_text(
        "---\n"
        "id: first\n"
        "question: Example\n"
        "if: user.kind == 'a'\n"
        "fields:\n"
        "  - Example: shared\n"
        "---\n"
        "question: Example\n"
        "if:\n"
        "  - user.kind == 'b'\n"
        "  - user.kind == 'c'\n"
        "fields:\n"
        "  - Example: shared\n"
        "---\n"
        "question: Example\n"
        "fields:\n"
        "  - Example: plain\n",
        encoding="utf-8",
    )

    def fallback_question(line, variable, *, explicit_id=None):
        question = _question(
            tmp_path,
            line=line,
            variable=variable,
            explicit_id=explicit_id,
            source_code=False,
        )
        # A controlled runtime substitute has discarded compiled block text and
        # debug metadata; the public catalog must recover authored provenance.
        question.data_for_debug = {}
        question.from_source.filepath = str(fallback)
        return question

    first = fallback_question(1, "shared", explicit_id="first")
    second = fallback_question(8, "shared")
    unguarded = fallback_question(15, "plain")
    interview = SimpleNamespace(
        questions_list=[first, second, unguarded],
        questions={
            "shared": {"*": [first, second]},
            "plain": {"*": [unguarded]},
        },
    )
    catalog = InterviewCatalog(tmp_path, "docassemble.pkg:data/questions/main.yml")
    monkeypatch.setattr(catalog, "_compile_for_inspection", lambda: interview)
    before = set(tmp_path.rglob("*"))

    questions = catalog.questions().result["blocks"]
    indexed = catalog.index("shared").result["index"]["shared"]

    assert questions[0]["source_file"] == ("docassemble.pkg:data/questions/main.yml")
    assert questions[0]["location"].endswith(":1")
    assert questions[0]["id"] == "first"
    assert questions[0]["block_type"] == "fields"
    assert questions[0]["guards"] == ["user.kind == 'a'"]
    assert questions[1]["guards"] == ["user.kind == 'b'", "user.kind == 'c'"]
    assert questions[1]["guard"] == ["user.kind == 'b'", "user.kind == 'c'"]
    assert questions[2]["guard"] == "none"
    assert [entry["guards"] for entry in indexed] == [
        questions[0]["guards"],
        questions[1]["guards"],
    ]
    assert [entry["location"] for entry in indexed] == [
        questions[0]["location"],
        questions[1]["location"],
    ]
    assert "<code object" not in repr({"questions": questions, "index": indexed})
    assert set(tmp_path.rglob("*")) == before
    assert not (tmp_path / ".simulator" / "sessions").exists()

    # The CLI adapters expose the same public catalog result in JSON and human
    # form; neither output substitutes a compiled code-object representation.
    monkeypatch.setattr(
        "docassemble_simulator.catalog.InterviewCatalog",
        lambda root, selector: catalog,
    )
    json_args = cli.build_parser().parse_args(["questions", "--json"])
    assert cli.cmd_catalog(json_args, tmp_path) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["blocks"] == questions

    human_args = cli.build_parser().parse_args(["questions"])
    assert cli.cmd_catalog(human_args, tmp_path) == 0
    human = capsys.readouterr().out
    for value in ("user.kind == 'a'", "user.kind == 'b'", "guard: none"):
        assert value in human
    assert "<code object" not in human


def _reload_workspace(tmp_path):
    questions = tmp_path / "docassemble" / "pkg" / "data" / "questions"
    questions.mkdir(parents=True)
    (tmp_path / "docassemble" / "pkg" / "__init__.py").write_text("")
    source = questions / "main.yml"
    source.write_text("---\nquestion: First\nfields:\n  - Name: user_name\n")
    return source


def _install_interview_cache(monkeypatch, source, interview_factory=None):
    """A stand-in docassemble cache the reload seam can invalidate."""
    module = types.ModuleType("docassemble.base.interview_cache")
    module.cache = {}
    calls = []

    def get_interview(identity):
        calls.append(identity)
        interview = (
            interview_factory()
            if interview_factory is not None
            else SimpleNamespace(questions_list=[], questions={}, default_language="en")
        )
        module.cache[identity] = {
            "index": 1,
            "source": SimpleNamespace(
                filepath=str(source),
                get_modtime=lambda: source.stat().st_mtime,
            ),
            "interview": interview,
        }
        return interview

    module.get_interview = get_interview
    module.clear_cache = lambda identity: module.cache.pop(identity, None)
    monkeypatch.setitem(sys.modules, "docassemble.base.interview_cache", module)
    return calls


def test_compiled_cache_reuses_and_reloads_once_on_source_change(
    tmp_path, monkeypatch, da_stubs
):
    source = _reload_workspace(tmp_path)
    calls = _install_interview_cache(monkeypatch, source)
    catalog = InterviewCatalog(
        tmp_path, "docassemble.pkg:data/questions/main.yml", cache_compiled=True
    )

    first = catalog._compile()
    assert catalog._compile() is first
    assert len(calls) == 1

    source.write_text("---\nquestion: Second\nfields:\n  - Name: user_name\n")
    # Make the change explicit; the check must not depend on wall-clock
    # timestamp resolution to observe a rewrite.
    os.utime(source, (0, 0))

    second = catalog._compile()
    assert second is not first
    assert len(calls) == 2
    assert catalog._compile() is second
    assert len(calls) == 2


def test_compiled_cache_reloads_when_source_disappears(tmp_path, monkeypatch, da_stubs):
    source = _reload_workspace(tmp_path)
    calls = _install_interview_cache(monkeypatch, source)
    catalog = InterviewCatalog(
        tmp_path, "docassemble.pkg:data/questions/main.yml", cache_compiled=True
    )

    first = catalog._compile()
    source.unlink()

    # A missing source is treated as changed so recompilation can surface the
    # typed error in the worker instead of serving the stale program.
    second = catalog._compile()
    assert second is not first
    assert len(calls) == 2


def test_reload_evicts_declared_helper_modules(tmp_path, monkeypatch, da_stubs):
    source = _reload_workspace(tmp_path)

    def modules_interview():
        question = SimpleNamespace(
            question_type="modules",
            package="docassemble.pkg",
            module_list=["docassemble.pkg.helpers", ".relative"],
        )
        return SimpleNamespace(
            questions_list=[question], questions={}, default_language="en"
        )

    calls = _install_interview_cache(monkeypatch, source, modules_interview)
    for name in (
        "docassemble.pkg.helpers",
        "docassemble.pkg.helpers.sub",
        "docassemble.pkg.relative",
        "docassemble.other.helpers",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    invalidated = []
    monkeypatch.setattr(
        catalog_module.importlib, "invalidate_caches", lambda: invalidated.append(True)
    )
    catalog = InterviewCatalog(
        tmp_path, "docassemble.pkg:data/questions/main.yml", cache_compiled=True
    )

    catalog._compile()
    source.write_text("---\nquestion: Second\nfields:\n  - Name: user_name\n")
    os.utime(source, (0, 0))
    catalog._compile()

    assert len(calls) == 2
    assert "docassemble.pkg.helpers" not in sys.modules
    assert "docassemble.pkg.helpers.sub" not in sys.modules
    assert "docassemble.pkg.relative" not in sys.modules
    assert "docassemble.other.helpers" in sys.modules
    assert invalidated == [True]
