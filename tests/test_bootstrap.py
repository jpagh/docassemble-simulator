import sys
import types
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from docassemble_simulator._artifacts import (
    LocalFileRegistry,
    capture_published_attachments,
)
from docassemble_simulator.bootstrap import (
    _configured_timezone,
    _without_pdf_conversion,
    install_attachment_filename_fallback,
    prepare_environment,
)
from docassemble_simulator.config import deep_merge


class TestBootstrapConfig:
    def test_configured_timezone_uses_docassemble_config(self, monkeypatch):
        da = types.ModuleType("docassemble")
        base = types.ModuleType("docassemble.base")
        config = types.ModuleType("docassemble.base.config")
        config.daconfig = {"timezone": "America/Chicago"}
        base.__path__ = []
        da.base = base
        base.config = config
        for name, module in {
            "docassemble": da,
            "docassemble.base": base,
            "docassemble.base.config": config,
        }.items():
            monkeypatch.setitem(sys.modules, name, module)

        assert _configured_timezone() == "America/Chicago"

    def test_prepare_environment_writes_effective_yaml(self, tmp_path, monkeypatch):
        from docassemble_simulator import bootstrap

        monkeypatch.setattr(bootstrap, "_PREPARED", False)
        target = tmp_path / ".simulator" / "config-effective.yml"
        prepare_environment(
            config_path=target,
            extra_config={
                "timezone": "America/Chicago",
                "jinja data": {"category": {"family": "Family"}},
            },
        )

        import yaml

        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert loaded["timezone"] == "America/Chicago"
        assert loaded["jinja data"]["category"]["family"] == "Family"


class TestLocalAttachments:
    def test_registry_survives_process_shaped_reconstruction_and_publishes_uri(
        self, tmp_path
    ):
        source = tmp_path / "rendered.docx"
        source.write_bytes(b"docx")
        first = LocalFileRegistry(tmp_path / ".simulator" / "files")

        number, extension, mimetype = first.save("Family Plan.docx", source)
        second = LocalFileRegistry(tmp_path / ".simulator" / "files")
        with capture_published_attachments() as published:
            uri = second.url_for(types.SimpleNamespace(number=number))

        found = second.find(number)
        assert extension == "docx"
        assert mimetype == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        assert found["filename"] == "Family Plan.docx"
        assert Path(found["path"]).read_bytes() == b"docx"
        assert uri == Path(found["path"]).resolve().as_uri()
        assert len(published) == 1
        assert published[0].filename == "Family Plan.docx"
        assert published[0].uri == uri
        assert published[0].structure == "ok"

    def test_registry_adopts_existing_pre_index_numbered_files(self, tmp_path):
        directory = tmp_path / ".simulator" / "files"
        directory.mkdir(parents=True)
        existing = directory / "dasimulator-22.docx"
        existing.write_bytes(b"old")
        source = tmp_path / "new.docx"
        source.write_bytes(b"new")
        registry = LocalFileRegistry(directory)

        found = registry.find(22, "Family Plan.docx")
        number, _, _ = registry.save("Next Plan.docx", source)

        assert found["filename"] == "Family Plan.docx"
        assert Path(found["path"]) == existing
        assert number == 23
        assert existing.read_bytes() == b"old"

    def test_corrupt_registry_never_reuses_a_number_or_replaces_a_file(self, tmp_path):
        directory = tmp_path / ".simulator" / "files"
        directory.mkdir(parents=True)
        existing = directory / "dasimulator-1.docx"
        existing.write_bytes(b"keep")
        (directory / "index.json").write_text("not json", encoding="utf-8")
        source = tmp_path / "new.docx"
        source.write_bytes(b"replace")

        with pytest.raises(RuntimeError, match="file index"):
            LocalFileRegistry(directory).save("new.docx", source)

        assert existing.read_bytes() == b"keep"

    def test_published_docx_reports_nested_paragraph_structure(self, tmp_path):
        source = tmp_path / "nested.docx"
        with ZipFile(source, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "word/document.xml",
                """<?xml version="1.0" encoding="UTF-8"?>
                <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                  <w:body><w:p><w:r><w:p><w:r/></w:p></w:r></w:p></w:body>
                </w:document>""",
            )
        registry = LocalFileRegistry(tmp_path / ".simulator" / "files")
        number, _, _ = registry.save("nested.docx", source)

        with capture_published_attachments() as published:
            registry.url_for(types.SimpleNamespace(number=number))

        assert len(published) == 1
        assert published[0].diagnostics[0].kind == "docx-structure"
        assert published[0].diagnostics[0].details == {
            "part": "word/document.xml",
            "problem": "nested-paragraph",
            "count": 1,
        }


class TestAttachmentFormats:
    def test_pdf_conversion_is_omitted_but_docx_remains(self):
        result = {
            "formats_to_use": ["pdf", "docx"],
            "valid_formats": ["pdf", "docx"],
        }

        _without_pdf_conversion(result)

        assert result == {
            "formats_to_use": ["docx"],
            "valid_formats": ["docx"],
        }

    def test_pdf_only_attachment_has_no_output_format(self):
        result = {"formats_to_use": ["pdf"], "valid_formats": ["pdf"]}

        _without_pdf_conversion(result)

        assert result["formats_to_use"] == []
        assert result["valid_formats"] == []

    def test_finalizer_never_receives_generated_pdf(self, monkeypatch):
        from docassemble_simulator import bootstrap

        seen = {}

        class FakeQuestion:
            def finalize_attachment(self, _attachment, result, _user_dict):
                seen.update(result)
                return result

        parse = types.ModuleType("docassemble.base.parse")
        parse.Question = FakeQuestion
        monkeypatch.setitem(sys.modules, "docassemble.base.parse", parse)
        monkeypatch.setattr(bootstrap, "_ATTACHMENT_FALLBACK_INSTALLED", False)

        install_attachment_filename_fallback()
        result = {"formats_to_use": ["pdf", "docx"], "valid_formats": ["pdf", "docx"]}
        FakeQuestion().finalize_attachment(None, result, {})

        assert seen["formats_to_use"] == ["docx"]
        assert seen["valid_formats"] == ["docx"]


class TestForegroundBackgroundActions:
    def test_foreground_task_runs_event_in_current_context(self, monkeypatch):
        from docassemble_simulator import bootstrap

        thread = types.SimpleNamespace(
            current_dict={"value": 3},
            current_info={},
            interview_status=object(),
            interview=types.SimpleNamespace(
                askfor=lambda *args, **kwargs: {
                    "question": types.SimpleNamespace(
                        question_type="backgroundresponse", backgroundresponse=7
                    )
                }
            ),
        )
        functions = types.ModuleType("docassemble.base.functions")
        functions.this_thread = thread
        monkeypatch.setitem(sys.modules, "docassemble.base.functions", functions)
        monkeypatch.setattr(bootstrap, "_BACKGROUND_ACTION_MODE", "foreground")

        task = bootstrap._foreground_background_action("event", answer=1)

        assert task.ready() and not task.failed()
        assert task.get() == 7
        assert thread.current_info == {}

    def test_callable_background_response_becomes_completed_task(self, monkeypatch):
        from docassemble_simulator import bootstrap

        class BackgroundResponseError(Exception):
            def __init__(self, value):
                self.backgroundresponse = value

        thread = types.SimpleNamespace(
            current_dict={},
            current_info={},
            interview_status=object(),
            interview=object(),
        )
        functions = types.ModuleType("docassemble.base.functions")
        functions.this_thread = thread
        errors = types.ModuleType("docassemble.base.error")
        errors.BackgroundResponseError = BackgroundResponseError
        errors.BackgroundResponseActionError = type(
            "BackgroundResponseActionError", (Exception,), {}
        )
        monkeypatch.setitem(sys.modules, "docassemble.base.functions", functions)
        monkeypatch.setitem(sys.modules, "docassemble.base.error", errors)
        monkeypatch.setattr(bootstrap, "_BACKGROUND_ACTION_MODE", "foreground")

        def action():
            raise BackgroundResponseError("done")

        task = bootstrap._foreground_background_action(action)

        assert task.ready() and not task.failed()
        assert task.get() == "done"
        assert thread.current_info == {}

    def test_disabled_mode_retains_pending_task(self, monkeypatch):
        from docassemble_simulator import bootstrap

        monkeypatch.setattr(bootstrap, "_BACKGROUND_ACTION_MODE", "disabled")
        task = bootstrap._foreground_background_action("event")
        assert not task.ready()
        assert not task.failed()


class TestDeepMerge:
    def test_nested_merge_preserves_unrelated_keys(self):
        base = {"a": {"x": 1, "y": 2}, "keep": True}
        deep_merge(base, {"a": {"y": 3}})
        assert base == {"a": {"x": 1, "y": 3}, "keep": True}

    def test_scalar_override_replaces_dict(self):
        base = {"a": {"x": 1}}
        deep_merge(base, {"a": "flat"})
        assert base == {"a": "flat"}

    def test_dict_override_replaces_scalar(self):
        base = {"a": "flat"}
        deep_merge(base, {"a": {"x": 1}})
        assert base == {"a": {"x": 1}}

    def test_new_key_added(self):
        base = {}
        deep_merge(base, {"redis": "redis://localhost:6399"})
        assert base == {"redis": "redis://localhost:6399"}

    def test_deeply_nested(self):
        base = {"a": {"b": {"c": 1, "d": 2}}}
        deep_merge(base, {"a": {"b": {"d": 9}}})
        assert base == {"a": {"b": {"c": 1, "d": 9}}}
