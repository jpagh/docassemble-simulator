"""Contract tests for attachment-aware DOCX rendering through InterviewRenderer."""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

from docassemble_simulator.execution import InterviewExecution, Start
from docassemble_simulator.render import (
    FreshSource,
    InterviewRenderer,
    RenderRequest,
    SavedSessionSource,
)


class _FakeFileInPackage:
    def __init__(self, ref: str, *, is_code: bool = False):
        self._ref = ref
        self.is_code = is_code

    def original_reference(self) -> str:
        return self._ref


class _FakeDAObject:
    """Mimics a DAObject populated in code blocks (empty attrList, strict __getattr__)."""

    def __init__(self, instance_name: str, **attrs):
        self.instanceName = instance_name
        self.attrList: list[str] = []
        for key, value in attrs.items():
            setattr(self, key, value)

    def __getattr__(self, name: str):
        raise AttributeError(
            f"strict undefined attribute {name!r} on {self.instanceName}"
        )


def _make_docx(path: Path, paragraphs: int) -> None:
    body = "".join(
        f"<w:p><w:r><w:t>Paragraph {index}</w:t></w:r></w:p>"
        for index in range(paragraphs)
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body>"
        "</w:document>"
    )
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


def _docx_file(path: Path, filename: str = "Rendered.docx"):
    return SimpleNamespace(filename=filename, path=lambda: str(path))


def _attachment_spec(
    variable_name: str,
    *,
    question_name: str = "q_attach",
    indexno: int = 0,
    docx_templates: tuple[object, ...] | None = None,
    extra_options: dict | None = None,
    extra_fields: dict | None = None,
) -> dict:
    options: dict = {}
    if docx_templates is None:
        options["docx_template_file"] = [_FakeFileInPackage("form.docx")]
    elif docx_templates:
        options["docx_template_file"] = list(docx_templates)
    if extra_options:
        options.update(extra_options)
    spec: dict = {
        "variable_name": variable_name,
        "question_name": question_name,
        "indexno": indexno,
        "options": options,
    }
    if extra_fields:
        spec.update(extra_fields)
    return spec


class _FakeQuestion:
    def __init__(
        self,
        attachments: list[dict],
        *,
        name: str = "q_attach",
        is_generic: bool = False,
        docx_path: Path | None = None,
        filename: str = "Rendered.docx",
        on_prepare=None,
        on_finalize=None,
    ):
        self.name = name
        self.is_generic = is_generic
        self.attachments = attachments
        self._docx_path = docx_path
        self._filename = filename
        self._on_prepare = on_prepare
        self._on_finalize = on_finalize
        self.prepare_calls: list[dict] = []

    def prepare_attachment(self, attachment, user_dict):
        self.prepare_calls.append(
            {
                "variable_name": attachment["variable_name"],
                "x": user_dict.get("x"),
                "i": user_dict.get("i"),
                "j": user_dict.get("j"),
                "k": user_dict.get("k"),
            }
        )
        if self._on_prepare is not None:
            return self._on_prepare(attachment, user_dict)
        return {"field_data": {}, "formats_to_use": ["docx"]}

    def finalize_attachment(self, attachment, prepared, user_dict):
        if self._on_finalize is not None:
            return self._on_finalize(attachment, prepared, user_dict)
        assert self._docx_path is not None
        collection = SimpleNamespace(
            instanceName="assembled",
            docx=_docx_file(self._docx_path, self._filename),
        )
        canonical = _resolve_fake_canonical(
            attachment["variable_name"], user_dict, self.is_generic
        )
        _assign_target(user_dict, canonical, collection)
        return {
            "formats_to_use": ["docx"],
            "file": {"docx": 1},
            "filename": Path(self._filename).stem,
        }


def _resolve_fake_canonical(template: str, user_dict: dict, is_generic: bool) -> str:
    var = template
    if is_generic and "x" in user_dict and user_dict["x"] is not None:
        instance_name = vars(user_dict["x"]).get("instanceName")
        if isinstance(instance_name, str) and instance_name:
            var = instance_name + var[1:]
    for idx in ("i", "j", "k", "l", "m", "n"):
        if idx in user_dict:
            var = var.replace(f"[{idx}]", f"[{user_dict[idx]!r}]")
    return var


def _resolve_node(node: ast.AST, user_dict: dict):
    if isinstance(node, ast.Name):
        return user_dict[node.id]
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Attribute):
        return getattr(_resolve_node(node.value, user_dict), node.attr)
    if isinstance(node, ast.Subscript):
        return _resolve_node(node.value, user_dict)[
            _resolve_node(node.slice, user_dict)
        ]
    raise AssertionError(f"unsupported expression node: {ast.dump(node)}")


def _assign_target(user_dict: dict, expression: str, value) -> None:
    node = ast.parse(expression, mode="eval").body
    if isinstance(node, ast.Name):
        user_dict[node.id] = value
        return
    if isinstance(node, ast.Attribute):
        setattr(_resolve_node(node.value, user_dict), node.attr, value)
        return
    if isinstance(node, ast.Subscript):
        _resolve_node(node.value, user_dict)[_resolve_node(node.slice, user_dict)] = (
            value
        )
        return
    raise AssertionError(f"unsupported target expression: {expression!r}")


def _setup_workspace(
    tmp_path: Path,
    monkeypatch,
    questions: list[_FakeQuestion],
    *,
    populate=None,
    template_files: tuple[str, ...] = ("form.docx",),
):
    root = tmp_path / "workspace"
    q_dir = root / "docassemble" / "pkg" / "data" / "questions"
    t_dir = root / "docassemble" / "pkg" / "data" / "templates"
    q_dir.mkdir(parents=True, exist_ok=True)
    t_dir.mkdir(parents=True, exist_ok=True)
    (q_dir / "main.yml").write_text("---\nquestion: x\n")
    for rel in template_files:
        path = t_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"docx")

    parse = sys.modules["docassemble.base.parse"]
    parse.get_initial_dict = lambda: {
        "_internal": {"tracker": 0},
        "nav": SimpleNamespace(instanceName="nav", sections=None),
        "url_args": {},
    }
    util = types.ModuleType("docassemble.base.util")
    util.DAObject = SimpleNamespace
    monkeypatch.setitem(sys.modules, "docassemble.base.util", util)

    class FakeInterview:
        source = SimpleNamespace(path="main.yml", package="docassemble.pkg")
        all_questions = tuple(questions)

        def populate_non_pickleable(self, namespace):
            if populate is not None:
                populate(namespace)

        def assemble(self, namespace, interview_status):
            if populate is not None:
                populate(namespace)

    cache = types.ModuleType("docassemble.base.interview_cache")
    cache.get_interview = lambda identity: FakeInterview()
    monkeypatch.setitem(sys.modules, "docassemble.base.interview_cache", cache)

    execution = InterviewExecution(root, "docassemble.pkg:data/questions/main.yml")
    renderer = InterviewRenderer(root, execution)
    return root, execution, renderer


class TestAttachmentMatchingAndResolution:
    def test_matches_generic_root_quoted_keys_and_multiple_indices(
        self, tmp_path, monkeypatch, da_stubs
    ):
        docx = tmp_path / "out.docx"
        _make_docx(docx, paragraphs=3)
        q = _FakeQuestion(
            [_attachment_spec("x.original[i].items[j].petition")],
            is_generic=True,
            docx_path=docx,
        )

        def populate(ns):
            item_2 = SimpleNamespace()
            orig_0 = SimpleNamespace(items=[None, None, item_2])
            stage = _FakeDAObject(
                "documents['dor_variance_petition']", original=[orig_0]
            )
            ns["documents"] = {"dor_variance_petition": stage}

        _, _, renderer = _setup_workspace(tmp_path, monkeypatch, [q], populate=populate)
        outcome = renderer.render(
            RenderRequest(
                None,
                FreshSource(),
                False,
                attachment="documents['dor_variance_petition'].original[0].items[2].petition",
            )
        )

        assert outcome.ok is True
        assert (
            outcome.result.attachment
            == "documents['dor_variance_petition'].original[0].items[2].petition"
        )
        assert outcome.result.paragraphs == 3
        assert q.prepare_calls[-1]["i"] == 0
        assert q.prepare_calls[-1]["j"] == 2

    def test_repeated_index_requires_equal_values(
        self, tmp_path, monkeypatch, da_stubs
    ):
        docx = tmp_path / "out.docx"
        _make_docx(docx, paragraphs=1)
        q = _FakeQuestion(
            [_attachment_spec("docs[k].items[i].copy[k]")],
            is_generic=False,
            docx_path=docx,
        )

        def populate(ns):
            ns["docs"] = [
                None,
                SimpleNamespace(items=[SimpleNamespace(copy={0: None, 1: None})]),
            ]

        _, _, renderer = _setup_workspace(tmp_path, monkeypatch, [q], populate=populate)

        matched = renderer.render(
            RenderRequest(
                None, FreshSource(), False, attachment="docs[1].items[0].copy[1]"
            )
        )
        assert matched.ok is True
        assert q.prepare_calls[-1]["k"] == 1
        assert q.prepare_calls[-1]["i"] == 0

        mismatched = renderer.render(
            RenderRequest(
                None, FreshSource(), False, attachment="docs[1].items[0].copy[2]"
            )
        )
        assert mismatched.ok is False
        assert mismatched.error.kind == "input"
        assert "no attachment block declares" in mismatched.error.message

    def test_leading_x_is_literal_when_not_generic_and_wins_over_generic(
        self, tmp_path, monkeypatch, da_stubs
    ):
        docx = tmp_path / "out.docx"
        _make_docx(docx, paragraphs=1)
        q_non_generic = _FakeQuestion(
            [_attachment_spec("x.complaint", question_name="q_lit")],
            name="q_lit",
            is_generic=False,
            docx_path=docx,
        )
        q_generic = _FakeQuestion(
            [_attachment_spec("x.complaint", question_name="q_gen")],
            name="q_gen",
            is_generic=True,
            docx_path=docx,
        )

        def populate(ns):
            ns["x"] = _FakeDAObject("x")
            ns["clients"] = [SimpleNamespace()]

        _, _, renderer = _setup_workspace(
            tmp_path, monkeypatch, [q_generic, q_non_generic], populate=populate
        )

        # Literal non-generic x.complaint succeeds without hitting generic rejection
        literal = renderer.render(
            RenderRequest(None, FreshSource(), False, attachment="x.complaint")
        )
        assert literal.ok is True
        assert literal.result.attachment == "x.complaint"
        assert len(q_non_generic.prepare_calls) == 1
        assert q_generic.prepare_calls == []

        # Non-generic alone does not match arbitrary roots
        _, _, lit_only = _setup_workspace(
            tmp_path, monkeypatch, [q_non_generic], populate=populate
        )
        other = lit_only.render(
            RenderRequest(None, FreshSource(), False, attachment="clients[0].complaint")
        )
        assert other.ok is False
        assert other.error.kind == "input"
        assert "no attachment block declares" in other.error.message

    def test_rejects_generic_x_operands_with_domain_neutral_hint_and_candidates(
        self, tmp_path, monkeypatch, da_stubs
    ):
        docx = tmp_path / "out.docx"
        _make_docx(docx, paragraphs=2)
        q = _FakeQuestion(
            [
                _attachment_spec(
                    "x.original[0].petition", question_name="q_pet", indexno=0
                ),
                _attachment_spec(
                    "x.original[i].indexed_doc", question_name="q_pet", indexno=1
                ),
            ],
            name="q_pet",
            is_generic=True,
            docx_path=docx,
        )

        def populate_empty(ns):
            ns["x_doc"] = _FakeDAObject("x_doc", original=[SimpleNamespace()])
            ns["documents"] = {
                "x": _FakeDAObject("documents['x']", original=[SimpleNamespace()])
            }

        _, _, renderer = _setup_workspace(
            tmp_path, monkeypatch, [q], populate=populate_empty
        )

        for bad_var, expected_example in (
            ("x.original[0].petition", "<object>.original[0].petition"),
            ("x.sub.original[0].petition", "<object>.original[0].petition"),
            ("x['stage'].original[0].petition", "<object>.original[0].petition"),
            ("(x).original[0].petition", "<object>.original[0].petition"),
            ("x.original[i].indexed_doc", "<object>.original[0].indexed_doc"),
        ):
            outcome = renderer.render(
                RenderRequest(None, FreshSource(), False, attachment=bad_var)
            )
            assert outcome.ok is False
            assert outcome.error.kind == "input"
            assert "generic placeholder 'x'" in outcome.error.message
            assert expected_example in outcome.error.message
            assert "documents['<stage>']" not in outcome.error.message
            assert outcome.error.details == {"attachment": bad_var}

        # Concrete roots that start with or contain 'x' still match and render
        for good_var in (
            "x_doc.original[0].petition",
            "documents['x'].original[0].petition",
        ):
            outcome = renderer.render(
                RenderRequest(None, FreshSource(), False, attachment=good_var)
            )
            assert outcome.ok is True, (good_var, outcome)
            assert outcome.result.attachment == good_var

        # When namespace holds an assembled candidate, generic x error lists it
        def populate_candidate(ns):
            ns["doc"] = _FakeDAObject(
                "documents['stage_a'].original[0].petition",
                info={"attachment": {"name": "q_pet", "number": 0}},
            )

        _, _, cand_renderer = _setup_workspace(
            tmp_path, monkeypatch, [q], populate=populate_candidate
        )
        with_cand = cand_renderer.render(
            RenderRequest(
                None, FreshSource(), False, attachment="x.original[0].petition"
            )
        )
        assert with_cand.ok is False
        assert with_cand.error.kind == "input"
        assert "documents['stage_a'].original[0].petition" in with_cand.error.message
        assert with_cand.error.details["candidates"] == [
            "documents['stage_a'].original[0].petition"
        ]

    def test_rejects_invalid_expression_and_reports_zero_and_ambiguous_matches(
        self, tmp_path, monkeypatch, da_stubs
    ):
        q_sim = _FakeQuestion(
            [
                _attachment_spec(
                    "x.original[0].petition", question_name="q_sim", indexno=0
                ),
                _attachment_spec(
                    "x.redacted[0].petition", question_name="q_sim", indexno=1
                ),
            ],
            name="q_sim",
            is_generic=True,
        )
        _, _, sim_renderer = _setup_workspace(tmp_path, monkeypatch, [q_sim])

        bad_expr = sim_renderer.render(
            RenderRequest(
                None,
                FreshSource(),
                False,
                attachment="documents['unclosed.original[0].petition",
            )
        )
        assert bad_expr.ok is False
        assert bad_expr.error.kind == "input"
        assert "no attachment block declares" in bad_expr.error.message

        no_match = sim_renderer.render(
            RenderRequest(
                None,
                FreshSource(),
                False,
                attachment="documents['a'].draft[0].petition",
            )
        )
        assert no_match.ok is False
        assert no_match.error.kind == "input"
        assert no_match.error.details["candidates"] == [
            "x.original[0].petition",
            "x.redacted[0].petition",
        ]

        # Duplicate variable_name across distinct questions keeps bare candidates
        # while exposing structured entries and qualified labels in the message.
        dup1 = _FakeQuestion(
            [_attachment_spec("x.petition", question_name="q1", indexno=0)],
            name="q1",
            is_generic=True,
        )
        dup2 = _FakeQuestion(
            [_attachment_spec("x.petition", question_name="q2", indexno=0)],
            name="q2",
            is_generic=True,
        )
        _, _, dup_renderer = _setup_workspace(tmp_path, monkeypatch, [dup1, dup2])
        dup_outcome = dup_renderer.render(
            RenderRequest(
                None, FreshSource(), False, attachment="documents['a'].petition"
            )
        )
        assert dup_outcome.ok is False
        assert dup_outcome.error.kind == "input"
        assert dup_outcome.error.details["candidates"] == ["x.petition"]
        assert dup_outcome.error.details["entries"] == [
            {"variable_name": "x.petition", "question": "q1", "indexno": 0},
            {"variable_name": "x.petition", "question": "q2", "indexno": 0},
        ]
        assert "x.petition (q1[0])" in dup_outcome.error.message
        assert "x.petition (q2[0])" in dup_outcome.error.message

        # Distinct templates that both match list both templates cleanly
        broad1 = _FakeQuestion(
            [_attachment_spec("x.petition", question_name="q_broad", indexno=0)],
            name="q_broad",
            is_generic=True,
        )
        broad2 = _FakeQuestion(
            [_attachment_spec("x.sub.petition", question_name="q_narrow", indexno=0)],
            name="q_narrow",
            is_generic=True,
        )
        _, _, broad_renderer = _setup_workspace(tmp_path, monkeypatch, [broad1, broad2])
        broad_outcome = broad_renderer.render(
            RenderRequest(
                None, FreshSource(), False, attachment="documents['a'].sub.petition"
            )
        )
        assert broad_outcome.ok is False
        assert broad_outcome.error.kind == "input"
        assert broad_outcome.error.details["candidates"] == [
            "x.petition",
            "x.sub.petition",
        ]


class TestAutomaticResolutionAndCandidateDiscovery:
    def test_matches_nested_and_skips_code_based_templates(
        self, tmp_path, monkeypatch, da_stubs
    ):
        docx = tmp_path / "out.docx"
        _make_docx(docx, paragraphs=2)
        q = _FakeQuestion(
            [
                _attachment_spec(
                    "nested_doc",
                    question_name="q_t",
                    indexno=0,
                    docx_templates=(
                        _FakeFileInPackage(
                            "docassemble.pkg:data/templates/sub/form.docx"
                        ),
                    ),
                ),
                _attachment_spec(
                    "dynamic_doc",
                    question_name="q_t",
                    indexno=1,
                    docx_templates=(
                        _FakeFileInPackage("indicated by dynamic_tpl", is_code=True),
                    ),
                ),
                _attachment_spec(
                    "",
                    question_name="q_t",
                    indexno=2,
                    docx_templates=(_FakeFileInPackage("form.docx"),),
                ),
            ],
            name="q_t",
            docx_path=docx,
        )

        _, execution, renderer = _setup_workspace(
            tmp_path,
            monkeypatch,
            [q],
            template_files=("sub/form.docx", "form.docx", "dynamic.docx"),
        )
        assert execution.run(Start()).ok is True

        # Nested template auto-resolves to concrete entry
        nested = renderer.render(
            RenderRequest("sub/form.docx", SavedSessionSource(), assemble=False)
        )
        assert nested.ok is True
        assert nested.result.attachment == "nested_doc"

        # Bare form.docx does not match sub/form.docx (nor nameless entry) -> direct path
        monkeypatch.setattr(
            "docassemble_simulator.render.prepare_docx_template",
            lambda path: SimpleNamespace(_dasimulator_paragraphs=4),
        )
        monkeypatch.setattr(
            "docassemble_simulator.render.render_template",
            lambda prepared, namespace, **kwargs: prepared,
        )
        direct = renderer.render(
            RenderRequest("form.docx", SavedSessionSource(), assemble=False)
        )
        assert direct.ok is True
        assert direct.result.attachment is None

        # Code-based template is skipped by auto-resolution, still works via --attachment
        explicit_dynamic = renderer.render(
            RenderRequest(
                None, SavedSessionSource(), assemble=False, attachment="dynamic_doc"
            )
        )
        assert explicit_dynamic.ok is True
        assert explicit_dynamic.result.attachment == "dynamic_doc"

    def test_discovers_candidates_through_elements_vars_cycles_and_decision_table(
        self, tmp_path, monkeypatch, da_stubs
    ):
        docx = tmp_path / "out.docx"
        _make_docx(docx, paragraphs=4)
        q = _FakeQuestion(
            [
                _attachment_spec(
                    "x.original[0].petition",
                    question_name="q_pet",
                    indexno=0,
                    docx_templates=(_FakeFileInPackage("petition.docx"),),
                ),
                _attachment_spec(
                    "x.redacted[0].petition",
                    question_name="q_pet",
                    indexno=1,
                    docx_templates=(_FakeFileInPackage("petition.docx"),),
                ),
            ],
            name="q_pet",
            is_generic=True,
            docx_path=docx,
        )

        # 0 candidates on generic entry -> input error suggesting --attachment
        _, execution, renderer = _setup_workspace(
            tmp_path,
            monkeypatch,
            [q],
            template_files=("petition.docx", "partial.docx"),
        )
        assert execution.run(Start()).ok is True
        no_cand = renderer.render(
            RenderRequest("petition.docx", SavedSessionSource(), assemble=False)
        )
        assert no_cand.ok is False
        assert no_cand.error.kind == "input"
        assert "--attachment" in no_cand.error.message

        # 1 candidate discovered through elements (dict+list), vars(), empty attrList, cycle
        def populate_one(ns):
            col_orig = _FakeDAObject(
                "documents['stage'].original[0].petition",
                info={"attachment": {"name": "q_pet", "number": 0}},
            )
            orig_item = _FakeDAObject(
                "documents['stage'].original[0]", petition=col_orig
            )
            orig_list = _FakeDAObject(
                "documents['stage'].original", elements=[orig_item]
            )
            stage = _FakeDAObject("documents['stage']", original=orig_list)
            stage.self_ref = stage
            documents = _FakeDAObject("documents", elements={"stage": stage})
            ns["_internal"] = {"ignored": col_orig}
            ns["documents"] = documents
            ns["duplicate_ref"] = col_orig

        def populate_two(ns):
            populate_one(ns)
            col_red = _FakeDAObject(
                "documents['stage'].redacted[0].petition",
                info={"attachment": {"name": "q_pet", "number": 1}},
            )
            red_item = _FakeDAObject("documents['stage'].redacted[0]", petition=col_red)
            ns["documents"].elements["stage"].redacted = _FakeDAObject(
                "documents['stage'].redacted", elements=[red_item]
            )

        # Override _assign_target helper path via a custom finalize for DAObject elements
        def custom_finalize(attachment, prepared, user_dict):
            stage = user_dict["documents"].elements["stage"]
            stage.original.elements[0].petition = SimpleNamespace(
                instanceName="documents['stage'].original[0].petition",
                docx=_docx_file(docx, "Petition_Original.docx"),
            )
            return {
                "formats_to_use": ["docx"],
                "file": {"docx": 1},
                "filename": "Petition_Original",
            }

        q_one = _FakeQuestion(
            q.attachments,
            name="q_pet",
            is_generic=True,
            docx_path=docx,
            on_finalize=custom_finalize,
        )

        # Monkeypatch DAObject __getitem__ via a dict-backed subclass for eval()
        class _EvalDict(_FakeDAObject):
            def __getitem__(self, key):
                return self.elements[key]

        def populate_one_eval(ns):
            populate_one(ns)
            stage = ns["documents"].elements["stage"]
            orig_list = _EvalDict(
                "documents['stage'].original", elements=stage.original.elements
            )
            stage.original = orig_list
            ns["documents"] = _EvalDict("documents", elements={"stage": stage})

        _, exec_one, rend_one = _setup_workspace(
            tmp_path / "one",
            monkeypatch,
            [q_one],
            populate=populate_one_eval,
            template_files=("petition.docx",),
        )
        assert exec_one.run(Start()).ok is True
        auto_one = rend_one.render(
            RenderRequest("petition.docx", SavedSessionSource(), assemble=False)
        )
        assert auto_one.ok is True, auto_one
        assert auto_one.result.attachment == "documents['stage'].original[0].petition"
        assert auto_one.result.paragraphs == 4
        assert len(auto_one.attachments) == 1
        assert auto_one.attachments[0].filename == "Petition_Original.docx"
        assert auto_one.attachments[0].structure == "ok"

        # 2 candidates -> ambiguity input error listing both sorted
        _, exec_two, rend_two = _setup_workspace(
            tmp_path / "two",
            monkeypatch,
            [q],
            populate=populate_two,
            template_files=("petition.docx",),
        )
        assert exec_two.run(Start()).ok is True
        auto_two = rend_two.render(
            RenderRequest("petition.docx", SavedSessionSource(), assemble=False)
        )
        assert auto_two.ok is False
        assert auto_two.error.kind == "input"
        assert "2 attachment variables" in auto_two.error.message
        assert auto_two.error.details["candidates"] == [
            "documents['stage'].original[0].petition",
            "documents['stage'].redacted[0].petition",
        ]


class TestValidationAndAliases:
    def test_rejects_manual_pdf_form_raw_and_pdf_only_before_assembly(
        self, tmp_path, monkeypatch, da_stubs
    ):
        q = _FakeQuestion(
            [
                _attachment_spec(
                    "manual_doc", indexno=0, extra_options={"manual": True}
                ),
                _attachment_spec(
                    "pdf_form_doc",
                    indexno=1,
                    docx_templates=(),
                    extra_options={"pdf_template_file": "form.pdf"},
                ),
                _attachment_spec(
                    "raw_doc", indexno=2, docx_templates=(), extra_fields={"raw": True}
                ),
                _attachment_spec(
                    "pdf_only_doc",
                    indexno=3,
                    docx_templates=(),
                    extra_fields={"valid_formats": ["pdf"]},
                ),
            ]
        )
        _, _, renderer = _setup_workspace(tmp_path, monkeypatch, [q])

        for var, expected_kind, snippet in (
            ("manual_doc", "input", "manual"),
            ("pdf_form_doc", "input", "pdf template"),
            ("raw_doc", "input", "raw attachment"),
            ("pdf_only_doc", "pdf-unavailable", "PDF"),
        ):
            outcome = renderer.render(
                RenderRequest(None, FreshSource(), False, attachment=var)
            )
            assert outcome.ok is False
            assert outcome.error.kind == expected_kind
            assert snippet in outcome.error.message
        assert q.prepare_calls == []

    def test_expect_missing_resolves_aliased_and_indexed_bindings(
        self, tmp_path, monkeypatch, da_stubs
    ):
        def on_prepare(attachment, user_dict):
            return {"field_data": {"S": user_dict["x"]}, "formats_to_use": ["docx"]}

        def on_finalize(attachment, prepared, user_dict):
            err = RuntimeError("'documents['petition'].items[1].flag' is undefined")
            err.filename = "form.docx"
            raise err

        q = _FakeQuestion(
            [_attachment_spec("x.original[i].doc")],
            is_generic=True,
            on_prepare=on_prepare,
            on_finalize=on_finalize,
        )

        def populate(ns):
            ns["documents"] = {"petition": _FakeDAObject("documents['petition']")}

        _, _, renderer = _setup_workspace(tmp_path, monkeypatch, [q], populate=populate)

        for expected in (
            "S.items[i].flag",
            "documents['petition'].items[1].flag",
        ):
            outcome = renderer.render(
                RenderRequest(
                    None,
                    FreshSource(),
                    False,
                    expect_missing=expected,
                    attachment="documents['petition'].original[1].doc",
                )
            )
            assert outcome.ok is True, (expected, outcome)
            assert outcome.result.paragraphs == 0
            assert outcome.result.attachment == "documents['petition'].original[1].doc"

        mismatched = renderer.render(
            RenderRequest(
                None,
                FreshSource(),
                False,
                expect_missing="S.items[i].other",
                attachment="documents['petition'].original[1].doc",
            )
        )
        assert mismatched.ok is False
        assert mismatched.error.kind == "render"
        assert mismatched.error.details["resolved_expected_missing"] == (
            "documents['petition'].items[1].other"
        )

    def test_applies_bindings_attributes_nested_subdoc_and_handles_unreadable_docx(
        self, tmp_path, monkeypatch, da_stubs
    ):
        observed_bindings: dict[str, object] = {}
        invalid_docx = tmp_path / "not-a-zip.docx"
        invalid_docx.write_bytes(b"not a zip archive")

        def on_prepare(attachment, user_dict):
            observed_bindings["custom_note"] = user_dict.get("custom_note")
            return {
                "field_data": {"note": user_dict.get("custom_note")},
                "formats_to_use": ["docx"],
            }

        def populate(ns):
            ns["seed_value"] = "from-bind"
            stage = _FakeDAObject("documents['petition']", original=[SimpleNamespace()])
            ns["documents"] = {"petition": stage}

        # --bind runs before prepare_attachment; double-quoted spelling normalizes
        # to canonical instanceName; unreadable DOCX yields 0 paragraphs.
        q_ok = _FakeQuestion(
            [_attachment_spec("x.original[0].doc")],
            is_generic=True,
            docx_path=invalid_docx,
            on_prepare=on_prepare,
        )
        root, _, renderer = _setup_workspace(
            tmp_path,
            monkeypatch,
            [q_ok],
            populate=populate,
            template_files=("form.docx", "nested/subdoc.docx"),
        )
        (root / "docassemble" / "pkg" / "data" / "outside.docx").write_bytes(b"outside")

        rendered = renderer.render(
            RenderRequest(
                None,
                FreshSource(),
                False,
                bindings=(("custom_note", "seed_value.upper()"),),
                attachment='documents["petition"].original[0].doc',
            )
        )
        assert rendered.ok is True
        assert observed_bindings == {"custom_note": "FROM-BIND"}
        assert rendered.result.attachment == "documents['petition'].original[0].doc"
        assert rendered.result.paragraphs == 0

        # Nested data/templates/nested/subdoc.docx is attributed from bare basename,
        # while relative path traversal outside data/templates falls back to parent.
        for fail_filename, expected_template in (
            ("subdoc.docx, form.docx", "subdoc.docx"),
            ("../outside.docx", "form.docx"),
        ):

            def on_finalize(attachment, prepared, user_dict, fname=fail_filename):
                err = RuntimeError("'missing_in_subdoc' is undefined")
                err.filename = fname
                raise err

            q_fail = _FakeQuestion(
                [_attachment_spec("x.original[0].doc")],
                is_generic=True,
                on_finalize=on_finalize,
            )
            _, _, fail_renderer = _setup_workspace(
                tmp_path,
                monkeypatch,
                [q_fail],
                populate=populate,
                template_files=("form.docx", "nested/subdoc.docx"),
            )
            err_outcome = fail_renderer.render(
                RenderRequest(
                    None,
                    FreshSource(),
                    False,
                    attachment="documents['petition'].original[0].doc",
                )
            )
            assert err_outcome.ok is False
            assert err_outcome.error.kind == "render"
            assert err_outcome.error.details["template"] == expected_template
