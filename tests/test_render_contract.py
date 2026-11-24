import sys
import types
from types import SimpleNamespace
from typing import ClassVar

import pytest

import docassemble_simulator.execution as execution_module
from docassemble_simulator.execution import InterviewExecution, Start
from docassemble_simulator.render import (
    FixtureSource,
    FreshSource,
    InterviewRenderer,
    RenderError,
    RenderFailure,
    RenderOutcome,
    RenderRequest,
    RenderResult,
    SavedSessionSource,
    SnapshotSource,
)


def _install_fake_runtime(monkeypatch):
    class DAObject(SimpleNamespace):
        def __init__(self, instanceName=None, **values):
            super().__init__(instanceName=instanceName, **values)

    parse = sys.modules["docassemble.base.parse"]
    parse.get_initial_dict = lambda: {
        "_internal": {"tracker": 0},
        "nav": DAObject("nav", sections=None),
        "url_args": {},
    }
    util = types.ModuleType("docassemble.base.util")
    util.DAObject = DAObject
    monkeypatch.setitem(sys.modules, "docassemble.base.util", util)

    class FakeInterview:
        source = SimpleNamespace(path="main.yml", package="docassemble.pkg")

        def populate_non_pickleable(self, namespace):
            namespace["name"] = "Alice"

    cache = types.ModuleType("docassemble.base.interview_cache")
    cache.get_interview = lambda identity: FakeInterview()
    monkeypatch.setitem(sys.modules, "docassemble.base.interview_cache", cache)


def _workspace(tmp_path):
    questions = tmp_path / "docassemble" / "pkg" / "data" / "questions"
    templates = questions.parent / "templates"
    questions.mkdir(parents=True)
    templates.mkdir()
    (questions / "main.yml").write_text("---\nquestion: x\n")
    (templates / "form.docx").write_bytes(b"docx")
    return InterviewExecution(tmp_path, "docassemble.pkg:data/questions/main.yml")


def test_missing_template_returns_a_typed_render_failure(tmp_path):
    questions = tmp_path / "docassemble" / "pkg" / "data" / "questions"
    questions.mkdir(parents=True)
    (questions / "main.yml").write_text("---\nquestion: x\n")
    execution = InterviewExecution(tmp_path, "docassemble.pkg:data/questions/main.yml")

    outcome = InterviewRenderer(tmp_path, execution).render(
        RenderRequest("missing.docx", SavedSessionSource(), assemble=False)
    )

    assert isinstance(outcome, RenderOutcome)
    assert outcome.ok is False
    assert isinstance(outcome.error, RenderFailure)
    assert outcome.error.kind == "input"
    assert "missing.docx" in outcome.error.message


def test_render_preparation_callback_is_not_part_of_the_execution_interface():
    assert "PrepareRender" not in execution_module.__all__
    assert not hasattr(execution_module, "PrepareRender")


def test_artifact_cannot_replace_an_authored_template(tmp_path):
    execution = _workspace(tmp_path)
    template = tmp_path / "docassemble" / "pkg" / "data" / "templates" / "form.docx"
    template.write_bytes(b"authored")

    outcome = InterviewRenderer(tmp_path, execution).render(
        RenderRequest(
            "form.docx",
            FreshSource(),
            assemble=False,
            output=template,
        )
    )

    assert outcome.ok is False
    assert outcome.error.kind == "input"
    assert "template" in outcome.error.message
    assert template.read_bytes() == b"authored"


def test_render_effects_cannot_target_saved_session_storage(tmp_path):
    destination = tmp_path / ".simulator" / "sessions" / "clobber.pkl"

    outcome = InterviewRenderer(tmp_path, _workspace(tmp_path)).render(
        RenderRequest(
            "form.docx",
            FreshSource(),
            assemble=False,
            output=destination,
        )
    )

    assert outcome.ok is False
    assert outcome.error.kind == "input"
    assert "saved-session" in outcome.error.message
    assert not destination.exists()


def test_render_assembly_reports_exhausted_seek_as_unresolved_variable(
    tmp_path, monkeypatch, da_stubs
):
    class DAErrorMissingVariable(Exception):
        def __init__(self, variable):
            super().__init__(f"could not define {variable}")
            self.variable = variable

    sys.modules[
        "docassemble.base.error"
    ].DAErrorMissingVariable = DAErrorMissingVariable
    sys.modules["docassemble.base.error"].DAErrorNoEndpoint = type(
        "DAErrorNoEndpoint", (Exception,), {}
    )
    _install_fake_runtime(monkeypatch)

    class MissingInterview:
        source = SimpleNamespace(path="main.yml", package="docassemble.pkg")
        questions_by_name: ClassVar[dict] = {}

        def populate_non_pickleable(self, namespace):
            namespace["name"] = "Alice"

        def assemble(self, namespace, interview_status):
            raise DAErrorMissingVariable("M.orphan")

    cache = types.ModuleType("docassemble.base.interview_cache")
    cache.get_interview = lambda identity: MissingInterview()
    monkeypatch.setitem(sys.modules, "docassemble.base.interview_cache", cache)
    execution = _workspace(tmp_path)
    started = execution.run(Start())
    assert not started.ok and started.error.kind == "unresolved-variable"

    outcome = InterviewRenderer(tmp_path, execution).render(
        RenderRequest("form.docx", SavedSessionSource(), assemble=True)
    )

    assert outcome.ok is False
    assert outcome.error.kind == "unresolved-variable"
    assert outcome.error.details.get("sought_variable") == "M.orphan"


def test_destination_validation_precedes_fixture_execution(tmp_path):
    execution = _workspace(tmp_path)
    fixture = tmp_path / "fixture.py"
    marker = tmp_path / "marker"
    fixture.write_text(f"{marker!r}.write_text('ran')\n")
    template_directory = tmp_path / "docassemble" / "pkg" / "data" / "templates"

    outcome = InterviewRenderer(tmp_path, execution).render(
        RenderRequest(
            "form.docx",
            FixtureSource(fixture),
            assemble=False,
            output=template_directory / "output.docx",
        )
    )

    assert outcome.ok is False
    assert outcome.error.kind == "input"
    assert not marker.exists()


def test_snapshot_destination_cannot_replace_an_authored_template(tmp_path):
    execution = _workspace(tmp_path)
    template_directory = tmp_path / "docassemble" / "pkg" / "data" / "templates"
    destination = template_directory / "state.snapshot"

    outcome = InterviewRenderer(tmp_path, execution).render(
        RenderRequest(
            "form.docx",
            FreshSource(),
            assemble=False,
            save_snapshot=destination,
        )
    )

    assert outcome.ok is False
    assert outcome.error.kind == "input"
    assert "template directories" in outcome.error.message
    assert not destination.exists()


def test_snapshot_and_artifact_destinations_must_be_distinct(tmp_path):
    destination = tmp_path / "render-output"

    outcome = InterviewRenderer(tmp_path, _workspace(tmp_path)).render(
        RenderRequest(
            "form.docx",
            FreshSource(),
            assemble=False,
            save_snapshot=destination,
            output=destination,
        )
    )

    assert outcome.ok is False
    assert outcome.error.kind == "input"
    assert "must be different" in outcome.error.message
    assert not destination.exists()


def test_render_effect_cannot_replace_a_snapshot_input(tmp_path):
    snapshot = tmp_path / "input.snapshot"
    snapshot.write_bytes(b"snapshot")

    outcome = InterviewRenderer(tmp_path, _workspace(tmp_path)).render(
        RenderRequest(
            "form.docx",
            SnapshotSource(snapshot),
            assemble=False,
            output=snapshot,
        )
    )

    assert outcome.ok is False
    assert outcome.error.kind == "input"
    assert "render input" in outcome.error.message
    assert snapshot.read_bytes() == b"snapshot"


def test_source_variants_require_exactly_their_own_data(tmp_path):
    assert SavedSessionSource() == SavedSessionSource()
    assert FreshSource() == FreshSource()
    assert SnapshotSource(tmp_path / "state") == SnapshotSource(tmp_path / "state")
    with pytest.raises(TypeError):
        SnapshotSource()
    with pytest.raises(TypeError):
        SavedSessionSource(tmp_path / "unexpected")


def test_successful_render_returns_a_typed_result(tmp_path, monkeypatch, da_stubs):
    _install_fake_runtime(monkeypatch)
    fake_docx = SimpleNamespace(_dasimulator_paragraphs=4)
    monkeypatch.setattr(
        "docassemble_simulator.render.prepare_docx_template", lambda path: fake_docx
    )
    monkeypatch.setattr(
        "docassemble_simulator.render.render_template",
        lambda prepared, namespace, **kwargs: prepared,
    )

    outcome = InterviewRenderer(tmp_path, _workspace(tmp_path)).render(
        RenderRequest("form.docx", FreshSource(), assemble=False)
    )

    assert isinstance(outcome, RenderOutcome)
    assert outcome.ok is True
    assert outcome.error is None
    assert outcome.result == RenderResult("form.docx", 4)


def test_expectation_and_io_failures_still_carry_template_attribution(
    tmp_path, monkeypatch, da_stubs
):
    _install_fake_runtime(monkeypatch)
    monkeypatch.setattr(
        "docassemble_simulator.render.prepare_docx_template",
        lambda path: SimpleNamespace(_dasimulator_paragraphs=8),
    )
    monkeypatch.setattr(
        "docassemble_simulator.render.render_template",
        lambda prepared, namespace, **kwargs: prepared,
    )

    outcome = InterviewRenderer(tmp_path, _workspace(tmp_path)).render(
        RenderRequest(
            "form.docx",
            FreshSource(),
            assemble=False,
            expect_missing="M.x",
        )
    )

    assert outcome.ok is False
    assert outcome.error.kind == "render"
    assert outcome.error.details == {
        "template": "form.docx",
        "error_type": "RenderExpectationError",
    }


def test_exception_filename_is_used_only_when_it_is_a_package_template(
    tmp_path, monkeypatch, da_stubs
):
    _install_fake_runtime(monkeypatch)
    monkeypatch.setattr(
        "docassemble_simulator.render.prepare_docx_template",
        lambda path: SimpleNamespace(_dasimulator_paragraphs=8),
    )
    foreign = tmp_path / "sandbox" / "included.docx"
    foreign.parent.mkdir()
    foreign.write_bytes(b"not a package template")

    def fail(prepared, namespace, **kwargs):
        raise RenderError(
            "bad include",
            paragraph=3,
            error_type="UndefinedError",
            template=str(foreign),
        )

    monkeypatch.setattr("docassemble_simulator.render.render_template", fail)

    outcome = InterviewRenderer(tmp_path, _workspace(tmp_path)).render(
        RenderRequest("form.docx", FreshSource(), assemble=False)
    )

    assert outcome.ok is False
    assert outcome.error.kind == "render"
    assert outcome.error.details["template"] == "form.docx"


def test_fresh_render_never_acquires_the_session_lock(tmp_path, monkeypatch, da_stubs):
    _install_fake_runtime(monkeypatch)
    monkeypatch.setattr(
        "docassemble_simulator.render.prepare_docx_template",
        lambda path: SimpleNamespace(_dasimulator_paragraphs=4),
    )
    monkeypatch.setattr(
        "docassemble_simulator.render.render_template",
        lambda prepared, namespace, **kwargs: prepared,
    )

    def forbidden_lock():
        raise AssertionError("session lock must not be acquired by render")

    monkeypatch.setattr(execution_module.StateStore, "lock", forbidden_lock)

    outcome = InterviewRenderer(tmp_path, _workspace(tmp_path)).render(
        RenderRequest("form.docx", FreshSource(), assemble=False)
    )

    assert outcome.ok is True


def test_saved_source_without_a_session_names_the_other_render_sources(
    tmp_path, monkeypatch, da_stubs
):
    _install_fake_runtime(monkeypatch)

    outcome = InterviewRenderer(tmp_path, _workspace(tmp_path)).render(
        RenderRequest("form.docx", SavedSessionSource(), assemble=False)
    )

    assert outcome.ok is False
    assert outcome.error.kind == "state"
    assert "no saved session" in outcome.error.message
    assert "`--fresh`" in outcome.error.message
    assert "`--snapshot PATH`" in outcome.error.message
    assert "`--fixture PATH`" in outcome.error.message


def test_fixture_failure_carries_path_error_type_and_traceback(
    tmp_path, monkeypatch, da_stubs
):
    _install_fake_runtime(monkeypatch)
    fixture = tmp_path / "fixture.py"
    fixture.write_text(
        "def build_namespace():\n"
        '    raise FileNotFoundError("missing letterhead: /nonexistent.docx")\n'
        "\n"
        "build_namespace()\n",
        encoding="utf-8",
    )

    outcome = InterviewRenderer(tmp_path, _workspace(tmp_path)).render(
        RenderRequest("form.docx", FixtureSource(fixture), assemble=False)
    )

    assert outcome.ok is False
    assert outcome.error.kind == "execution"
    assert outcome.error.message.startswith(
        "fixture failed: FileNotFoundError: missing letterhead: /nonexistent.docx"
    )
    assert outcome.error.details["fixture"] == str(fixture)
    assert outcome.error.details["error_type"] == "FileNotFoundError"
    report = outcome.error.details["traceback"]
    assert "Traceback (most recent call last)" in report
    assert str(fixture) in report
    assert "build_namespace" in report
    assert "missing letterhead: /nonexistent.docx" in report


def test_unexpected_fixture_exception_is_still_a_fixture_failure(
    tmp_path, monkeypatch, da_stubs
):
    _install_fake_runtime(monkeypatch)
    fixture = tmp_path / "fixture.py"
    fixture.write_text("1 / 0\n", encoding="utf-8")

    outcome = InterviewRenderer(tmp_path, _workspace(tmp_path)).render(
        RenderRequest("form.docx", FixtureSource(fixture), assemble=False)
    )

    assert outcome.ok is False
    assert outcome.error.kind == "execution"
    assert outcome.error.message.startswith("fixture failed: ZeroDivisionError")
    assert outcome.error.details["error_type"] == "ZeroDivisionError"
    assert "ZeroDivisionError" in outcome.error.details["traceback"]


def test_render_failure_identifies_the_requested_template(
    tmp_path, monkeypatch, da_stubs
):
    _install_fake_runtime(monkeypatch)
    monkeypatch.setattr(
        "docassemble_simulator.render.prepare_docx_template",
        lambda path: SimpleNamespace(_dasimulator_paragraphs=8),
    )
    monkeypatch.setattr(
        "docassemble_simulator.render.render_template",
        lambda prepared, namespace, **kwargs: (_ for _ in ()).throw(
            RenderError("bad template", paragraph=7, error_type="UndefinedError")
        ),
    )

    outcome = InterviewRenderer(tmp_path, _workspace(tmp_path)).render(
        RenderRequest("form.docx", FreshSource(), assemble=False)
    )

    assert outcome.ok is False
    assert outcome.error.kind == "render"
    assert outcome.error.details == {
        "template": "form.docx",
        "error_type": "UndefinedError",
        "paragraph": 7,
    }


def test_attachment_render_validates_mutual_exclusion_and_fixture_rejection(tmp_path):
    execution = _workspace(tmp_path)
    fixture = tmp_path / "fixture.py"
    fixture.write_text("x = 1\n")

    both = InterviewRenderer(tmp_path, execution).render(
        RenderRequest("form.docx", SavedSessionSource(), False, attachment="doc")
    )
    assert both.ok is False
    assert both.error.kind == "input"
    assert "not both" in both.error.message

    neither = InterviewRenderer(tmp_path, execution).render(
        RenderRequest(None, SavedSessionSource(), False)
    )
    assert neither.ok is False
    assert neither.error.kind == "input"

    with_fixture = InterviewRenderer(tmp_path, execution).render(
        RenderRequest(None, FixtureSource(fixture), False, attachment="doc")
    )
    assert with_fixture.ok is False
    assert with_fixture.error.kind == "input"
    assert "fixture" in with_fixture.error.message


def test_attachment_render_protects_template_and_session_destinations(tmp_path):
    execution = _workspace(tmp_path)
    template_dest = tmp_path / "docassemble" / "pkg" / "data" / "templates" / "out.docx"

    outcome = InterviewRenderer(tmp_path, execution).render(
        RenderRequest(
            None,
            FreshSource(),
            False,
            output=template_dest,
            attachment="doc",
        )
    )

    assert outcome.ok is False
    assert outcome.error.kind == "input"
    assert "template directories" in outcome.error.message
    assert not template_dest.exists()


def test_attachment_render_maps_expect_missing_alias_and_publishes_artifact(
    tmp_path, monkeypatch, da_stubs
):
    from zipfile import ZipFile

    _install_fake_runtime(monkeypatch)
    source_docx = tmp_path / "numbered.docx"
    with ZipFile(source_docx, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:body></w:document>",
        )

    class FakeFileRef:
        is_code = False

        def original_reference(self):
            return "form.docx"

    class FakeDocxFile:
        filename = "Rendered_Form.docx"

        def path(self):
            return str(source_docx)

    class FakeCollection:
        def __init__(self, instance_name):
            self.instanceName = instance_name
            self.docx = FakeDocxFile()

    class FakeStage:
        def __init__(self):
            self.instanceName = "documents['stage']"

    class FakeQuestion:
        name = "q_attach"
        is_generic = True

        def __init__(self, *, fail_missing: bool = False):
            self.fail_missing = fail_missing
            self.attachments = [
                {
                    "variable_name": "x.original[0].doc",
                    "question_name": "q_attach",
                    "indexno": 0,
                    "options": {"docx_template_file": [FakeFileRef()]},
                }
            ]

        def prepare_attachment(self, attachment, user_dict):
            return {"field_data": {"S": user_dict["x"]}, "formats_to_use": ["docx"]}

        def finalize_attachment(self, attachment, prepared, user_dict):
            if self.fail_missing:
                err = RuntimeError("'documents['stage'].notary_block_yn' is undefined")
                err.filename = "form.docx"
                raise err
            user_dict["documents"]["stage"].original[0].doc = FakeCollection(
                "documents['stage'].original[0].doc"
            )
            return {
                "formats_to_use": ["docx"],
                "file": {"docx": 1},
                "filename": "Rendered_Form",
            }

    def make_interview(fail_missing: bool):
        question = FakeQuestion(fail_missing=fail_missing)

        class FakeAttachInterview:
            source = SimpleNamespace(path="main.yml", package="docassemble.pkg")
            all_questions = (question,)

            def populate_non_pickleable(self, namespace):
                stage = FakeStage()
                stage.original = [SimpleNamespace()]
                namespace["documents"] = {"stage": stage}

        return FakeAttachInterview()

    cache = types.ModuleType("docassemble.base.interview_cache")
    cache.get_interview = lambda identity: make_interview(fail_missing=False)
    monkeypatch.setitem(sys.modules, "docassemble.base.interview_cache", cache)

    execution = _workspace(tmp_path)
    output_path = tmp_path / "exported.docx"
    outcome = InterviewRenderer(tmp_path, execution).render(
        RenderRequest(
            None,
            FreshSource(),
            False,
            output=output_path,
            attachment="documents['stage'].original[0].doc",
        )
    )

    assert outcome.ok is True
    assert outcome.result == RenderResult(
        "form.docx",
        1,
        output_path.resolve(),
        attachment="documents['stage'].original[0].doc",
    )
    assert output_path.is_file()
    assert len(outcome.attachments) == 1
    assert outcome.attachments[0].filename == "Rendered_Form.docx"
    assert outcome.attachments[0].path == output_path.resolve()

    # Now verify --expect-missing alias matching (S.notary_block_yn -> documents['stage'].notary_block_yn)
    cache.get_interview = lambda identity: make_interview(fail_missing=True)
    matched = InterviewRenderer(tmp_path, execution).render(
        RenderRequest(
            None,
            FreshSource(),
            False,
            expect_missing="S.notary_block_yn",
            attachment="documents['stage'].original[0].doc",
        )
    )
    assert matched.ok is True
    assert matched.result == RenderResult(
        "form.docx",
        0,
        None,
        attachment="documents['stage'].original[0].doc",
    )

    mismatched = InterviewRenderer(tmp_path, execution).render(
        RenderRequest(
            None,
            FreshSource(),
            False,
            expect_missing="S.other_flag",
            attachment="documents['stage'].original[0].doc",
        )
    )
    assert mismatched.ok is False
    assert mismatched.error.kind == "render"
    assert mismatched.error.details["template"] == "form.docx"
    assert mismatched.error.details["paragraph"] is None
    assert (
        mismatched.error.details["sought_variable"]
        == "documents['stage'].notary_block_yn"
    )
    assert mismatched.error.details["expected_missing"] == "S.other_flag"
    assert (
        mismatched.error.details["resolved_expected_missing"]
        == "documents['stage'].other_flag"
    )
