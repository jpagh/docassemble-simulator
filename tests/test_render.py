from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from docassemble_simulator.render import (
    RenderError,
    RenderExpectationError,
    TemplateNotFoundError,
    assert_missing,
    find_template,
    missing_error_matches,
    prepare_docx_template,
    render_template,
    write_artifact,
)


class TestFindTemplate:
    def test_resolves_filename_under_package_templates(self, tmp_path):
        template = tmp_path / "docassemble" / "pkg" / "data" / "templates" / "form.docx"
        template.parent.mkdir(parents=True)
        template.write_bytes(b"docx")

        assert find_template(tmp_path, "form.docx") == template

    def test_missing_template_reports_available_location(self, tmp_path):
        (tmp_path / "docassemble" / "pkg" / "data" / "templates").mkdir(parents=True)

        with pytest.raises(TemplateNotFoundError, match="form.docx"):
            find_template(tmp_path, "form.docx")

    def test_ambiguous_template_is_rejected(self, tmp_path):
        for pkg in ("one", "two"):
            template = (
                tmp_path / "docassemble" / pkg / "data" / "templates" / "form.docx"
            )
            template.parent.mkdir(parents=True, exist_ok=True)
            template.write_bytes(b"docx")

        with pytest.raises(TemplateNotFoundError, match="ambiguous"):
            find_template(tmp_path, "form.docx")

    def test_rejects_template_symlink_outside_templates_directory(self, tmp_path):
        templates = tmp_path / "docassemble" / "pkg" / "data" / "templates"
        templates.mkdir(parents=True)
        outside = tmp_path / "outside.docx"
        outside.write_bytes(b"docx")
        (templates / "form.docx").symlink_to(outside)

        with pytest.raises(TemplateNotFoundError, match="form.docx"):
            find_template(tmp_path, "form.docx")


class TestPrepareDocxTemplate:
    def test_injects_paragraph_lines_and_fixes_only_expression_quotes(
        self, monkeypatch, tmp_path
    ):
        template_path = tmp_path / "template.docx"
        template_path.write_bytes(b"placeholder")
        calls = {}

        class FakeTemplate:
            def __init__(self, path):
                calls["path"] = path

            def render_init(self):
                calls["render_init"] = True

            def get_xml(self):
                return "<w:body><w:p><w:t>{{ “name” }}</w:t></w:p><w:p><w:t>body “quote”</w:t></w:p></w:body>"

            def patch_xml(self, xml):
                calls["patched_xml"] = xml
                return xml + "<!-- patched -->"

        class FakeEnvironment:
            def parse(self, xml):
                calls["parsed_xml"] = xml

        def fake_fix_quotes(match):
            return match.group(1).replace("“", '"').replace("”", '"')

        monkeypatch.setitem(
            sys.modules, "docxtpl", types.SimpleNamespace(DocxTemplate=FakeTemplate)
        )
        monkeypatch.setitem(
            sys.modules,
            "docassemble.base.helpers",
            types.SimpleNamespace(fix_quotes=fake_fix_quotes),
        )
        monkeypatch.setitem(
            sys.modules,
            "docassemble.base.jinja",
            types.SimpleNamespace(custom_jinja_env=lambda: FakeEnvironment()),
        )

        result = prepare_docx_template(template_path)

        assert isinstance(result, FakeTemplate)
        assert calls["path"] == template_path
        assert calls["render_init"] is True
        assert calls["patched_xml"].count("\n<w:p") == 2
        assert '{{ "name" }}' in calls["patched_xml"]
        assert "body “quote”" in calls["patched_xml"]
        assert calls["parsed_xml"] == calls["patched_xml"] + "<!-- patched -->"
        assert result._dasimulator_paragraphs == 2


class TestRenderErrors:
    def test_missing_error_matches_exact_variable_name(self):
        assert missing_error_matches(
            RenderError("'M.x' is undefined", error_type="UndefinedError"), "M.x"
        )
        assert not missing_error_matches(
            RenderError("'M.xyz' is undefined", error_type="UndefinedError"), "M.x"
        )

    def test_extracts_template_line_as_paragraph(self):
        source_error = RuntimeError("bad value")
        source_error.lineno = 31

        error = RenderError.from_exception(source_error)

        assert error.paragraph == 31
        assert error.error_type == "RuntimeError"
        assert str(error) == "bad value"


class TestAssertMissing:
    def test_missing_value_passes(self):
        assert_missing({"M": SimpleNamespace()}, "M.x") is None

    def test_resolved_value_fails(self):
        with pytest.raises(RenderExpectationError, match="M.x"):
            assert_missing({"M": SimpleNamespace(x=1)}, "M.x")

    def test_unrelated_runtime_error_is_not_treated_as_missing(self):
        class Exploding:
            @property
            def x(self):
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            assert_missing({"M": Exploding()}, "M.x")


class TestRenderContext:
    def test_render_enters_docx_context_and_returns_template(self, monkeypatch):
        calls = []
        thread = SimpleNamespace(misc={})

        functions = types.ModuleType("docassemble.base.functions")

        def set_context(kind, template=None):
            calls.append(("set", kind, template))
            thread.evaluation_context = kind
            thread.misc["docx_include_count"] = 0
            thread.misc["docx_template"] = template

        def reset_context():
            calls.append(("reset",))
            thread.evaluation_context = None
            thread.misc.pop("docx_include_count", None)
            thread.misc.pop("docx_template", None)

        functions.set_context = set_context
        functions.reset_context = reset_context
        functions.this_thread = thread
        monkeypatch.setitem(sys.modules, "docassemble.base.functions", functions)
        monkeypatch.setitem(
            sys.modules,
            "docassemble.base.jinja",
            types.SimpleNamespace(custom_jinja_env=lambda: object()),
        )

        class FakeTemplate:
            def render(self, context, jinja_env):
                assert thread.evaluation_context == "docx"
                assert thread.misc["docx_template"] is self
                assert context == {"name": "Alice"}

        template = FakeTemplate()

        assert render_template(template, {"name": "Alice"}) is template
        assert calls[0] == ("set", "docx", template)
        assert calls[-1] == ("reset",)
        assert thread.evaluation_context is None

    def test_include_requests_a_second_docx_render_pass(self, monkeypatch):
        thread = SimpleNamespace(misc={})
        functions = types.ModuleType("docassemble.base.functions")

        def set_context(kind, template=None):
            thread.evaluation_context = kind
            thread.misc["docx_include_count"] = 0
            thread.misc["docx_template"] = template

        def reset_context():
            thread.evaluation_context = None
            thread.misc.pop("docx_include_count", None)
            thread.misc.pop("docx_template", None)

        functions.set_context = set_context
        functions.reset_context = reset_context
        functions.this_thread = thread
        monkeypatch.setitem(sys.modules, "docassemble.base.functions", functions)
        monkeypatch.setitem(
            sys.modules,
            "docassemble.base.jinja",
            types.SimpleNamespace(custom_jinja_env=lambda: object()),
        )

        class FakeTemplate:
            def __init__(self, path=None):
                self.render_count = 0
                self.include_once = path is None
                self._dasimulator_paragraphs = 3

            def render_init(self):
                pass

            def render(self, context, jinja_env):
                self.render_count += 1
                if self.include_once and self.render_count == 1:
                    thread.misc["docx_include_count"] += 1

            def save(self, path):
                Path(path).write_bytes(b"docx")

        monkeypatch.setitem(
            sys.modules, "docxtpl", types.SimpleNamespace(DocxTemplate=FakeTemplate)
        )
        template = FakeTemplate()

        result = render_template(template, {})

        assert result is not template
        assert result.render_count == 1
        assert result._dasimulator_paragraphs == 3
        assert thread.evaluation_context is None


class TestArtifact:
    def test_writes_rendered_docx_atomically(self, tmp_path):
        class FakeTemplate:
            def save(self, path):
                Path(path).write_bytes(b"rendered")

        artifact = write_artifact(FakeTemplate(), tmp_path / "render", "form.docx")

        assert artifact.read_bytes() == b"rendered"
        assert not list((tmp_path / "render").glob(".*.tmp"))


class TestRenderModule:
    def test_exact_artifact_path_is_atomic(self, tmp_path):
        class FakeTemplate:
            def save(self, path):
                Path(path).write_bytes(b"rendered")

        target = tmp_path / "artifacts" / "named.docx"
        assert write_artifact(FakeTemplate(), target) == target.resolve()
        assert target.read_bytes() == b"rendered"
        assert not list(target.parent.glob(".*"))
