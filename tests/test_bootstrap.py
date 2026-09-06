import sys
import types
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from docassemble_simulator._artifacts import (
    LocalFileRegistry,
    capture_published_attachments,
)
from docassemble_simulator._runtime import (
    SimulatorRuntime,
    _configured_timezone,
    _without_pdf_conversion,
    install_attachment_filename_fallback,
    register_hooks,
)
from docassemble_simulator.config import deep_merge


class TestRuntimeBindings:
    def test_legacy_runtime_installs_file_metadata_on_existing_server(
        self, monkeypatch
    ):
        da = types.ModuleType("docassemble")
        da.__path__ = []
        base = types.ModuleType("docassemble.base")
        base.__path__ = []
        functions = types.ModuleType("docassemble.base.functions")
        original_server = types.SimpleNamespace()
        functions.server = original_server
        config = types.ModuleType("docassemble.base.config")
        config.daconfig = {}
        util = types.ModuleType("docassemble.base.util")
        util.Individual = type("Individual", (), {})
        da.base = base
        base.functions = functions
        base.config = config
        base.util = util
        for name, module in {
            "docassemble": da,
            "docassemble.base": base,
            "docassemble.base.functions": functions,
            "docassemble.base.config": config,
            "docassemble.base.util": util,
        }.items():
            monkeypatch.setitem(sys.modules, name, module)

        register_hooks()

        assert functions.server is original_server
        assert original_server.get_ext_and_mimetype("pleading.docx") == (
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    def test_legacy_runtime_normalizes_server_keywords_and_defaults(
        self, monkeypatch, tmp_path
    ):
        from docassemble_simulator import _runtime as runtime_module

        da = types.ModuleType("docassemble")
        da.__path__ = []
        base = types.ModuleType("docassemble.base")
        base.__path__ = []
        functions = types.ModuleType("docassemble.base.functions")
        functions.server = types.SimpleNamespace()
        config = types.ModuleType("docassemble.base.config")
        config.daconfig = {
            "jinja data": {"category": "Family"},
            "timezone": "America/New_York",
        }
        util = types.ModuleType("docassemble.base.util")
        util.Individual = type("Individual", (), {})
        da.base = base
        base.config = config
        base.functions = functions
        base.util = util
        for name, module in {
            "docassemble": da,
            "docassemble.base": base,
            "docassemble.base.config": config,
            "docassemble.base.functions": functions,
            "docassemble.base.util": util,
        }.items():
            monkeypatch.setitem(sys.modules, name, module)

        seen = {}
        authored = tmp_path / "authored-file"

        def fake_authored_file_path(reference, **kwargs):
            seen.update(reference=reference, **kwargs)
            return authored

        monkeypatch.setattr(
            runtime_module, "_authored_file_path", fake_authored_file_path
        )
        register_hooks()
        server = functions.server

        assert server.get_default_voice() == ""
        assert server.get_default_dialect() == ""
        assert server.get_default_language() == "en"
        assert server.get_default_locale() == "en_US"
        assert server.get_default_timezone() == "America/New_York"
        assert server.get_default_country() == "US"
        assert server.default_voice == ""
        assert server.default_dialect == ""
        assert server.default_language == "en"
        assert server.default_locale == "en_US"
        assert server.default_timezone == "America/New_York"
        assert server.default_country == "US"
        assert server.hostname == "localhost"
        assert server.debug is True
        assert server.debug_status is True
        assert server.main_page_parts == {}
        assert server.button_class_prefix == "btn"
        assert server.daconfig["jinja data"] == {"category": "Family"}

        assert server.file_finder(
            "template.docx",
            _question="question",
            _package="docassemble.package",
            folder="templates",
            ignored=True,
        )["path"] == str(authored)
        assert seen == {
            "reference": "template.docx",
            "question": "question",
            "folder": "templates",
            "package": "docassemble.package",
        }

        register_hooks()
        assert server.file_finder("template.docx", _package="docassemble.package")[
            "path"
        ] == str(authored)

    def test_legacy_url_finder_normalizes_option_styles(self, monkeypatch, tmp_path):
        from docassemble_simulator import _runtime as runtime_module

        da = types.ModuleType("docassemble")
        da.__path__ = []
        base = types.ModuleType("docassemble.base")
        base.__path__ = []
        functions = types.ModuleType("docassemble.base.functions")
        functions.server = types.SimpleNamespace()
        config = types.ModuleType("docassemble.base.config")
        config.daconfig = {}
        util = types.ModuleType("docassemble.base.util")
        util.Individual = type("Individual", (), {})
        for name, module in {
            "docassemble": da,
            "docassemble.base": base,
            "docassemble.base.functions": functions,
            "docassemble.base.config": config,
            "docassemble.base.util": util,
        }.items():
            monkeypatch.setitem(sys.modules, name, module)
        authored = tmp_path / "docassemble" / "pkg" / "data" / "static"
        authored.mkdir(parents=True)
        (authored / "template.docx").write_bytes(b"docx")
        with runtime_module.SimulatorRuntime().activate(tmp_path):
            runtime_module.register_hooks()
            server = functions.server
            via_options = server.url_finder(
                "template.docx", {"_package": "docassemble.pkg"}
            )
            via_kwargs = server.url_finder("template.docx", _package="docassemble.pkg")
        assert via_options == via_kwargs
        assert via_options.startswith("file://")


def _legacy_functions(thread=None, daconfig=None, omit=()):
    """A recording legacy ``functions`` double with thread-local semantics."""
    thread = thread if thread is not None else types.SimpleNamespace()
    calls = {"restored": []}
    functions = types.SimpleNamespace(
        server=types.SimpleNamespace(),
        this_thread=thread,
    )
    functions.populate_this_thread_defaults = lambda: setattr(thread, "populated", True)
    functions.backup_thread_variables = lambda: setattr(thread, "backed_up", True)

    def restore_thread_variables(saved):
        calls["restored"].append(dict(saved))
        vars(thread).clear()
        vars(thread).update(saved)

    functions.restore_thread_variables = restore_thread_variables
    for name in omit:
        delattr(functions, name)
    functions.calls = calls
    functions.seed_daconfig = dict(daconfig or {})
    return functions


def _install_legacy_modules(monkeypatch, functions):
    """Expose a legacy-only stub runtime through ``sys.modules``."""
    da = types.ModuleType("docassemble")
    da.__path__ = []
    base = types.ModuleType("docassemble.base")
    base.__path__ = []
    module = types.ModuleType("docassemble.base.functions")
    for name in (
        "server",
        "this_thread",
        "populate_this_thread_defaults",
        "backup_thread_variables",
        "restore_thread_variables",
    ):
        if hasattr(functions, name):
            setattr(module, name, getattr(functions, name))
    config = types.ModuleType("docassemble.base.config")
    config.daconfig = functions.seed_daconfig
    da.base = base
    base.functions = module
    base.config = config
    for name, mod in {
        "docassemble": da,
        "docassemble.base": base,
        "docassemble.base.functions": module,
        "docassemble.base.config": config,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return config


class TestStatusField:
    def test_modern_shape_passes_through(self):
        from docassemble_simulator._runtime import status_field

        status = types.SimpleNamespace(
            question_text="Q?", subquestion_text="sub", continue_label="Go"
        )
        assert status_field(status, "question_text") == "Q?"
        assert status_field(status, "subquestion_text") == "sub"
        assert status_field(status, "continue_label") == "Go"

    def test_legacy_shape_maps_camel_case(self):
        from docassemble_simulator._runtime import status_field

        status = types.SimpleNamespace(
            questionText="Q?", subquestionText="sub", continueLabel="Go"
        )
        assert status_field(status, "question_text") == "Q?"
        assert status_field(status, "subquestion_text") == "sub"
        assert status_field(status, "continue_label") == "Go"

    def test_unknown_field_raises_attribute_error(self):
        from docassemble_simulator._runtime import status_field

        with pytest.raises(AttributeError):
            status_field(types.SimpleNamespace(), "question_text")


class TestLegacyContext:
    def test_installs_namespace_and_restores_previous_state(self, monkeypatch):
        from docassemble_simulator._runtime import runtime_context

        thread = types.SimpleNamespace(preexisting="keep")
        functions = _legacy_functions(thread=thread)
        _install_legacy_modules(monkeypatch, functions)
        namespace = {"_internal": {"marker": True}, "user_var": 1}

        with runtime_context(namespace):
            assert functions.this_thread.current_dict is namespace
            assert functions.this_thread.internal == {"marker": True}
            assert functions.this_thread.populated is True
            assert functions.this_thread.backed_up is True

        assert vars(thread) == {"preexisting": "keep"}
        assert functions.calls["restored"] == [{"preexisting": "keep"}]

    def test_exception_still_restores_previous_state(self, monkeypatch):
        from docassemble_simulator._runtime import runtime_context

        thread = types.SimpleNamespace(preexisting="keep")
        functions = _legacy_functions(thread=thread)
        _install_legacy_modules(monkeypatch, functions)

        with (
            pytest.raises(RuntimeError, match="boom"),
            runtime_context({"user_var": 1}),
        ):
            raise RuntimeError("boom")

        assert vars(thread) == {"preexisting": "keep"}
        assert functions.calls["restored"] == [{"preexisting": "keep"}]

    def test_nested_contexts_restore_outer_state(self, monkeypatch):
        from docassemble_simulator._runtime import runtime_context

        functions = _legacy_functions()
        _install_legacy_modules(monkeypatch, functions)
        outer = {"user_var": "outer"}
        inner = {"user_var": "inner"}

        with runtime_context(outer):
            with runtime_context(inner):
                assert functions.this_thread.current_dict is inner
            assert functions.this_thread.current_dict is outer

    def test_context_entry_refreshes_server_config(self, monkeypatch):
        from docassemble_simulator._runtime import runtime_context

        functions = _legacy_functions(daconfig={"timezone": "America/New_York"})
        config = _install_legacy_modules(monkeypatch, functions)

        with runtime_context():
            pass
        assert functions.server.daconfig["timezone"] == "America/New_York"
        assert functions.server.daconfig["debug"] is True

        config.daconfig = {"timezone": "Europe/Paris"}
        with runtime_context():
            pass
        assert functions.server.daconfig["timezone"] == "Europe/Paris"

    def test_incomplete_runtime_names_missing_capability(self, monkeypatch):
        from docassemble_simulator._runtime import (
            RuntimeCompatibilityError,
            runtime_context,
        )

        functions = _legacy_functions(omit=("backup_thread_variables",))
        _install_legacy_modules(monkeypatch, functions)

        with (
            pytest.raises(RuntimeCompatibilityError, match="backup_thread_variables"),
            runtime_context(),
        ):
            pass  # pragma: no cover - adapter rejects before entry


class TestLegacyFakeRedis:
    def test_redis_installed_and_preserved_on_reinstall(self, monkeypatch):
        from docassemble_simulator._runtime import FakeRedis, register_hooks

        functions = _legacy_functions()
        _install_legacy_modules(monkeypatch, functions)
        util = types.ModuleType("docassemble.base.util")
        util.Individual = type("Individual", (), {})
        monkeypatch.setitem(sys.modules, "docassemble.base.util", util)

        register_hooks()
        assert isinstance(functions.server.server_redis, FakeRedis)
        assert isinstance(functions.server.server_redis_user, FakeRedis)

        redis, redis_user = (
            functions.server.server_redis,
            functions.server.server_redis_user,
        )
        register_hooks()
        assert functions.server.server_redis is redis
        assert functions.server.server_redis_user is redis_user


class TestIncompleteRuntime:
    def test_register_hooks_missing_util_is_actionable(self, monkeypatch):
        from docassemble_simulator._runtime import (
            RuntimeCompatibilityError,
            register_hooks,
        )

        da = types.ModuleType("docassemble")
        da.__path__ = []
        base = types.ModuleType("docassemble.base")
        base.__path__ = []
        functions = types.ModuleType("docassemble.base.functions")
        functions.server = types.SimpleNamespace()
        config = types.ModuleType("docassemble.base.config")
        config.daconfig = {}
        for name, module in {
            "docassemble": da,
            "docassemble.base": base,
            "docassemble.base.functions": functions,
            "docassemble.base.config": config,
        }.items():
            monkeypatch.setitem(sys.modules, name, module)
        monkeypatch.delitem(sys.modules, "docassemble.base.util", raising=False)
        monkeypatch.delitem(
            sys.modules, "docassemble.base.plugin_manager", raising=False
        )

        with pytest.raises(RuntimeCompatibilityError, match="util"):
            register_hooks()

    def test_configuration_without_config_module_is_actionable(self, monkeypatch):
        from docassemble_simulator._runtime import (
            RuntimeCompatibilityError,
            _SimulatorRuntimeBindings,
        )

        da = types.ModuleType("docassemble")
        da.__path__ = []
        base = types.ModuleType("docassemble.base")
        base.__path__ = []
        for name, module in {
            "docassemble": da,
            "docassemble.base": base,
        }.items():
            monkeypatch.setitem(sys.modules, name, module)
        monkeypatch.delitem(sys.modules, "docassemble.base.config", raising=False)

        with pytest.raises(RuntimeCompatibilityError, match="docassemble.base.config"):
            _SimulatorRuntimeBindings().get_configuration()


class TestLegacyBackgroundFallback:
    def _stub_legacy_without_background(self, monkeypatch):
        from docassemble_simulator import _runtime as runtime_module

        monkeypatch.setattr(runtime_module, "_BACKGROUND_INSTALLED", False)
        da = types.ModuleType("docassemble")
        da.__path__ = []
        base = types.ModuleType("docassemble.base")
        base.__path__ = []
        functions = types.ModuleType("docassemble.base.functions")
        functions.server = types.SimpleNamespace()
        interview = types.SimpleNamespace(
            askfor=lambda *args, **kwargs: {
                "question": types.SimpleNamespace(
                    question_type="backgroundresponse", backgroundresponse=42
                )
            }
        )
        functions.this_thread = types.SimpleNamespace(
            current_dict={},
            current_info={},
            interview_status=object(),
            interview=interview,
        )
        config = types.ModuleType("docassemble.base.config")
        config.daconfig = {}
        util = types.ModuleType("docassemble.base.util")
        util.Individual = type("Individual", (), {})
        util.background_action = None
        for name, module in {
            "docassemble": da,
            "docassemble.base": base,
            "docassemble.base.functions": functions,
            "docassemble.base.config": config,
            "docassemble.base.util": util,
        }.items():
            monkeypatch.setitem(sys.modules, name, module)
        monkeypatch.delitem(sys.modules, "docassemble.base.background", raising=False)
        monkeypatch.delitem(
            sys.modules, "docassemble.base.plugin_manager", raising=False
        )
        return functions, util

    def test_legacy_background_dispatch_completes_without_background_module(
        self, monkeypatch
    ):
        from docassemble_simulator import _runtime as runtime_module

        functions, util = self._stub_legacy_without_background(monkeypatch)

        runtime_module.register_hooks()
        task = functions.server.bg_action("event")
        assert task.ready() and not task.failed()
        assert task.get() == 42

        runtime_module._install_background_action_fallback("foreground")
        # Every legacy dispatch seam resolves without the modern module.
        for dispatch in (
            functions.server.bg_action,
            functions.background_action,
            util.background_action,
        ):
            task = dispatch("event")
            assert task.ready() and not task.failed()
            assert task.get() == 42

    def test_legacy_disabled_mode_returns_pending(self, monkeypatch):
        from docassemble_simulator import _runtime as runtime_module

        functions, _ = self._stub_legacy_without_background(monkeypatch)

        runtime_module._install_background_action_fallback("disabled")
        task = functions.server.bg_action("event")
        assert not task.ready()
        assert not task.failed()

    def test_legacy_reinstall_reasserts_server_seam(self, monkeypatch):
        from docassemble_simulator import _runtime as runtime_module

        functions, _ = self._stub_legacy_without_background(monkeypatch)

        runtime_module._install_background_action_fallback("foreground")
        task = functions.server.bg_action("event")
        assert task.ready() and task.get() == 42
        # Simulate a replaced legacy server object; reinstall must patch it.
        functions.server = types.SimpleNamespace()
        runtime_module._install_background_action_fallback("foreground")
        task = functions.server.bg_action("event")
        assert task.ready() and not task.failed()
        assert task.get() == 42


class TestMissingRuntime:
    @pytest.fixture
    def no_docassemble(self, monkeypatch):
        for name in [
            name
            for name in sys.modules
            if name == "docassemble" or name.startswith("docassemble.")
        ]:
            monkeypatch.delitem(sys.modules, name)
        import importlib.abc

        class _Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "docassemble" or fullname.startswith("docassemble."):
                    raise ModuleNotFoundError(
                        f"No module named {fullname!r}", name=fullname
                    )

        blocker = _Blocker()
        sys.meta_path.insert(0, blocker)
        try:
            yield
        finally:
            sys.meta_path.remove(blocker)

    def test_runtime_context_reports_missing_runtime(self, no_docassemble):
        from docassemble_simulator._runtime import (
            RuntimeCompatibilityError,
            runtime_context,
        )

        with (
            pytest.raises(RuntimeCompatibilityError, match="No docassemble runtime"),
            runtime_context(),
        ):
            pass  # pragma: no cover - detection rejects before entry

    def test_register_hooks_reports_missing_runtime(self, no_docassemble):
        from docassemble_simulator._runtime import (
            RuntimeCompatibilityError,
            register_hooks,
        )

        with pytest.raises(
            RuntimeCompatibilityError, match="target package interpreter"
        ):
            register_hooks()


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
        target = tmp_path / ".simulator" / "config-effective.yml"
        with SimulatorRuntime().activate(
            tmp_path,
            config_path=target,
            extra_config={
                "timezone": "America/Chicago",
                "jinja data": {"category": {"family": "Family"}},
            },
        ):
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
        seen = {}

        class FakeQuestion:
            def finalize_attachment(self, _attachment, result, _user_dict):
                seen.update(result)
                return result

        parse = types.ModuleType("docassemble.base.parse")
        parse.Question = FakeQuestion
        monkeypatch.setitem(sys.modules, "docassemble.base.parse", parse)
        install_attachment_filename_fallback()
        result = {"formats_to_use": ["pdf", "docx"], "valid_formats": ["pdf", "docx"]}
        FakeQuestion().finalize_attachment(None, result, {})

        assert seen["formats_to_use"] == ["docx"]
        assert seen["valid_formats"] == ["docx"]


class TestForegroundBackgroundActions:
    def test_foreground_task_runs_event_in_current_context(self, monkeypatch, tmp_path):
        from docassemble_simulator import _runtime as runtime_module

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
        with runtime_module.SimulatorRuntime().activate(
            tmp_path, background_action_mode="foreground"
        ):
            task = runtime_module._foreground_background_action("event", answer=1)

        assert task.ready() and not task.failed()
        assert task.get() == 7
        assert thread.current_info == {}

    def test_callable_background_response_becomes_completed_task(
        self, monkeypatch, tmp_path
    ):
        from docassemble_simulator import _runtime as runtime_module

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

        def action():
            raise BackgroundResponseError("done")

        with runtime_module.SimulatorRuntime().activate(
            tmp_path, background_action_mode="foreground"
        ):
            task = runtime_module._foreground_background_action(action)

        assert task.ready() and not task.failed()
        assert task.get() == "done"
        assert thread.current_info == {}

    def test_disabled_mode_retains_pending_task(self, tmp_path):
        from docassemble_simulator import _runtime as runtime_module

        with runtime_module.SimulatorRuntime().activate(
            tmp_path, background_action_mode="disabled"
        ):
            task = runtime_module._foreground_background_action("event")
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
