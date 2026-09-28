from __future__ import annotations

import datetime
import sys
import types
from contextlib import contextmanager
from types import SimpleNamespace
from typing import ClassVar

import pytest

from docassemble_simulator.detect import guess_main_interview, resolve_interview
from docassemble_simulator.execution import (
    Answer,
    Evaluate,
    Execute,
    InterviewExecution,
    Refresh,
    Seek,
    Start,
    Status,
    Variables,
)
from docassemble_simulator.render import (
    FixtureSource,
    FreshSource,
    InterviewRenderer,
    RenderRequest,
    SavedSessionSource,
    SnapshotSource,
)


@pytest.fixture
def pkg_root(tmp_path):
    for pkg in ("pkg1", "pkg2"):
        qdir = tmp_path / "docassemble" / pkg / "data" / "questions"
        qdir.mkdir(parents=True)
        (qdir.parent.parent / "__init__.py").write_text("")
        (qdir / "main.yml").write_text("---\nquestion: x\n")
        (qdir / "shared.yml").write_text("---\nquestion: y\n")
    return tmp_path


def test_guess_main_interview_prefers_main():
    assert guess_main_interview(
        [
            "docassemble.pkg:data/questions/x.yml",
            "docassemble.pkg:data/questions/main.yml",
        ]
    ).endswith("main.yml")


def test_resolve_interview_rejects_ambiguous_filename(pkg_root):
    with pytest.raises(SystemExit, match="ambiguous"):
        resolve_interview(pkg_root, "shared.yml")


def _runtime(monkeypatch):
    class DAObject(SimpleNamespace):
        def __init__(self, instanceName=None, **values):
            super().__init__(instanceName=instanceName, **values)

    class DADict:
        def __init__(self, *, elements):
            self.elements = elements

    DADateTime = datetime.datetime

    def as_datetime(value):
        parsed = datetime.date.fromisoformat(value)
        return DADateTime(parsed.year, parsed.month, parsed.day, tzinfo=datetime.UTC)

    parse = sys.modules["docassemble.base.parse"]
    parse.get_initial_dict = lambda: {
        "_internal": {"tracker": 0, "answers": {}, "objselections": {}},
        "nav": DAObject("nav", sections=None),
        "url_args": {},
    }
    util = types.ModuleType("docassemble.base.util")
    util.DAObject = DAObject
    util.DADict = DADict
    util.DAEmpty = PicklableEmpty
    util.DADateTime = DADateTime
    util.as_datetime = as_datetime
    monkeypatch.setitem(sys.modules, "docassemble.base.util", util)
    error = sys.modules["docassemble.base.error"]
    error.DAErrorNoEndpoint = type("DAErrorNoEndpoint", (Exception,), {})
    return SimpleNamespace(
        DAErrorNoEndpoint=error.DAErrorNoEndpoint, DADateTime=DADateTime
    )


class PicklableStubObject(SimpleNamespace):
    """A stub DAObject that can cross the session pickle boundary."""

    def __init__(self, instanceName=None, **values):
        super().__init__(instanceName=instanceName, **values)


class PicklableStubDict(SimpleNamespace):
    """A stub DADict that can cross the session pickle boundary."""

    def __init__(self, *, elements=None):
        super().__init__(elements={} if elements is None else elements)


class PicklableEmpty:
    """A stub DAEmpty that can cross the session pickle boundary."""

    def __init__(self, *pargs, **kwargs):
        self.str = str(kwargs.get("str", ""))

    def __str__(self):
        return self.str


class NamespaceAwareList:
    """A stub object list modelling the ``DAList`` surface the answer path uses.

    docassemble stores ``object_checkboxes``/``object_multiselect`` answers in
    a ``DAList`` whose ``elements`` attribute is the underlying Python list.
    Docassemble 1.9.x's ``get_user_dict()`` walks the call stack, so a mutation
    that resolves interview state only works when the caller runs inside the
    interview namespace, as the real server's answer handling does. Every
    mutation is frame-checked for the same reason.
    """

    def __init__(self, values=(), **kwargs):
        self.elements = list(values)
        self.gathered = False

    def _require_frame(self, operation):
        from docassemble.base import functions

        if "frame_probe" not in functions.get_user_dict():
            raise RuntimeError(f"the interview namespace is unavailable to {operation}")

    def append(self, item):
        self._require_frame("append")
        self.elements.append(item)

    def remove(self, item):
        self._require_frame("remove")
        self.elements.remove(item)

    def clear(self):
        self._require_frame("clear")
        self.elements.clear()

    def __len__(self):
        return len(self.elements)

    def names(self):
        return [item.instanceName for item in self.elements]


@pytest.fixture
def legacy_frame_resolution(monkeypatch):
    """Install docassemble 1.9.x's frame-walking ``get_user_dict()``.

    1.10+ keeps the interview in a context variable that the simulator already
    sets; 1.9 walks the call stack looking for the frame that owns
    ``_internal``.
    """
    functions = sys.modules["docassemble.base.functions"]

    def legacy_get_user_dict():
        frame = sys._getframe(1)
        while frame is not None:
            if "_internal" in frame.f_locals:
                return frame.f_locals
            frame = frame.f_back
        return {}

    monkeypatch.setattr(functions, "get_user_dict", legacy_get_user_dict, raising=False)


class FakeInterview:
    source = SimpleNamespace(path="main.yml", package="docassemble.pkg")
    questions_by_name: ClassVar[dict] = {}

    def populate_non_pickleable(self, namespace):
        namespace["helper"] = lambda value: f"${value}"

    def assemble(self, namespace, interview_status):
        raise sys.modules["docassemble.base.error"].DAErrorNoEndpoint("finished")


class GenericDateInterview(FakeInterview):
    """A generic-object date screen: fields use ``x.date``, target is ``rav.date``."""

    required = False

    def assemble(self, namespace, interview_status):
        if "rav" not in namespace:
            from docassemble.base.util import DAObject

            namespace["rav"] = DAObject("rav")
        interview_status.question = SimpleNamespace(
            question_type="fields",
            name=None,
            validation_code=None,
            fields=[
                SimpleNamespace(
                    saveas="x.date", datatype="date", required=self.required
                )
            ],
        )
        interview_status.question_text = "What is the date?"
        interview_status.subquestion_text = None
        interview_status.continue_label = None
        interview_status.sought = "x.date"
        interview_status.orig_sought = "rav.date"
        interview_status.selectcompute = {}


class GenericMultiFieldInterview(FakeInterview):
    """A generic screen whose triggering field is not the only described field."""

    def assemble(self, namespace, interview_status):
        if "rav" not in namespace:
            from docassemble.base.util import DAObject

            namespace["rav"] = DAObject("rav")
        interview_status.question = SimpleNamespace(
            question_type="fields",
            name=None,
            validation_code=None,
            fields=[
                SimpleNamespace(
                    saveas="x.service_type", datatype="text", required=False
                ),
                SimpleNamespace(saveas="x.date", datatype="date", required=False),
            ],
        )
        interview_status.question_text = "Service"
        interview_status.subquestion_text = None
        interview_status.continue_label = None
        interview_status.sought = "x.service_type"
        interview_status.orig_sought = "rav.service_type"
        interview_status.selectcompute = {}


class MixedRequirementInterview(FakeInterview):
    """One required and one optional field on the same generic screen."""

    def assemble(self, namespace, interview_status):
        if "rav" not in namespace:
            from docassemble.base.util import DAObject

            namespace["rav"] = DAObject("rav")
        interview_status.question = SimpleNamespace(
            question_type="fields",
            name=None,
            validation_code=None,
            fields=[
                SimpleNamespace(
                    saveas="x.service_type", datatype="text", required=True
                ),
                SimpleNamespace(saveas="x.date", datatype="date", required=False),
            ],
        )
        interview_status.question_text = "Service"
        interview_status.subquestion_text = None
        interview_status.continue_label = None
        interview_status.sought = "x.service_type"
        interview_status.orig_sought = "rav.service_type"
        interview_status.selectcompute = {}


class DefaultedDateInterview(MixedRequirementInterview):
    """A required date field whose default the browser would prefill."""

    def assemble(self, namespace, interview_status):
        super().assemble(namespace, interview_status)
        interview_status.question.fields[1] = SimpleNamespace(
            saveas="x.date",
            datatype="date",
            required=True,
            default="2026-01-15",
        )


class PrefilledDateInterview(MixedRequirementInterview):
    """An optional date field already populated when the screen is shown."""

    def assemble(self, namespace, interview_status):
        super().assemble(namespace, interview_status)
        namespace["rav"].date = 5


class HiddenFieldInterview(MixedRequirementInterview):
    """A required field hidden by ``show if`` on the same screen."""

    def assemble(self, namespace, interview_status):
        super().assemble(namespace, interview_status)
        interview_status.question.fields[1] = SimpleNamespace(
            saveas="x.hidden",
            datatype="text",
            required=True,
            showif_code="False",
        )


class SignatureInterview(MixedRequirementInterview):
    """A required signature field alongside a required text field."""

    def assemble(self, namespace, interview_status):
        super().assemble(namespace, interview_status)
        interview_status.question.fields[1] = SimpleNamespace(
            saveas="x.signature",
            datatype="signature",
            required=True,
        )


class BadDefaultDateInterview(MixedRequirementInterview):
    """An optional date whose rendered default cannot be applied."""

    def assemble(self, namespace, interview_status):
        super().assemble(namespace, interview_status)
        interview_status.question.fields[1] = SimpleNamespace(
            saveas="x.date",
            datatype="date",
            required=False,
            default="not-a-date",
        )


class CheckboxGroupInterview(FakeInterview):
    """A required checkbox group beside an optional one on the same screen."""

    CHOICES: ClassVar[list[dict]] = [
        {"key": "housing", "label": "Housing"},
        {"key": "benefits", "label": "Benefits"},
    ]

    def assemble(self, namespace, interview_status):
        if "rav" not in namespace:
            from docassemble.base.util import DAObject

            namespace["rav"] = DAObject("rav")
        interview_status.question = SimpleNamespace(
            question_type="fields",
            name=None,
            validation_code=None,
            fields=[
                SimpleNamespace(
                    saveas="x.service_type", datatype="text", required=True
                ),
                SimpleNamespace(
                    saveas="x.topics",
                    datatype="checkboxes",
                    required=True,
                    choices=self.CHOICES,
                ),
                SimpleNamespace(
                    saveas="x.optional_topics",
                    datatype="checkboxes",
                    required=False,
                    choices=self.CHOICES,
                ),
            ],
        )
        interview_status.question_text = "Topics"
        interview_status.subquestion_text = None
        interview_status.continue_label = None
        interview_status.sought = "x.service_type"
        interview_status.orig_sought = "rav.service_type"
        interview_status.selectcompute = {}


class FrameAwareCheckboxInterview(FakeInterview):
    """An object checkbox screen whose list only accepts in-frame mutations.

    ``SEEDED`` names objects the list already holds before the screen is shown;
    ``CHOICES`` is what the screen offers. The two differ when a test needs a
    list element with no matching checkbox.
    """

    SEEDED: ClassVar[tuple[str, ...]] = ()
    CHOICES: ClassVar[dict[str, str]] = {"alice": "Alice"}

    def assemble(self, namespace, interview_status):
        namespace.setdefault("frame_probe", True)
        namespace.setdefault("alice", PicklableStubObject(instanceName="alice"))
        namespace.setdefault("bob", PicklableStubObject(instanceName="bob"))
        if "shortlist" not in namespace:
            seeded = [namespace[name] for name in self.SEEDED]
            namespace["shortlist"] = NamespaceAwareList(seeded)
        namespace["_internal"].setdefault("objselections", {})["shortlist"] = {
            key: namespace[key] for key in self.CHOICES
        }
        interview_status.question = SimpleNamespace(
            question_type="fields",
            name="shortlist choice",
            validation_code=None,
            fields=[
                SimpleNamespace(
                    saveas="shortlist",
                    datatype="object_checkboxes",
                    required=False,
                    choices=[
                        {"key": key, "label": label}
                        for key, label in self.CHOICES.items()
                    ],
                )
            ],
        )
        interview_status.question_text = "Who belongs on the shortlist?"
        interview_status.subquestion_text = None
        interview_status.continue_label = None
        interview_status.sought = "shortlist"
        interview_status.orig_sought = "shortlist"
        interview_status.selectcompute = {}


class BooleanStyleInterview(FakeInterview):
    """One screen covering every boolean input style and numeric blanks."""

    def assemble(self, namespace, interview_status):
        if "rav" not in namespace:
            from docassemble.base.util import DAObject

            namespace["rav"] = DAObject("rav")
        interview_status.question = SimpleNamespace(
            question_type="fields",
            name=None,
            validation_code=None,
            fields=[
                SimpleNamespace(
                    saveas="x.service_type", datatype="text", required=True
                ),
                SimpleNamespace(
                    saveas="x.agree",
                    datatype="boolean",
                    inputtype="yesno",
                    required=False,
                ),
                SimpleNamespace(
                    saveas="x.declined",
                    datatype="boolean",
                    inputtype="noyes",
                    required=False,
                ),
                SimpleNamespace(
                    saveas="x.refer",
                    datatype="boolean",
                    inputtype="yesnoradio",
                    required=False,
                ),
                SimpleNamespace(saveas="x.count", datatype="integer", required=False),
                SimpleNamespace(saveas="x.ratio", datatype="number", required=False),
            ],
        )
        interview_status.question_text = "Styles"
        interview_status.subquestion_text = None
        interview_status.continue_label = None
        interview_status.sought = "x.service_type"
        interview_status.orig_sought = "rav.service_type"
        interview_status.selectcompute = {}


def _execution(tmp_path, monkeypatch, da_stubs, interview=FakeInterview):
    qdir = tmp_path / "docassemble" / "pkg" / "data" / "questions"
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / "main.yml").write_text("---\nquestion: x\n")
    runtime = _runtime(monkeypatch)
    selected = {"interview": interview}
    cache = types.ModuleType("docassemble.base.interview_cache")
    cache.get_interview = lambda identity: selected["interview"]()
    monkeypatch.setitem(sys.modules, "docassemble.base.interview_cache", cache)
    execution = InterviewExecution(tmp_path, "docassemble.pkg:data/questions/main.yml")
    return execution, runtime, selected


def _session_files(root):
    return sorted((root / ".simulator" / "sessions").glob("*.pkl"))


def _session_bytes(root):
    return {path.name: path.read_bytes() for path in _session_files(root)}


def test_start_commits_versioned_per_interview_state_and_status_is_pure(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs)

    started = execution.run(Start())
    before = _session_bytes(tmp_path)
    status = execution.run(Status())

    assert started.ok and started.result["kind"] == "finished"
    assert status.result == started.result
    assert _session_bytes(tmp_path) == before
    assert len(before) == 1
    assert "main.yml" in next(iter(before))


def test_variable_seek_diagnostics_do_not_depend_on_server_debug_mode(
    tmp_path, monkeypatch, da_stubs
):
    class SeekingInterview(FakeInterview):
        debug = False

        def assemble(self, namespace, interview_status):
            interview_status.seeking = []
            if self.debug:
                interview_status.seeking.append({"variable": "M.value"})
            raise sys.modules["docassemble.base.error"].DAErrorNoEndpoint("finished")

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, SeekingInterview)

    outcome = execution.run(Start())

    assert outcome.ok
    assert outcome.diagnostics[0].details == {"variable": "M.value"}


def test_disabled_seek_diagnostics_captures_nothing_and_keeps_debug_unchanged(
    tmp_path, monkeypatch, da_stubs
):
    from docassemble_simulator import _diagnostics as diagnostics_module

    monkeypatch.setattr(diagnostics_module, "_ENABLED", False)

    class SeekingInterview(FakeInterview):
        def assemble(self, namespace, interview_status):
            interview_status.seeking = [{"variable": "M.children[0].name"}]
            raise sys.modules["docassemble.base.error"].DAErrorNoEndpoint("finished")

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, SeekingInterview)

    outcome = execution.run(Start())

    assert outcome.ok
    assert outcome.diagnostics == ()


def test_resolved_variable_seeking_is_diagnostic_not_failure(
    tmp_path, monkeypatch, da_stubs
):
    class SeekingInterview(FakeInterview):
        def assemble(self, namespace, interview_status):
            interview_status.seeking = [
                {"variable": "M.children[0].name"},
                {
                    "question": SimpleNamespace(name="children_name"),
                    "reason": "considering",
                },
                {
                    "question": SimpleNamespace(name="children_name"),
                    "reason": "asking",
                },
            ]
            raise sys.modules["docassemble.base.error"].DAErrorNoEndpoint("finished")

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, SeekingInterview)

    outcome = execution.run(Start())

    assert outcome.ok
    assert [item.kind for item in outcome.diagnostics] == [
        "variable-seek",
        "variable-seek",
        "variable-seek",
    ]
    assert outcome.diagnostics[0].details == {"variable": "M.children[0].name"}
    assert outcome.diagnostics[1].details == {
        "question": "children_name",
        "reason": "considering",
    }


def test_exhausted_variable_seeking_is_one_typed_failure(
    tmp_path, monkeypatch, da_stubs
):
    class DAErrorMissingVariable(Exception):
        def __init__(self, variable):
            super().__init__(f"could not define {variable}")
            self.variable = variable

    sys.modules[
        "docassemble.base.error"
    ].DAErrorMissingVariable = DAErrorMissingVariable

    class MissingInterview(FakeInterview):
        def assemble(self, namespace, interview_status):
            interview_status.seeking = [{"variable": "M.unknown"}]
            raise DAErrorMissingVariable("M.unknown")

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, MissingInterview)

    outcome = execution.run(Start())

    assert not outcome.ok
    assert outcome.error.kind == "unresolved-variable"
    assert outcome.error.details["sought_variable"] == "M.unknown"
    assert len(outcome.diagnostics) == 1
    assert outcome.diagnostics[0].details["variable"] == "M.unknown"


@pytest.mark.parametrize("activate", [False, True])
def test_explicit_exhausted_seek_uses_unresolved_variable_failure(
    tmp_path, monkeypatch, da_stubs, activate
):
    class DAErrorMissingVariable(Exception):
        def __init__(self, variable):
            super().__init__(f"could not define {variable}")
            self.variable = variable

    sys.modules[
        "docassemble.base.error"
    ].DAErrorMissingVariable = DAErrorMissingVariable

    class MissingInterview(FakeInterview):
        def askfor(self, variable, *args, **kwargs):
            args[2].seeking = [{"variable": variable}]
            raise DAErrorMissingVariable(variable)

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, MissingInterview)
    assert execution.run(Start()).ok

    outcome = execution.run(Seek("M.unknown", activate=activate))

    assert not outcome.ok
    assert outcome.error.kind == "unresolved-variable"
    assert outcome.error.details["sought_variable"] == "M.unknown"


def test_flow_error_is_committed_but_reported_as_failed_operation(
    tmp_path, monkeypatch, da_stubs
):
    class BrokenInterview(FakeInterview):
        def assemble(self, namespace, interview_status):
            namespace["debug_value"] = 42
            raise RuntimeError("authored flow broke")

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, BrokenInterview)

    outcome = execution.run(Start())

    assert not outcome.ok and outcome.error.kind == "execution"
    assert execution.run(Status()).result["kind"] == "error"
    assert execution.run(Evaluate("debug_value")).result["value"] == "42"


def test_explicit_dependency_pin_wins_over_stale_uv_lock(tmp_path):
    from docassemble_simulator.preflight import _locked_runtime_specs

    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["docassemble-base==1.0"]\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "docassemble-base"\nversion = "2.0"\n',
        encoding="utf-8",
    )

    assert _locked_runtime_specs(tmp_path, ["docassemble.base"]) == [
        "docassemble-base==1.0"
    ]


def test_sessions_are_isolated_by_canonical_interview_identity(
    tmp_path, monkeypatch, da_stubs
):
    first, _, _ = _execution(tmp_path, monkeypatch, da_stubs)
    second_path = (
        tmp_path / "docassemble" / "pkg" / "data" / "questions" / "another.yml"
    )
    second_path.write_text("---\nquestion: y\n")
    second = InterviewExecution(tmp_path, "docassemble.pkg:data/questions/another.yml")

    assert first.run(Start()).ok
    assert second.run(Start()).ok

    files = _session_files(tmp_path)
    assert len(files) == 2
    assert files[0].name != files[1].name


def test_concurrent_execution_operations_do_not_lose_updates(
    tmp_path, monkeypatch, da_stubs
):
    from concurrent.futures import ThreadPoolExecutor

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs)
    assert execution.run(Start()).ok

    def mutate_many(_):
        return [
            execution.run(
                Execute(
                    "counter = globals().get('counter', 0) + 1",
                    assemble=False,
                )
            )
            for _ in range(20)
        ]

    with ThreadPoolExecutor(max_workers=4) as workers:
        outcomes = list(workers.map(mutate_many, range(4)))

    assert all(outcome.ok for batch in outcomes for outcome in batch)
    assert execution.run(Evaluate("counter")).result["value"] == "80"


def test_execution_mutation_acquires_the_session_transaction_lock(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs)
    assert execution.run(Start()).ok
    acquired = []
    original_lock = execution._store.lock

    @contextmanager
    def observed_lock():
        acquired.append(True)
        with original_lock():
            yield

    monkeypatch.setattr(execution._store, "lock", observed_lock)

    outcome = execution.run(Execute("counter = 1", assemble=False))

    assert outcome.ok
    assert acquired == [True]


def test_read_only_evaluation_rehydrates_callables_without_changing_session(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs)
    assert execution.run(Start()).ok
    before = _session_bytes(tmp_path)

    result = execution.run(Evaluate("helper(12)"))

    assert result.ok and result.result["value"] == "'$12'"
    assert _session_bytes(tmp_path) == before


@pytest.mark.parametrize("source_kind", ["saved", "fresh", "snapshot", "fixture"])
def test_every_render_source_rehydrates_without_changing_the_saved_session(
    source_kind, tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs)
    assert execution.run(Start()).ok
    before = _session_bytes(tmp_path)
    template = tmp_path / "docassemble" / "pkg" / "data" / "templates" / "form.docx"
    template.parent.mkdir()
    template.write_bytes(b"docx")
    fake_docx = SimpleNamespace(_dasimulator_paragraphs=1)
    seen = []
    monkeypatch.setattr(
        "docassemble_simulator.render.prepare_docx_template", lambda path: fake_docx
    )

    def render(prepared, namespace, **kwargs):
        seen.append((namespace["helper"](7), namespace.get("fixture_ran", False)))
        return prepared

    monkeypatch.setattr("docassemble_simulator.render.render_template", render)
    renderer = InterviewRenderer(tmp_path, execution)
    if source_kind == "snapshot":
        snapshot = tmp_path / "snapshot.pkl"
        assert renderer.render(
            RenderRequest(
                "form.docx",
                FreshSource(),
                assemble=False,
                save_snapshot=snapshot,
            )
        ).ok
        source = SnapshotSource(snapshot)
    elif source_kind == "fixture":
        fixture = tmp_path / "fixture.py"
        fixture.write_text("fixture_ran = True\n")
        source = FixtureSource(fixture)
    elif source_kind == "fresh":
        source = FreshSource()
    else:
        source = SavedSessionSource()

    outcome = renderer.render(RenderRequest("form.docx", source, assemble=False))

    assert outcome.ok
    assert seen[-1][0] == "$7"
    assert seen[-1][1] is (source_kind == "fixture")
    assert _session_bytes(tmp_path) == before


def test_refresh_variables_exec_and_seek_follow_their_commit_policies(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, selected = _execution(tmp_path, monkeypatch, da_stubs)
    assert execution.run(Start()).ok
    assert execution.run(Execute("counter = 1", assemble=False)).ok

    before = _session_bytes(tmp_path)
    variables = execution.run(Variables("counter"))
    assert variables.ok and variables.result == {"counter": "1"}
    assert _session_bytes(tmp_path) == before

    executed = execution.run(Execute("counter += 1", assemble=False))
    assert executed.ok
    assert execution.run(Evaluate("counter")).result["value"] == "2"

    refreshed = execution.run(Refresh())
    assert refreshed.ok and refreshed.result["kind"] == "finished"

    class SeekingInterview(FakeInterview):
        def askfor(self, variable, *args, **kwargs):
            return {"type": "continue"}

    selected["interview"] = SeekingInterview
    before_seek = _session_bytes(tmp_path)
    isolated = execution.run(Seek("target", fresh=True))
    assert isolated.ok and _session_bytes(tmp_path) == before_seek

    activated = execution.run(Seek("target", activate=True))
    assert activated.ok
    assert execution.run(Status()).result["sought_variable"] == "target"


def test_failed_atomic_replacement_preserves_the_previous_session(
    tmp_path, monkeypatch, da_stubs
):
    import docassemble_simulator._files as file_module

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs)
    assert execution.run(Start()).ok
    assert execution.run(Execute("counter = 1", assemble=False)).ok
    before = _session_bytes(tmp_path)
    monkeypatch.setattr(
        file_module.os,
        "replace",
        lambda source, target: (_ for _ in ()).throw(OSError("disk failure")),
    )

    outcome = execution.run(Execute("counter += 1", assemble=False))

    assert not outcome.ok and outcome.error.kind == "fault"
    assert _session_bytes(tmp_path) == before
    assert not list((tmp_path / ".simulator" / "sessions").glob(".*.tmp"))


def test_date_answer_is_field_aware_and_invalid_multi_answer_rolls_back(
    tmp_path, monkeypatch, da_stubs
):
    class DateInterview(FakeInterview):
        def assemble(self, namespace, interview_status):
            interview_status.question = SimpleNamespace(
                question_type="fields",
                name=None,
                validation_code=None,
                fields=[
                    SimpleNamespace(
                        saveas="filing_date", datatype="date", required=False
                    ),
                    SimpleNamespace(saveas="caption", datatype="text", required=False),
                ],
            )
            interview_status.question_text = "Dates"
            interview_status.subquestion_text = None
            interview_status.continue_label = None
            interview_status.sought = None
            interview_status.orig_sought = None
            interview_status.selectcompute = {}

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, DateInterview)
    assert execution.run(Start()).ok

    accepted = execution.run(
        Answer((("filing_date", "2026-08-26"), ("caption", "2026-08-26")))
    )
    assert accepted.ok
    assert (
        execution.run(Evaluate("type(filing_date).__name__")).result["value"]
        == "'datetime'"
    )
    assert execution.run(Evaluate("caption")).result["value"] == "'2026-08-26'"

    empty = execution.run(Answer((("filing_date", ""), ("caption", "kept"))))
    assert empty.ok
    assert execution.run(Evaluate("filing_date")).result["value"] == "''"

    before = _session_bytes(tmp_path)
    rejected = execution.run(
        Answer((("filing_date", "2026-02-30"), ("caption", "changed")))
    )
    assert not rejected.ok and rejected.error.kind == "answer-input"
    assert _session_bytes(tmp_path) == before
    assert execution.run(Evaluate("caption")).result["value"] == "'kept'"

    coded = execution.run(Answer((("filing_date", "'2026-08-26'"),), code=True))
    assert coded.ok
    assert (
        execution.run(Evaluate("type(filing_date).__name__")).result["value"] == "'str'"
    )


class DeclaredObjectInterview(FakeInterview):
    """Defines ``rav`` on demand and asks its generic question afterwards."""

    def populate_non_pickleable(self, namespace):
        super().populate_non_pickleable(namespace)
        namespace["DAObject"] = sys.modules["docassemble.base.util"].DAObject

    def askfor(self, variable, namespace, *args, **kwargs):
        if variable == "rav":
            namespace["rav"] = namespace["DAObject"]("rav")
            return {"type": "continue", "sought": "rav", "orig_sought": "rav"}
        if "rav" not in namespace:
            raise NameError("name 'rav' is not defined")
        return {
            "type": "question",
            "sought": "x.date",
            "orig_sought": variable,
            "question": SimpleNamespace(
                question_type="fields",
                name=None,
                validation_code=None,
                fields=[
                    SimpleNamespace(saveas="x.date", datatype="date", required=False)
                ],
            ),
            "question_text": "What is the date?",
            "subquestion_text": None,
            "continue_label": None,
            "selectcompute": {},
        }


class RootQuestionInterview(FakeInterview):
    """A root that needs a screen before its attribute can be asked."""

    def askfor(self, variable, namespace, *args, **kwargs):
        return {
            "type": "question",
            "sought": "rav",
            "orig_sought": "rav",
            "question": SimpleNamespace(
                question_type="fields",
                name=None,
                validation_code=None,
                fields=[SimpleNamespace(saveas="rav", datatype="text", required=False)],
            ),
            "question_text": "Who is rav?",
            "subquestion_text": None,
            "continue_label": None,
            "selectcompute": {},
        }


def test_seek_returns_the_screen_that_defines_a_missing_root(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, RootQuestionInterview)
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    sought = execution.run(Seek("rav.date"))

    assert sought.ok
    assert sought.result["question_text"] == "Who is rav?"
    assert sought.result["orig_sought"] == "rav"


def test_seek_defines_missing_root_before_asking(tmp_path, monkeypatch, da_stubs):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, DeclaredObjectInterview
    )
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok
    before = _session_bytes(tmp_path)

    sought = execution.run(Seek("rav.date"))

    assert sought.ok
    assert sought.result["orig_sought"] == "rav.date"
    assert _session_bytes(tmp_path) == before

    activated = execution.run(Seek("rav.date", activate=True))

    assert activated.ok
    assert "rav" in execution.run(Variables("rav")).result


def _generic_date_execution(tmp_path, monkeypatch, da_stubs):
    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, GenericDateInterview)
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    return execution


def test_generic_object_answer_uses_orig_sought_for_date_coercion(
    tmp_path, monkeypatch, da_stubs
):
    execution = _generic_date_execution(tmp_path, monkeypatch, da_stubs)
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("rav.date", "2026-08-26"),)))

    assert accepted.ok
    assert (
        execution.run(Evaluate("type(rav.date).__name__")).result["value"]
        == "'datetime'"
    )

    before = _session_bytes(tmp_path)
    rejected = execution.run(Answer((("rav.date", "2026-02-30"),)))
    assert not rejected.ok and rejected.error.kind == "answer-input"
    assert _session_bytes(tmp_path) == before


def test_generic_object_resolves_every_field_through_the_sought_prefix(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, GenericMultiFieldInterview
    )
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    accepted = execution.run(
        Answer((("x.service_type", "Electronic"), ("x.date", "2026-10-14")))
    )

    assert accepted.ok
    assert execution.run(Evaluate("rav.service_type")).result["value"] == "'Electronic'"
    assert execution.run(Evaluate("rav.date.day")).result["value"] == "14"


def test_generic_object_answer_precedence_when_both_names_are_supplied(
    tmp_path, monkeypatch, da_stubs
):
    execution = _generic_date_execution(tmp_path, monkeypatch, da_stubs)
    assert execution.run(Start()).ok

    accepted = execution.run(
        Answer((("rav.date", "2026-01-01"), ("x.date", "2026-02-02")))
    )

    assert accepted.ok
    assert execution.run(Evaluate("rav.date.month")).result["value"] == "2"


def test_generic_object_validation_evaluates_resolved_targets(
    tmp_path, monkeypatch, da_stubs
):
    class RequiredDateInterview(GenericDateInterview):
        required = True

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, RequiredDateInterview)
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("rav.date", "2026-08-26"),)))

    assert accepted.ok
    assert "warnings" not in accepted.result


def test_generic_object_answer_resolves_placeholder_field_names(
    tmp_path, monkeypatch, da_stubs
):
    execution = _generic_date_execution(tmp_path, monkeypatch, da_stubs)
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("x.date", "2026-09-09"),)))

    assert accepted.ok
    assert execution.run(Evaluate("rav.date.day")).result["value"] == "9"
    assert (
        execution.run(Evaluate("globals().get('x') is None")).result["value"] == "True"
    )


def test_answer_rejects_fields_not_on_the_active_screen(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, MixedRequirementInterview
    )
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    before = _session_bytes(tmp_path)
    rejected = execution.run(
        Answer((("x.service_type", "Electronic"), ("off_screen", "1")))
    )

    assert not rejected.ok
    assert rejected.error.kind == "answer-input"
    assert any("off_screen" in item for item in rejected.error.details["errors"])
    assert _session_bytes(tmp_path) == before


def test_answer_enforces_required_fields_by_default(tmp_path, monkeypatch, da_stubs):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, MixedRequirementInterview
    )
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    before = _session_bytes(tmp_path)
    rejected = execution.run(Answer((("x.date", "2026-10-14"),)))

    assert not rejected.ok
    assert rejected.error.kind == "validation"
    assert any("x.service_type" in item for item in rejected.error.details["errors"])
    assert _session_bytes(tmp_path) == before


def test_answer_submits_blank_optional_fields(tmp_path, monkeypatch, da_stubs):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, MixedRequirementInterview
    )
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("x.service_type", "Electronic"),)))

    # The browser would have posted the blank date input as an empty string,
    # so the flow has nothing left to re-seek.
    assert accepted.ok
    assert "warnings" not in accepted.result
    assert execution.run(Evaluate("rav.date")).result["value"] == "''"


def test_answer_applies_field_defaults_like_the_browser(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, DefaultedDateInterview
    )
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("x.service_type", "Electronic"),)))

    assert accepted.ok
    assert execution.run(Evaluate("rav.date.day")).result["value"] == "15"


def test_answer_keeps_prefilled_optional_values(tmp_path, monkeypatch, da_stubs):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, PrefilledDateInterview
    )
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("x.service_type", "Electronic"),)))

    assert accepted.ok
    assert execution.run(Evaluate("rav.date")).result["value"] == "5"


def test_partial_answer_keeps_warn_only_and_skips_blank_synthesis(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, MixedRequirementInterview
    )
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("x.date", "2026-10-14"),), partial=True))

    assert accepted.ok
    assert any("x.service_type" in item for item in accepted.result["warnings"])
    undefined = execution.run(Evaluate("rav.service_type"))
    assert not undefined.ok

    strict = execution.run(
        Answer((("x.date", "2026-10-14"),), partial=True, strict=True)
    )
    assert not strict.ok
    assert strict.error.kind == "validation"


def test_answer_synthesizes_browser_boolean_and_numeric_blanks(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, BooleanStyleInterview)
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("x.service_type", "Electronic"),)))

    assert accepted.ok
    assert execution.run(Evaluate("rav.agree is False")).result["value"] == "True"
    assert execution.run(Evaluate("rav.declined is True")).result["value"] == "True"
    assert execution.run(Evaluate("rav.refer is None")).result["value"] == "True"
    assert execution.run(Evaluate("rav.count")).result["value"] == "0"
    assert execution.run(Evaluate("rav.ratio")).result["value"] == "0.0"


def test_answer_coerces_empty_submitted_values_per_datatype(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, BooleanStyleInterview)
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    accepted = execution.run(
        Answer(
            (
                ("x.service_type", "Electronic"),
                ("x.count", ""),
                ("x.ratio", ""),
                ("x.agree", ""),
            )
        )
    )

    assert accepted.ok
    assert execution.run(Evaluate("rav.count")).result["value"] == "0"
    assert execution.run(Evaluate("rav.ratio")).result["value"] == "0.0"
    assert execution.run(Evaluate("rav.agree is None")).result["value"] == "True"


def test_required_checkbox_group_needs_a_selection(tmp_path, monkeypatch, da_stubs):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, CheckboxGroupInterview
    )
    util = sys.modules["docassemble.base.util"]
    monkeypatch.setattr(util, "DAObject", PicklableStubObject)
    monkeypatch.setattr(util, "DADict", PicklableStubDict)
    assert execution.run(Start()).ok

    for raw in ('{"housing": false, "benefits": false}', "{}"):
        before = _session_bytes(tmp_path)
        rejected = execution.run(
            Answer((("x.service_type", "Electronic"), ("x.topics", raw)))
        )

        assert not rejected.ok, raw
        assert rejected.error.kind == "validation", raw
        assert any("x.topics" in item for item in rejected.error.details["errors"]), raw
        assert any(
            "at least one" in item for item in rejected.error.details["errors"]
        ), raw
        assert _session_bytes(tmp_path) == before, raw

    accepted = execution.run(
        Answer(
            (
                ("x.service_type", "Electronic"),
                ("x.topics", '{"housing": true}'),
            )
        )
    )

    assert accepted.ok, accepted.error
    topics_value = execution.run(Evaluate("rav.topics.elements"))
    assert topics_value.ok, topics_value.error
    assert topics_value.result["value"] == "{'housing': True}"
    # The optional group still takes its all-false browser blank.
    optional_value = execution.run(Evaluate("rav.optional_topics.elements"))
    assert optional_value.ok, optional_value.error
    assert optional_value.result["value"] == ("{'housing': False, 'benefits': False}")


def test_object_checkbox_append_runs_inside_the_interview_namespace(
    tmp_path, monkeypatch, da_stubs, legacy_frame_resolution
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, FrameAwareCheckboxInterview
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("shortlist", '["alice"]'),)))

    assert accepted.ok, accepted.error
    assert execution.run(Evaluate("len(shortlist)")).result["value"] == "1"
    assert execution.run(Evaluate("shortlist.gathered")).result["value"] == "True"


class OffScreenSelectionCheckboxInterview(FrameAwareCheckboxInterview):
    """A shortlist that already holds an object the screen does not offer."""

    SEEDED = ("bob",)
    CHOICES: ClassVar[dict[str, str]] = {"alice": "Alice"}


class EmptyChoiceCheckboxInterview(FakeInterview):
    """A screen whose object group resolved to no choices at all, beside a
    plain field the caller answers."""

    def assemble(self, namespace, interview_status):
        namespace["_internal"].setdefault("objselections", {})["shortlist"] = {}
        interview_status.question = SimpleNamespace(
            question_type="fields",
            name="empty shortlist",
            validation_code=None,
            fields=[
                SimpleNamespace(saveas="note", datatype="text", required=True),
                SimpleNamespace(
                    saveas="shortlist",
                    datatype="object_checkboxes",
                    required=False,
                    choices=[],
                ),
            ],
        )
        interview_status.question_text = "Anything to note?"
        interview_status.subquestion_text = None
        interview_status.continue_label = None
        interview_status.sought = "note"
        interview_status.orig_sought = "note"
        interview_status.selectcompute = {}


def test_object_checkbox_keeps_elements_the_screen_did_not_offer(
    tmp_path, monkeypatch, da_stubs, legacy_frame_resolution
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, OffScreenSelectionCheckboxInterview
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("shortlist", '["alice"]'),)))

    assert accepted.ok, accepted.error
    assert (
        execution.run(
            Evaluate("[item.instanceName for item in shortlist.elements]")
        ).result["value"]
        == "['bob', 'alice']"
    )


def test_empty_choice_object_group_is_cleared_and_gathered(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, EmptyChoiceCheckboxInterview
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("note", "'hello'"),)))

    assert accepted.ok, accepted.error
    assert execution.run(Evaluate("len(shortlist)")).result["value"] == "0"
    assert execution.run(Evaluate("shortlist.gathered")).result["value"] == "True"


class UnsubmittedChoiceCheckboxInterview(FakeInterview):
    """A screen that offers choices for a group the caller never submits."""

    def assemble(self, namespace, interview_status):
        namespace.setdefault("alice", PicklableStubObject(instanceName="alice"))
        namespace["_internal"].setdefault("objselections", {})["shortlist"] = {
            "alice": namespace["alice"]
        }
        interview_status.question = SimpleNamespace(
            question_type="fields",
            name="unsubmitted shortlist",
            validation_code=None,
            fields=[
                SimpleNamespace(saveas="memo", datatype="text", required=True),
                SimpleNamespace(
                    saveas="shortlist",
                    datatype="object_checkboxes",
                    required=False,
                    choices=[{"key": "alice", "label": "Alice"}],
                ),
            ],
        )
        interview_status.question_text = "Anything to note?"
        interview_status.subquestion_text = None
        interview_status.continue_label = None
        interview_status.sought = "memo"
        interview_status.orig_sought = "memo"
        interview_status.selectcompute = {}


def test_unsubmitted_object_group_with_choices_stays_undefined(
    tmp_path, monkeypatch, da_stubs
):
    """The boundary ADR-0014 records: only a zero-choice group is cleared.

    A visible group that rendered choices is left undefined when the caller does
    not submit it, because resolving that interacts with ADR-0013's "prefilled
    values are never overwritten" rule. This pins the boundary rather than
    asserting it is correct.
    """
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, UnsubmittedChoiceCheckboxInterview
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("memo", "'hello'"),)))

    assert accepted.ok, accepted.error
    assert not execution.run(Evaluate("shortlist")).ok


def _shortlist_names(execution):
    return execution.run(
        Evaluate("[item.instanceName for item in shortlist.elements]")
    ).result["value"]


class TwoChoiceCheckboxInterview(FrameAwareCheckboxInterview):
    """Two rendered choices, so one can be left unticked."""

    CHOICES: ClassVar[dict[str, str]] = {"alice": "Alice", "bob": "Bob"}


class ReversedOrderCheckboxInterview(TwoChoiceCheckboxInterview):
    """A list already holding both choices in the opposite order."""

    SEEDED = ("bob", "alice")


class SeededChoiceCheckboxInterview(FrameAwareCheckboxInterview):
    """A list holding an off-screen object and a choice the screen offers."""

    SEEDED = ("bob", "alice")
    CHOICES: ClassVar[dict[str, str]] = {"alice": "Alice"}


def test_object_checkbox_preserves_the_order_of_existing_selections(
    tmp_path, monkeypatch, da_stubs, legacy_frame_resolution
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, ReversedOrderCheckboxInterview
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("shortlist", '["alice", "bob"]'),)))

    assert accepted.ok, accepted.error
    # Both were already present, so neither is removed and re-appended.
    assert _shortlist_names(execution) == "['bob', 'alice']"


def test_object_checkbox_removes_an_unticked_choice(
    tmp_path, monkeypatch, da_stubs, legacy_frame_resolution
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, ReversedOrderCheckboxInterview
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("shortlist", '["alice"]'),)))

    assert accepted.ok, accepted.error
    # ``bob`` is rendered and unticked. The stub list frame-checks ``remove``,
    # so this also proves the removal ran inside the interview namespace.
    assert _shortlist_names(execution) == "['alice']"


def test_object_checkbox_empty_answer_untickes_every_rendered_choice(
    tmp_path, monkeypatch, da_stubs, legacy_frame_resolution
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, SeededChoiceCheckboxInterview
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("shortlist", "[]"),)))

    assert accepted.ok, accepted.error
    assert _shortlist_names(execution) == "['bob']"


def test_object_checkbox_leaves_an_element_alone_when_told_none(
    tmp_path, monkeypatch, da_stubs, legacy_frame_resolution
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, OffScreenSelectionCheckboxInterview
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("shortlist", '{"alice": "None"}'),)))

    assert accepted.ok, accepted.error
    # ``None`` is outside the browser's True/False pair, so the server skips the
    # key rather than ticking or unticking it.
    assert _shortlist_names(execution) == "['bob']"


def test_object_checkbox_rejects_an_unticked_unknown_choice(
    tmp_path, monkeypatch, da_stubs, legacy_frame_resolution
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, FrameAwareCheckboxInterview
    )
    assert execution.run(Start()).ok

    rejected = execution.run(Answer((("shortlist", '{"carol": false}'),)))

    assert not rejected.ok and rejected.error.kind == "answer-input"
    assert any("carol" in item for item in rejected.error.details["errors"])


class FrameAwareRadioInterview(FakeInterview):
    """An object radio screen whose assignment must run in the namespace."""

    def assemble(self, namespace, interview_status):
        namespace.setdefault("frame_probe", True)
        namespace.setdefault("alice", PicklableStubObject(instanceName="alice"))
        namespace.setdefault("selection", None)
        namespace["_internal"].setdefault("objselections", {})["selection"] = {
            "alice": namespace["alice"]
        }
        interview_status.question = SimpleNamespace(
            question_type="fields",
            name="selection",
            validation_code=None,
            fields=[
                SimpleNamespace(
                    saveas="selection",
                    datatype="object_radio",
                    required=False,
                    choices=[{"key": "alice", "label": "Alice"}],
                )
            ],
        )
        interview_status.question_text = "Pick one"
        interview_status.subquestion_text = None
        interview_status.continue_label = None
        interview_status.sought = "selection"
        interview_status.orig_sought = "selection"
        interview_status.selectcompute = {}


def test_object_radio_assigns_the_selected_object(
    tmp_path, monkeypatch, da_stubs, legacy_frame_resolution
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, FrameAwareRadioInterview
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("selection", "alice"),)))

    assert accepted.ok, accepted.error
    assert (
        execution.run(Evaluate("selection.instanceName")).result["value"] == "'alice'"
    )


def test_answer_falls_back_when_a_default_cannot_be_applied(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, BadDefaultDateInterview
    )
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("x.service_type", "Electronic"),)))

    assert accepted.ok
    assert execution.run(Evaluate("rav.date")).result["value"] == "''"


def test_answer_code_mode_rejects_off_screen_variables(tmp_path, monkeypatch, da_stubs):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, MixedRequirementInterview
    )
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    rejected = execution.run(Answer((("x.off_screen", "1"),), code=True))

    assert not rejected.ok
    assert rejected.error.kind == "answer-input"
    assert any("off_screen" in item for item in rejected.error.details["errors"])


def test_required_empty_submission_is_rejected(tmp_path, monkeypatch, da_stubs):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, MixedRequirementInterview
    )
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    for raw in ("", "None"):
        before = _session_bytes(tmp_path)
        rejected = execution.run(Answer((("x.service_type", raw),)))

        assert not rejected.ok, raw
        assert rejected.error.kind == "validation", raw
        assert any("is empty" in item for item in rejected.error.details["errors"]), raw
        assert _session_bytes(tmp_path) == before, raw


def test_hidden_fields_are_not_assigned_or_required(tmp_path, monkeypatch, da_stubs):
    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, HiddenFieldInterview)
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("x.service_type", "Electronic"),)))

    assert accepted.ok
    assert "warnings" not in accepted.result
    hidden = execution.run(Evaluate("rav.hidden"))
    assert not hidden.ok


def test_signature_fields_become_daempty(tmp_path, monkeypatch, da_stubs):
    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, SignatureInterview)
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("x.service_type", "Electronic"),)))

    assert accepted.ok
    assert "warnings" not in accepted.result
    assert execution.run(Evaluate("rav.signature.str")).result["value"] == "''"
    assert execution.run(Evaluate("str(rav.signature)")).result["value"] == "''"
