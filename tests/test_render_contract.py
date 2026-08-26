import sys
import types
from types import SimpleNamespace

import pytest

import docassemble_simulator.execution as execution_module
from docassemble_simulator.execution import InterviewExecution
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
