"""Unit tests for attachment-aware DOCX rendering discovery, matching, and helpers."""

from __future__ import annotations

from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from docassemble_simulator._artifacts import (
    capture_published_attachments,
    docx_paragraph_count,
    publish_attachment,
)
from docassemble_simulator._attachments import (
    AttachmentEntry,
    AttachmentInputError,
    AttachmentTarget,
    assemble_attachment,
    canonical_variable_name,
    compile_matcher,
    discover_candidates,
    enumerate_entries,
    match_template_entries,
    match_variable,
    resolve_automatic_target,
    resolve_expect_missing_alias,
    resolve_explicit_target,
    resolve_targets,
)
from docassemble_simulator._runtime import PDFConversionUnavailable


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


def _make_docx(path, paragraphs: int) -> None:
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


def _entry(
    variable_name: str,
    *,
    is_generic: bool = False,
    question_name: str = "q_attach",
    indexno: int = 0,
    docx_templates: tuple[str, ...] = ("form.docx",),
    has_docx_template: bool = True,
    is_manual: bool = False,
    is_pdf_template: bool = False,
    is_raw: bool = False,
    question: object = None,
    attachment: dict | None = None,
) -> AttachmentEntry:
    return AttachmentEntry(
        question=question or SimpleNamespace(name=question_name, is_generic=is_generic),
        attachment=attachment or {"variable_name": variable_name, "indexno": indexno},
        question_name=question_name,
        indexno=indexno,
        variable_name=variable_name,
        is_generic=is_generic,
        docx_templates=docx_templates,
        has_docx_template=has_docx_template,
        is_manual=is_manual,
        is_pdf_template=is_pdf_template,
        is_raw=is_raw,
    )


class TestEnumerateEntries:
    def test_enumerates_static_and_excludes_code_based_template_names(self):
        static_file = _FakeFileInPackage("sub/petition.docx")
        code_file = _FakeFileInPackage("indicated by dynamic_tpl", is_code=True)
        q1 = SimpleNamespace(
            name="q_petition",
            is_generic=True,
            attachments=[
                {
                    "variable_name": "x.original[i].petition",
                    "question_name": "q_petition",
                    "indexno": 0,
                    "options": {"docx_template_file": [static_file]},
                },
                {
                    "variable_name": "x.dynamic[0].petition",
                    "question_name": "q_petition",
                    "indexno": 1,
                    "options": {"docx_template_file": [code_file]},
                },
                {
                    "variable_name": "",
                    "question_name": "q_petition",
                    "indexno": 2,
                    "options": {"docx_template_file": [static_file]},
                },
            ],
        )
        interview = SimpleNamespace(all_questions=[q1])

        entries = enumerate_entries(interview)

        assert len(entries) == 2
        assert entries[0].variable_name == "x.original[i].petition"
        assert entries[0].is_generic is True
        assert entries[0].docx_templates == ("sub/petition.docx",)
        assert entries[0].has_docx_template is True
        assert entries[1].variable_name == "x.dynamic[0].petition"
        assert entries[1].docx_templates == ()
        assert entries[1].has_docx_template is True


class TestMatcher:
    def test_matches_generic_root_and_multiple_indices_with_quoted_keys(self):
        entry = _entry("x.original[i].items[j].petition", is_generic=True)

        target = match_variable(
            entry, "documents['dor_variance_petition'].original[0].items[2].petition"
        )

        assert target is not None
        assert target.x_expression == "documents['dor_variance_petition']"
        assert target.index_values == (("i", 0), ("j", 2))

    def test_repeated_index_requires_equal_values(self):
        entry = _entry("docs[i].copy[i]")

        assert match_variable(entry, "docs[1].copy[1]") is not None
        assert match_variable(entry, "docs[1].copy[2]") is None

    def test_leading_x_is_literal_when_question_is_not_generic(self):
        entry = _entry("x.complaint", is_generic=False)

        assert match_variable(entry, "x.complaint") is not None
        assert match_variable(entry, "clients[0].complaint") is None

    def test_rejects_invalid_python_expression_for_generic_root(self):
        entry = _entry("x.petition", is_generic=True)

        assert match_variable(entry, "documents['unclosed.petition") is None

    def test_compile_matcher_reports_index_order(self):
        pattern, indices = compile_matcher(_entry("x.a[k].b[i].c[k]", is_generic=True))

        assert indices == ("k", "i")
        assert pattern.fullmatch("root.a[3].b[0].c[3]") is not None

    def test_explicit_resolution_reports_zero_and_ambiguous_matches(self):
        e1 = _entry("x.original[0].petition", is_generic=True)
        e2 = _entry("x.redacted[0].petition", is_generic=True)
        e3 = _entry("y.petition", is_generic=True)

        with pytest.raises(
            AttachmentInputError, match="no attachment block declares"
        ) as exc:
            resolve_explicit_target([e1, e2], "documents['a'].draft[0].petition")
        assert exc.value.details["candidates"] == [
            "x.original[0].petition",
            "x.redacted[0].petition",
        ]

        dup1 = _entry("x.petition", is_generic=True, question_name="q1")
        dup2 = _entry("x.petition", is_generic=True, question_name="q2")
        with pytest.raises(AttachmentInputError, match="matches 2 attachment entries"):
            resolve_explicit_target([dup1, dup2, e3], "documents['a'].petition")

        assert (
            len(resolve_targets([e1, e2], "documents['a'].original[0].petition")) == 1
        )


class TestTemplateMatching:
    def test_matches_basename_nested_path_and_package_qualified_ref(self):
        e1 = _entry(
            "a", docx_templates=("docassemble.pkg:data/templates/sub/form.docx",)
        )
        e2 = _entry("b", docx_templates=("other.docx",))
        e3 = _entry("c", docx_templates=())  # code-based template excluded

        assert match_template_entries([e1, e2, e3], "sub/form.docx") == (e1,)
        assert match_template_entries([e1, e2, e3], "form.docx") == ()
        assert match_template_entries([e1, e2, e3], "other.docx") == (e2,)


class TestCandidateDiscovery:
    def test_walks_elements_and_vars_with_empty_attrlist_and_cycles(self):
        entry_orig = _entry(
            "x.original[0].petition", is_generic=True, question_name="q_pet", indexno=0
        )
        entry_red = _entry(
            "x.redacted[0].petition", is_generic=True, question_name="q_pet", indexno=1
        )

        col_orig = _FakeDAObject(
            "documents['stage'].original[0].petition",
            info={"attachment": {"name": "q_pet", "number": 0}},
        )
        col_red = _FakeDAObject(
            "documents['stage'].redacted[0].petition",
            info={"attachment": {"name": "q_pet", "number": 1}},
        )
        # Nested in elements (dict + list) and vars(), plus a cycle back to parent
        orig_item = _FakeDAObject("documents['stage'].original[0]", petition=col_orig)
        red_item = _FakeDAObject("documents['stage'].redacted[0]", petition=col_red)
        orig_list = _FakeDAObject("documents['stage'].original", elements=[orig_item])
        red_list = _FakeDAObject("documents['stage'].redacted", elements=[red_item])
        stage = _FakeDAObject(
            "documents['stage']", original=orig_list, redacted=red_list
        )
        stage.self_ref = stage  # cycle
        documents = _FakeDAObject("documents", elements={"stage": stage})

        namespace = {
            "_internal": {"ignored": col_orig},
            "documents": documents,
            "duplicate_ref": col_orig,
        }

        candidates = discover_candidates(namespace, [entry_orig, entry_red])

        assert candidates == [
            ("documents['stage'].original[0].petition", entry_orig),
            ("documents['stage'].redacted[0].petition", entry_red),
        ]

    def test_automatic_resolution_decision_table(self):
        concrete = _entry("standalone", docx_templates=("standalone.docx",))
        generic = _entry(
            "x.original[0].petition",
            is_generic=True,
            question_name="q_pet",
            indexno=0,
            docx_templates=("petition.docx",),
        )
        generic_red = _entry(
            "x.redacted[0].petition",
            is_generic=True,
            question_name="q_pet",
            indexno=1,
            docx_templates=("petition.docx",),
        )

        # Unmatched template falls back to direct render (None)
        assert resolve_automatic_target([concrete, generic], {}, "partial.docx") is None

        # 0 candidates + 1 concrete entry -> renders concrete entry
        target = resolve_automatic_target([concrete], {}, "standalone.docx")
        assert target == AttachmentTarget(entry=concrete, variable="standalone")

        # 0 candidates + generic entry -> input error suggesting --attachment
        with pytest.raises(AttachmentInputError, match="--attachment"):
            resolve_automatic_target([generic], {}, "petition.docx")

        # 1 candidate -> auto-resolves
        col_orig = _FakeDAObject(
            "documents['stage'].original[0].petition",
            info={"attachment": {"name": "q_pet", "number": 0}},
        )
        target = resolve_automatic_target(
            [generic, generic_red], {"doc": col_orig}, "petition.docx"
        )
        assert target is not None
        assert target.variable == "documents['stage'].original[0].petition"
        assert target.x_expression == "documents['stage']"

        # 2 candidates -> ambiguity input error listing both
        col_red = _FakeDAObject(
            "documents['stage'].redacted[0].petition",
            info={"attachment": {"name": "q_pet", "number": 1}},
        )
        with pytest.raises(AttachmentInputError, match="2 attachment variables") as exc:
            resolve_automatic_target(
                [generic, generic_red],
                {"a": col_orig, "b": col_red},
                "petition.docx",
            )
        assert exc.value.details["candidates"] == [
            "documents['stage'].original[0].petition",
            "documents['stage'].redacted[0].petition",
        ]


class TestCanonicalAndAliases:
    def test_canonical_variable_name_substitutes_generic_x_and_indices(self):
        ns = {"x": _FakeDAObject("documents['petition']"), "i": 0, "j": 2}
        assert (
            canonical_variable_name("x.original[i].part[j]", ns, is_generic=True)
            == "documents['petition'].original[0].part[2]"
        )

    def test_expect_missing_alias_resolves_field_code_binding_and_index(self):
        stage = _FakeDAObject("documents['petition']")
        bindings = {"S": stage}
        namespace = {"i": 1}

        assert (
            resolve_expect_missing_alias("S.notary_block_yn", bindings, namespace)
            == "documents['petition'].notary_block_yn"
        )
        assert (
            resolve_expect_missing_alias("S.items[i].flag", bindings, namespace)
            == "documents['petition'].items[1].flag"
        )
        assert (
            resolve_expect_missing_alias(
                "documents['petition'].notary_block_yn", bindings, namespace
            )
            is None
        )


class TestAssembleValidationAndArtifacts:
    def test_rejects_manual_and_non_docx_before_assembly(self):
        manual = _entry("doc", is_manual=True)
        with pytest.raises(AttachmentInputError, match="manual"):
            assemble_attachment({}, AttachmentTarget(manual, "doc"))

        pdf_form = _entry("doc", is_pdf_template=True)
        with pytest.raises(AttachmentInputError, match="pdf template"):
            assemble_attachment({}, AttachmentTarget(pdf_form, "doc"))

        pdf_only_md = _entry(
            "doc",
            has_docx_template=False,
            docx_templates=(),
            attachment={"variable_name": "doc", "valid_formats": ["pdf"]},
        )
        with pytest.raises(PDFConversionUnavailable):
            assemble_attachment({}, AttachmentTarget(pdf_only_md, "doc"))

    def test_docx_paragraph_count_and_publish_attachment(self, tmp_path):
        docx_path = tmp_path / "rendered.docx"
        _make_docx(docx_path, paragraphs=5)

        assert docx_paragraph_count(docx_path) == 5
        assert docx_paragraph_count(tmp_path / "missing.docx") == 0

        with capture_published_attachments() as published:
            item = publish_attachment(docx_path, "Petition_Original.docx")
            # Duplicate path is deduplicated
            publish_attachment(docx_path, "Petition_Original.docx")

        assert len(published) == 1
        assert item.filename == "Petition_Original.docx"
        assert item.extension == "docx"
        assert item.structure == "ok"
        assert item.path == docx_path.resolve()
