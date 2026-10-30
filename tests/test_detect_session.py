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
    ReplayAutomatic,
    SeedState,
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
from docassemble_simulator.trace import (
    TraceEntry,
    replay_expectation,
    screen_content,
    screen_identity,
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
        return DADateTime(
            parsed.year, parsed.month, parsed.day, tzinfo=datetime.timezone.utc
        )

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
        # The scan stops at the module-level frame ``exec(source, namespace)``
        # creates, whose ``f_locals`` is the live namespace dict itself, so a
        # caller that mutated the result would mutate the interview.
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


class GenericAmountInterview(FakeInterview):
    """A required generic-object integer screen: ``x.amount`` targets ``rav.amount``."""

    def assemble(self, namespace, interview_status):
        if "rav" not in namespace:
            from docassemble.base.util import DAObject

            namespace["rav"] = DAObject("rav")
        interview_status.question = SimpleNamespace(
            question_type="fields",
            name=None,
            validation_code=None,
            fields=[
                SimpleNamespace(saveas="x.amount", datatype="integer", required=True)
            ],
        )
        interview_status.question_text = "What is the amount?"
        interview_status.subquestion_text = None
        interview_status.continue_label = None
        interview_status.sought = "x.amount"
        interview_status.orig_sought = "rav.amount"
        interview_status.selectcompute = {}

    def askfor(self, variable, namespace, *args, **kwargs):
        status = args[1]
        self.assemble(namespace, status)
        return {
            "type": "question",
            "question": status.question,
            "question_text": status.question_text,
            "subquestion_text": status.subquestion_text,
            "continue_label": status.continue_label,
            "sought": status.sought,
            "orig_sought": status.orig_sought,
            "selectcompute": status.selectcompute,
        }


class OverlappingAmountInterview(FakeInterview):
    """A generic screen whose placeholder spelling collides with a resolved target.

    ``sought`` is ``x.amount`` and ``orig_sought`` is ``x.child.amount``, so the
    described field ``x.amount`` resolves through ``_screen_field_targets`` to
    ``x.child.amount`` and the optional field ``x.child.amount`` resolves to
    ``x.child.child.amount``. A submission of ``x.amount`` therefore writes to
    the required field's resolved variable, while a blank ``x.child.amount``
    submission writes one level deeper.
    """

    def assemble(self, namespace, interview_status):
        if "x" not in namespace:
            from docassemble.base.util import DAObject

            x = DAObject("x")
            x.child = DAObject("child")
            x.child.child = DAObject("child")
            namespace["x"] = x
        interview_status.question = SimpleNamespace(
            question_type="fields",
            name=None,
            validation_code=None,
            fields=[
                SimpleNamespace(saveas="x.amount", datatype="integer", required=True),
                SimpleNamespace(
                    saveas="x.child.amount", datatype="integer", required=False
                ),
            ],
        )
        interview_status.question_text = "What is the amount?"
        interview_status.subquestion_text = None
        interview_status.continue_label = None
        interview_status.sought = "x.amount"
        interview_status.orig_sought = "x.child.amount"
        interview_status.selectcompute = {}

    def askfor(self, variable, namespace, *args, **kwargs):
        status = args[1]
        self.assemble(namespace, status)
        return {
            "type": "question",
            "question": status.question,
            "question_text": status.question_text,
            "subquestion_text": status.subquestion_text,
            "continue_label": status.continue_label,
            "sought": status.sought,
            "orig_sought": status.orig_sought,
            "selectcompute": status.selectcompute,
        }


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
    """Required and optional checkbox groups beside an optional multiselect."""

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
                SimpleNamespace(
                    saveas="x.multi_topics",
                    datatype="multiselect",
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


class AutoSeekInputInterview(FakeInterview):
    """Two defaultable screens followed by a required screen needing input."""

    def assemble(self, namespace, interview_status):
        if "first" not in namespace:
            fields = [
                SimpleNamespace(
                    saveas="first", datatype="text", required=True, default="one"
                )
            ]
            sought = "first"
        elif "second" not in namespace:
            fields = [
                SimpleNamespace(
                    saveas="second", datatype="text", required=True, default="two"
                )
            ]
            sought = "second"
        else:
            fields = [
                SimpleNamespace(saveas="target", datatype="text", required=True),
                SimpleNamespace(saveas="optional", datatype="text", required=False),
            ]
            sought = "target"
        interview_status.question = SimpleNamespace(
            question_type="fields",
            name=sought,
            validation_code=None,
            fields=fields,
        )
        interview_status.question_text = sought
        interview_status.subquestion_text = None
        interview_status.continue_label = None
        interview_status.sought = sought
        interview_status.orig_sought = sought
        interview_status.selectcompute = {}

    def askfor(self, variable, namespace, *args, **kwargs):
        status = args[1]
        self.assemble(namespace, status)
        return {
            "type": "question",
            "question": status.question,
            "question_text": status.question_text,
            "subquestion_text": status.subquestion_text,
            "continue_label": status.continue_label,
            "sought": status.sought,
            "orig_sought": status.orig_sought,
            "selectcompute": status.selectcompute,
        }


class AutoSeekNonProgressingInterview(FakeInterview):
    """A walk that never defines its target remains bounded."""

    def assemble(self, namespace, interview_status):
        interview_status.question = SimpleNamespace(
            question_type="fields",
            name="step",
            validation_code=None,
            fields=[
                SimpleNamespace(
                    saveas="step", datatype="text", required=False, default="again"
                )
            ],
        )
        interview_status.question_text = "step"
        interview_status.subquestion_text = None
        interview_status.continue_label = None
        interview_status.sought = "step"
        interview_status.orig_sought = "step"
        interview_status.selectcompute = {}

    def askfor(self, variable, namespace, *args, **kwargs):
        status = args[1]
        self.assemble(namespace, status)
        return {
            "type": "question",
            "question": status.question,
            "question_text": status.question_text,
            "subquestion_text": status.subquestion_text,
            "continue_label": status.continue_label,
            "sought": status.sought,
            "orig_sought": status.orig_sought,
            "selectcompute": status.selectcompute,
        }


class AutoSeekResolvedInterview(FakeInterview):
    """A resolved target must not be submitted repeatedly."""

    def assemble(self, namespace, interview_status):
        interview_status.question = SimpleNamespace(
            question_type="fields",
            name="target",
            validation_code=None,
            fields=[
                SimpleNamespace(
                    saveas="target", datatype="text", required=True, default="done"
                )
            ],
        )
        interview_status.question_text = "target"
        interview_status.subquestion_text = None
        interview_status.continue_label = None
        interview_status.sought = "target"
        interview_status.orig_sought = "target"
        interview_status.selectcompute = {}

    def askfor(self, variable, namespace, *args, **kwargs):
        status = args[1]
        self.assemble(namespace, status)
        return {
            "type": "question",
            "question": status.question,
            "question_text": status.question_text,
            "subquestion_text": status.subquestion_text,
            "continue_label": status.continue_label,
            "sought": status.sought,
            "orig_sought": status.orig_sought,
            "selectcompute": status.selectcompute,
        }


class AutoSeekCompletionInterview(AutoSeekInputInterview):
    """A defaultable target proves auto-seek stops at completion."""

    def assemble(self, namespace, interview_status):
        if "first" not in namespace:
            return super().assemble(namespace, interview_status)
        if "target" not in namespace:
            interview_status.question = SimpleNamespace(
                question_type="fields",
                name="target",
                validation_code=None,
                fields=[
                    SimpleNamespace(
                        saveas="target", datatype="text", required=True, default="done"
                    )
                ],
            )
            interview_status.question_text = "target"
            interview_status.subquestion_text = None
            interview_status.continue_label = None
            interview_status.sought = "target"
            interview_status.orig_sought = "target"
            interview_status.selectcompute = {}
            return
        raise sys.modules["docassemble.base.error"].DAErrorNoEndpoint("finished")


class AutoSeekValidationInterview(FakeInterview):
    """Validation mutates the namespace before rejecting an auto submission."""

    questions_by_name: ClassVar[dict] = {
        "validated": SimpleNamespace(
            validation_code=(
                "marker = 'changed'\n"
                "from docassemble.base.error import DAValidationError\n"
                "raise DAValidationError('not yet')"
            )
        )
    }

    def assemble(self, namespace, interview_status):
        interview_status.question = SimpleNamespace(
            question_type="fields",
            name="validated",
            validation_code=self.questions_by_name["validated"].validation_code,
            fields=[
                SimpleNamespace(
                    saveas="value", datatype="text", required=True, default="ok"
                ),
                SimpleNamespace(saveas="optional", datatype="text", required=False),
            ],
        )
        interview_status.question_text = "validated"
        interview_status.subquestion_text = None
        interview_status.continue_label = None
        interview_status.sought = "target"
        interview_status.orig_sought = "target"
        interview_status.selectcompute = {}

    def askfor(self, variable, namespace, *args, **kwargs):
        status = args[1]
        self.assemble(namespace, status)
        return {
            "type": "question",
            "question": status.question,
            "question_text": status.question_text,
            "subquestion_text": status.subquestion_text,
            "continue_label": status.continue_label,
            "sought": status.sought,
            "orig_sought": status.orig_sought,
            "selectcompute": status.selectcompute,
        }


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


def test_unresolved_variable_names_terminal_culprit_and_expression(
    tmp_path, monkeypatch, da_stubs
):
    class DAErrorMissingVariable(Exception):
        def __init__(self, variable):
            super().__init__(f"could not define {variable}")
            self.variable = variable

    sys.modules[
        "docassemble.base.error"
    ].DAErrorMissingVariable = DAErrorMissingVariable

    question = SimpleNamespace(
        name="Question_choices",
        id="choices-question",
        question_type="fields",
        line_number=4,
        from_source=SimpleNamespace(
            path="docassemble.pkg:data/questions/main.yml",
            filepath=str(tmp_path / "source.yml"),
        ),
        source_code=(
            "---\nquestion: Pick\nfields:\n  - Choices: choice\n"
            "    choices: missing_choices\n"
        ),
        data_for_debug={"question": "Pick"},
        fields=[],
    )
    (tmp_path / "source.yml").write_text(question.source_code)

    class CulpritInterview(FakeInterview):
        def assemble(self, namespace, interview_status):
            interview_status.question = question
            interview_status.seeking = [
                {"question": question, "reason": "asking"},
                {"variable": "missing_choices"},
            ]
            raise DAErrorMissingVariable("missing_choices")

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, CulpritInterview)

    outcome = execution.run(Start())

    assert not outcome.ok
    assert outcome.error.kind == "unresolved-variable"
    culprit = outcome.error.details["culprit"]
    assert culprit["provenance"]["id"] == "choices-question"
    assert culprit["sought_variable"] == "missing_choices"
    assert "choices" in culprit["expression"]
    assert any(entry.details.get("culprit") == culprit for entry in outcome.diagnostics)


def test_terminal_seek_uses_the_block_that_referenced_the_missing_variable(
    tmp_path, monkeypatch, da_stubs
):
    class DAErrorMissingVariable(Exception):
        def __init__(self, variable):
            super().__init__(f"could not define {variable}")
            self.variable = variable

    sys.modules[
        "docassemble.base.error"
    ].DAErrorMissingVariable = DAErrorMissingVariable

    def question(identifier, source_code):
        return SimpleNamespace(
            name=f"Question_{identifier}",
            id=identifier,
            question_type="fields",
            line_number=2,
            from_source=SimpleNamespace(
                path="docassemble.pkg:data/questions/main.yml",
                filepath=str(tmp_path / f"{identifier}.yml"),
            ),
            source_code=source_code,
            data_for_debug={"question": identifier},
            fields=[],
        )

    stale = question("stale-screen", "---\nquestion: Old screen\n")
    source = question(
        "choices-source",
        "---\nquestion: Pick\nfields:\n"
        "  - Pick: choice\n    choices: missing_choices\n",
    )
    (tmp_path / "choices-source.yml").write_text(source.source_code)

    class CulpritInterview(FakeInterview):
        def assemble(self, namespace, interview_status):
            # The runtime has not populated a new screen when a choices
            # expression exhausts seeking; this is the previous screen.
            interview_status.question = stale
            interview_status.seeking = [
                {"question": stale, "reason": "asking"},
                {"question": source, "reason": "mandatory question"},
                {"variable": "missing_choices"},
            ]
            error = DAErrorMissingVariable("missing_choices")
            error.interview_status = interview_status
            error.interview = self
            raise error

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, CulpritInterview)

    outcome = execution.run(Start())

    assert not outcome.ok
    assert outcome.error.kind == "unresolved-variable"
    culprit = outcome.error.details["culprit"]
    assert culprit["provenance"]["id"] == "choices-source"
    assert culprit["sought_variable"] == "missing_choices"
    assert "missing_choices" in culprit["expression"]
    assert outcome.diagnostics[-1].details["culprit"] == culprit


def test_terminal_expression_matches_the_full_nested_collection_path(
    tmp_path, monkeypatch, da_stubs
):
    class DAErrorMissingVariable(Exception):
        def __init__(self, variable):
            super().__init__(f"could not define {variable}")
            self.variable = variable

    sys.modules[
        "docassemble.base.error"
    ].DAErrorMissingVariable = DAErrorMissingVariable
    question = SimpleNamespace(
        name="Question_collection",
        id="collection-source",
        question_type="code",
        line_number=2,
        from_source=SimpleNamespace(
            path="docassemble.pkg:data/questions/main.yml",
            filepath=str(tmp_path / "collection-source.yml"),
        ),
        source_code=(
            "---\ncode: |\n"
            "  first = M.children[1].name\n"
            "  second = M.children[0].name\n"
        ),
        data_for_debug={},
        fields=[],
    )

    class CulpritInterview(FakeInterview):
        def assemble(self, namespace, interview_status):
            interview_status.seeking = [
                {"question": question, "reason": "running"},
                {"variable": "M.children[0].name"},
            ]
            raise DAErrorMissingVariable("M.children[0].name")

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, CulpritInterview)
    outcome = execution.run(Start())

    assert not outcome.ok
    assert outcome.error.details["culprit"]["provenance"]["id"] == "collection-source"
    assert (
        outcome.error.details["culprit"]["expression"] == "second = M.children[0].name"
    )


def test_terminal_expression_returns_no_precision_for_dynamic_collection_index(
    tmp_path, monkeypatch, da_stubs
):
    class DAErrorMissingVariable(Exception):
        def __init__(self, variable):
            super().__init__(f"could not define {variable}")
            self.variable = variable

    sys.modules[
        "docassemble.base.error"
    ].DAErrorMissingVariable = DAErrorMissingVariable
    question = SimpleNamespace(
        name="Question_dynamic_collection",
        id="dynamic-collection",
        question_type="code",
        line_number=2,
        from_source=SimpleNamespace(
            path="docassemble.pkg:data/questions/main.yml",
            filepath=str(tmp_path / "dynamic-collection.yml"),
        ),
        source_code="---\ncode: |\n  result = M.children[index].name\n",
        data_for_debug={},
        fields=[],
    )

    class CulpritInterview(FakeInterview):
        def assemble(self, namespace, interview_status):
            interview_status.seeking = [
                {"question": question, "reason": "running"},
                {"variable": "M.children[0].name"},
            ]
            raise DAErrorMissingVariable("M.children[0].name")

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, CulpritInterview)
    outcome = execution.run(Start())

    assert not outcome.ok
    assert outcome.error.details["culprit"]["provenance"]["id"] == "dynamic-collection"
    assert outcome.error.details["culprit"]["expression"] is None


def test_terminal_generic_expression_matches_the_resolved_object_suffix(
    tmp_path, monkeypatch, da_stubs
):
    class DAErrorMissingVariable(Exception):
        def __init__(self, variable):
            super().__init__(f"could not define {variable}")
            self.variable = variable

    sys.modules[
        "docassemble.base.error"
    ].DAErrorMissingVariable = DAErrorMissingVariable
    question = SimpleNamespace(
        name="Question_generic",
        id="generic-source",
        question_type="code",
        is_generic=True,
        generic_object="DAObject",
        line_number=2,
        from_source=SimpleNamespace(
            path="docassemble.pkg:data/questions/main.yml",
            filepath=str(tmp_path / "generic-source.yml"),
        ),
        source_code="---\ngeneric object: DAObject\ncode: |\n  result = x.cos.date\n",
        data_for_debug={},
        fields=[],
    )

    class CulpritInterview(FakeInterview):
        def assemble(self, namespace, interview_status):
            interview_status.seeking = [
                {"question": question, "reason": "running"},
                {"variable": "rav.cos.date"},
            ]
            raise DAErrorMissingVariable("rav.cos.date")

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, CulpritInterview)
    outcome = execution.run(Start())

    assert not outcome.ok
    assert outcome.error.details["culprit"]["expression"] == "result = x.cos.date"


def test_terminal_seek_without_matching_history_omits_stale_screen_culprit(
    tmp_path, monkeypatch, da_stubs
):
    class DAErrorMissingVariable(Exception):
        def __init__(self, variable):
            super().__init__(f"could not define {variable}")
            self.variable = variable

    sys.modules[
        "docassemble.base.error"
    ].DAErrorMissingVariable = DAErrorMissingVariable
    stale = SimpleNamespace(
        name="Question_stale",
        id="stale-screen",
        question_type="fields",
        source_code="---\nquestion: Stale\n",
        data_for_debug={},
        fields=[],
    )

    class CulpritInterview(FakeInterview):
        def assemble(self, namespace, interview_status):
            interview_status.question = stale
            interview_status.seeking = [{"variable": "missing_terminal"}]
            raise DAErrorMissingVariable("missing_terminal")

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, CulpritInterview)
    outcome = execution.run(Start())

    assert not outcome.ok
    assert outcome.error.kind == "unresolved-variable"
    assert "could not define missing_terminal" in outcome.error.message
    assert outcome.error.details["sought_variable"] == "missing_terminal"
    assert "culprit" not in outcome.error.details


def test_nested_terminal_seek_uses_the_last_referencing_block(
    tmp_path, monkeypatch, da_stubs
):
    class DAErrorMissingVariable(Exception):
        def __init__(self, variable):
            super().__init__(f"could not define {variable}")
            self.variable = variable

    sys.modules[
        "docassemble.base.error"
    ].DAErrorMissingVariable = DAErrorMissingVariable

    def question(identifier, source_code):
        return SimpleNamespace(
            name=f"Question_{identifier}",
            id=identifier,
            question_type="code",
            line_number=2,
            from_source=SimpleNamespace(
                path="docassemble.pkg:data/questions/main.yml",
                filepath=str(tmp_path / f"{identifier}.yml"),
            ),
            source_code=source_code,
            data_for_debug={"question": identifier},
            fields=[],
        )

    stale = question("stale-screen", "---\nquestion: Old screen\n")
    first = question(
        "first-reference",
        "---\ncode: |\n  first = missing_first\n",
    )
    terminal = question(
        "terminal-reference",
        "---\ncode: |\n  allowed = known\n  result = missing_terminal\n",
    )
    for item in (first, terminal):
        (tmp_path / f"{item.id}.yml").write_text(item.source_code)

    class CulpritInterview(FakeInterview):
        def assemble(self, namespace, interview_status):
            interview_status.question = stale
            interview_status.seeking = [
                {"question": stale, "reason": "asking"},
                {"question": first, "reason": "considering"},
                {"variable": "missing_first"},
                {"question": terminal, "reason": "running"},
                {"variable": "missing_terminal"},
            ]
            error = DAErrorMissingVariable("missing_terminal")
            error.interview_status = interview_status
            raise error

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, CulpritInterview)

    outcome = execution.run(Start())

    assert not outcome.ok
    culprit = outcome.error.details["culprit"]
    assert culprit["provenance"]["id"] == "terminal-reference"
    assert culprit["expression"] == "result = missing_terminal"
    assert all(
        entry.details.get("culprit", {}).get("provenance", {}).get("id")
        == "terminal-reference"
        for entry in outcome.diagnostics
        if "culprit" in entry.details
    )


def test_tolerated_seek_is_not_reused_for_a_later_terminal_failure(
    tmp_path, monkeypatch, da_stubs
):
    class DAErrorMissingVariable(Exception):
        def __init__(self, variable):
            super().__init__(f"could not define {variable}")
            self.variable = variable

    sys.modules[
        "docassemble.base.error"
    ].DAErrorMissingVariable = DAErrorMissingVariable

    tolerated = SimpleNamespace(
        name="review_item",
        id="review-item",
        question_type="review",
        source_code="---\nquestion: Review\n",
        data_for_debug={},
        fields=[],
    )
    terminal = SimpleNamespace(
        name="choices_question",
        id="actual-terminal",
        question_type="fields",
        source_code="---\nfields:\n  - Pick: choice\n    choices: missing_terminal\n",
        data_for_debug={},
        fields=[],
    )

    class CulpritInterview(FakeInterview):
        def assemble(self, namespace, interview_status):
            interview_status.question = tolerated
            interview_status.seeking = [
                {"question": tolerated, "reason": "review item"},
                {"variable": "review_item", "reason": "skipped review item"},
                {"question": terminal, "reason": "mandatory question"},
                {"variable": "missing_terminal"},
            ]
            error = DAErrorMissingVariable("missing_terminal")
            error.interview_status = interview_status
            raise error

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, CulpritInterview)

    outcome = execution.run(Start())

    culprit = outcome.error.details["culprit"]
    assert culprit["provenance"]["id"] == "actual-terminal"
    assert culprit["sought_variable"] == "missing_terminal"
    assert all(
        item.details.get("culprit", {}).get("provenance", {}).get("id") != "review-item"
        for item in outcome.diagnostics
        if "culprit" in item.details
    )


def test_tolerated_lazy_reference_does_not_name_a_culprit(
    tmp_path, monkeypatch, da_stubs
):
    class ToleratedInterview(FakeInterview):
        def assemble(self, namespace, interview_status):
            interview_status.seeking = [
                {"variable": "review_item", "reason": "skipped review item"}
            ]
            try:
                eval("missing_review_reference", {}, {})
            except NameError:
                pass
            raise sys.modules["docassemble.base.error"].DAErrorNoEndpoint("finished")

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, ToleratedInterview)

    outcome = execution.run(Start())

    assert outcome.ok
    assert outcome.diagnostics
    assert all("culprit" not in item.details for item in outcome.diagnostics)


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


@pytest.mark.parametrize("operation_type", [Seek, ReplayAutomatic])
@pytest.mark.parametrize("activate", [False, True])
def test_explicit_exhausted_seek_uses_unresolved_variable_failure(
    tmp_path, monkeypatch, da_stubs, activate, operation_type
):
    class DAErrorMissingVariable(RuntimeError):
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

    outcome = execution.run(operation_type("M.unknown", activate=activate))

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


@pytest.mark.parametrize("operation_type", [Seek, ReplayAutomatic])
@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("activate", [False, True])
def test_seek_and_replay_prepare_working_state_and_only_activate_on_request(
    tmp_path, monkeypatch, da_stubs, operation_type, fresh, activate
):
    class SeekingInterview(FakeInterview):
        def askfor(self, variable, namespace, *args, **kwargs):
            namespace["walk_origin"] = namespace.get("counter", "fresh")
            return {
                "type": "resolved",
                "origin": namespace["walk_origin"],
                "prepared": namespace["helper"](7),
            }

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, SeekingInterview)
    assert execution.run(Start()).ok
    assert execution.run(Execute("counter = 42", assemble=False)).ok
    before = _session_bytes(tmp_path)

    outcome = execution.run(operation_type("target", fresh=fresh, activate=activate))

    assert outcome.ok
    assert outcome.result["raw"] == {
        "type": "resolved",
        "origin": "fresh" if fresh else "42",
        "prepared": "$7",
    }
    if activate:
        assert _session_bytes(tmp_path) != before
        assert execution.run(Status()).result == outcome.result
        assert execution.run(Evaluate("walk_origin")).result["value"] == (
            "'fresh'" if fresh else "42"
        )
        assert execution.run(Refresh()).result["sought_variable"] == "target"
    else:
        assert _session_bytes(tmp_path) == before


@pytest.mark.parametrize("operation_type", [Seek, ReplayAutomatic])
@pytest.mark.parametrize("activate", [False, True])
def test_seek_and_replay_require_saved_state_unless_fresh(
    tmp_path, monkeypatch, da_stubs, operation_type, activate
):
    class SeekingInterview(FakeInterview):
        def askfor(self, *args, **kwargs):
            return {"type": "continue"}

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, SeekingInterview)

    missing = execution.run(operation_type("target", activate=activate))

    assert not missing.ok
    assert missing.error.kind == "state"
    assert missing.error.message == "no saved session; run `start` first"
    assert _session_bytes(tmp_path) == {}

    fresh = execution.run(operation_type("target", fresh=True, activate=activate))

    assert fresh.ok
    assert bool(_session_bytes(tmp_path)) is activate


@pytest.mark.parametrize("operation_type", [Seek, ReplayAutomatic])
@pytest.mark.parametrize("activate", [False, True])
def test_seek_and_replay_translate_error_shaped_outcomes(
    tmp_path, monkeypatch, da_stubs, operation_type, activate
):
    class BrokenInterview(FakeInterview):
        def askfor(self, *args, **kwargs):
            return {"type": "error"}

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, BrokenInterview)
    assert execution.run(Start()).ok
    before = _session_bytes(tmp_path)

    outcome = execution.run(operation_type("target", activate=activate))

    assert not outcome.ok
    assert outcome.error.kind == "execution"
    assert outcome.error.message == "interview assembly failed"
    assert outcome.error.details["kind"] == "error"
    if activate:
        assert execution.run(Status()).result == outcome.error.details
    else:
        assert _session_bytes(tmp_path) == before


@pytest.mark.parametrize("operation_type", [Seek, ReplayAutomatic])
@pytest.mark.parametrize("activate", [False, True])
@pytest.mark.parametrize("error_type", [RuntimeError, AssertionError])
def test_seek_and_replay_classify_runtime_exceptions(
    tmp_path, monkeypatch, da_stubs, operation_type, activate, error_type
):
    class BrokenInterview(FakeInterview):
        def askfor(self, *args, **kwargs):
            raise error_type("walk broke")

    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, BrokenInterview)
    assert execution.run(Start()).ok
    before = _session_bytes(tmp_path)

    if error_type is AssertionError:
        with pytest.raises(AssertionError, match="walk broke"):
            execution.run(operation_type("target", activate=activate))
    else:
        outcome = execution.run(operation_type("target", activate=activate))
        assert not outcome.ok
        # Activated operations retain _mutate/run's fault classification.
        assert outcome.error.kind == ("fault" if activate else "seek")
        assert outcome.error.message == "RuntimeError: walk broke"
    assert _session_bytes(tmp_path) == before


def test_auto_seek_walks_defaults_and_stops_at_the_first_required_screen(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, AutoSeekInputInterview
    )
    assert execution.run(Start()).ok

    sought = execution.run(Seek("target", auto=True))

    assert sought.ok
    assert [item["question_name"] for item in sought.result["auto_submitted"]] == [
        "first",
        "second",
    ]
    assert sought.result["auto_stop"]["reason"] == "required fields missing"
    assert sought.result["auto_stop"]["missing_required"] == ["target"]
    assert sought.result["question_name"] == "target"


def test_auto_seek_reports_a_non_progressing_walk_as_bounded(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, AutoSeekNonProgressingInterview
    )
    assert execution.run(Start()).ok

    sought = execution.run(Seek("target", auto=True))

    assert sought.ok
    assert len(sought.result["auto_submitted"]) == 100
    assert sought.result["auto_stop"] == {
        "reason": "auto-advance limit reached",
        "limit": 100,
    }


def test_auto_seek_stops_when_the_target_resolves_without_reasking_it(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, AutoSeekResolvedInterview
    )
    assert execution.run(Start()).ok

    sought = execution.run(Seek("target", auto=True))

    assert sought.ok
    assert len(sought.result["auto_submitted"]) == 1
    assert sought.result["auto_stop"] == {"reason": "sought variable resolved"}


def test_auto_seek_stops_at_completion_after_submitting_the_target(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, AutoSeekCompletionInterview
    )
    assert execution.run(Start()).ok

    sought = execution.run(Seek("target", auto=True))

    assert sought.ok
    assert [item["question_name"] for item in sought.result["auto_submitted"]] == [
        "first",
        "target",
    ]
    assert sought.result["auto_stop"] == {"reason": "interview completed"}
    assert sought.result["kind"] == "finished"


def test_auto_seek_rolls_back_rejected_defaults_and_validation_mutations(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, AutoSeekValidationInterview
    )
    assert execution.run(Start()).ok
    before = _session_bytes(tmp_path)

    rejected = execution.run(Seek("target", auto=True, activate=True))

    assert rejected.ok
    assert rejected.result["auto_submitted"] == []
    assert rejected.result["auto_stop"]["reason"] == "validation failed"
    assert execution.run(Evaluate("'value' in globals()")).result["value"] == "False"
    assert execution.run(Evaluate("'optional' in globals()")).result["value"] == "False"
    assert execution.run(Evaluate("'marker' in globals()")).result["value"] == "False"
    assert _session_bytes(tmp_path) != before  # activation stores the final seek


def test_auto_seek_required_rejection_rolls_back_optional_blanks(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, AutoSeekInputInterview
    )
    assert execution.run(Start()).ok

    rejected = execution.run(Seek("target", auto=True, activate=True))

    assert rejected.ok
    assert rejected.result["auto_stop"]["reason"] == "required fields missing"
    assert execution.run(Evaluate("'first' in globals()")).result["value"] == "True"
    assert execution.run(Evaluate("'second' in globals()")).result["value"] == "True"
    assert execution.run(Evaluate("'optional' in globals()")).result["value"] == "False"


def test_auto_seek_read_only_rejection_keeps_the_saved_session_unchanged(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, AutoSeekInputInterview
    )
    assert execution.run(Start()).ok
    before = _session_bytes(tmp_path)

    rejected = execution.run(Seek("target", auto=True))

    assert rejected.ok
    assert _session_bytes(tmp_path) == before


def test_state_seed_is_explicit_and_persists_without_assembly(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs)
    assert execution.run(Start()).ok

    seeded = execution.run(SeedState("counter = 7"))

    assert seeded.ok
    assert seeded.result == {
        "kind": "state-seeded",
        "state_seeded": True,
        "operation": "state-seed",
    }
    assert execution.run(Status()).result == seeded.result


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


def test_generic_object_resolved_target_blank_fails_required_gate(
    tmp_path, monkeypatch, da_stubs
):
    """An empty submission through the resolved target is explicit blank intent.

    The caller named ``rav.amount``, the screen's resolved target, so the
    required gate must see the blank even though the field variable is the
    generic placeholder ``x.amount``.
    """
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, GenericAmountInterview
    )
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    before = _session_bytes(tmp_path)
    rejected = execution.run(Answer((("rav.amount", ""),)))

    assert not rejected.ok
    assert rejected.error.kind == "validation"
    assert rejected.error.details["missing_required"] == ["x.amount"]
    assert "is empty" in rejected.error.details["errors"][0]
    assert _session_bytes(tmp_path) == before


def test_generic_object_placeholder_blank_still_fails_required_gate(
    tmp_path, monkeypatch, da_stubs
):
    """The placeholder spelling keeps rejecting an explicit blank (regression)."""
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, GenericAmountInterview
    )
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    before = _session_bytes(tmp_path)
    rejected = execution.run(Answer((("x.amount", ""),)))

    assert not rejected.ok
    assert rejected.error.kind == "validation"
    assert rejected.error.details["missing_required"] == ["x.amount"]
    assert "is empty" in rejected.error.details["errors"][0]
    assert _session_bytes(tmp_path) == before


def test_generic_object_resolved_target_explicit_zero_is_accepted(
    tmp_path, monkeypatch, da_stubs
):
    """An explicit ``0`` through the resolved target is not blank intent."""
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, GenericAmountInterview
    )
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("rav.amount", "0"),)))

    assert accepted.ok
    assert execution.run(Evaluate("rav.amount")).result["value"] == "0"


def test_generic_object_replay_override_naming_resolved_target_stops_required(
    tmp_path, monkeypatch, da_stubs
):
    """A replay override that names the resolved target carries blank intent too."""
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, GenericAmountInterview
    )
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok
    generic = execution.run(Seek("rav.amount")).result
    assert generic["sought"] == "x.amount"
    expected = replay_expectation(
        TraceEntry(
            seq=1,
            operation="answer",
            phase=None,
            submitted={},
            ok=False,
            screen=generic,
            identity=screen_identity(generic),
            content=screen_content(generic),
            error={"kind": "validation"},
        )
    )

    replay = execution.run(
        ReplayAutomatic(
            "rav.amount",
            submissions=((("rav.amount", ""),),),
            activate=True,
            expectations=(expected,),
        )
    )

    assert replay.ok
    assert replay.result["auto_trace"][0]["ok"] is False
    assert "replay_divergence" not in replay.result
    assert replay.result["auto_stop"]["reason"] == "required fields missing"
    assert replay.result["auto_stop"]["missing_required"] == ["x.amount"]
    assert execution.run(Evaluate("'amount' in vars(rav)")).result["value"] == "False"


def test_generic_object_blank_name_colliding_with_another_fields_target_is_not_blank(
    tmp_path, monkeypatch, da_stubs
):
    """A blank submission blanks only the field it writes to.

    ``x.child.amount`` is the resolved target of the required field
    ``x.amount``, so the old ``targets.get(variable) in blank_submissions``
    check read the *other* field's blank submission as if it blanked this
    field. The blank actually writes to ``x.child.child.amount``, leaving the
    required field's resolved value at 5; the submission must be accepted.
    """
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, OverlappingAmountInterview
    )
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("x.amount", "5"), ("x.child.amount", ""))))

    assert accepted.ok
    assert execution.run(Evaluate("x.child.amount")).result["value"] == "5"
    assert execution.run(Evaluate("x.child.child.amount")).result["value"] == "0"


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
                ("x.multi_topics", '{"housing": true}'),
            )
        )
    )

    assert accepted.ok, accepted.error
    topics_value = execution.run(Evaluate("rav.topics.elements"))
    assert topics_value.ok, topics_value.error
    # The submission omits ``benefits``; a browser posts it unticked, so the
    # group carries an explicit False rather than an undefined key.
    assert topics_value.result["value"] == "{'housing': True, 'benefits': False}"
    multi_value = execution.run(Evaluate("rav.multi_topics.elements"))
    assert multi_value.ok, multi_value.error
    assert multi_value.result["value"] == "{'housing': True, 'benefits': False}"
    # The optional group still takes its all-false browser blank.
    optional_value = execution.run(Evaluate("rav.optional_topics.elements"))
    assert optional_value.ok, optional_value.error
    assert optional_value.result["value"] == ("{'housing': False, 'benefits': False}")


class ReviewInterview(FakeInterview):
    """A review whose continue button is the only way to the next screen."""

    def assemble(self, namespace, interview_status):
        if namespace.get("review_ok"):
            interview_status.question = SimpleNamespace(
                question_type="fields",
                name="done",
                validation_code=None,
                fields=[],
            )
            interview_status.question_text = "Done"
        else:
            interview_status.question = SimpleNamespace(
                question_type="review",
                name="structured review",
                validation_code=None,
                review_saveas="review_ok",
                fields=[],
            )
            interview_status.question_text = "Review"
        interview_status.subquestion_text = None
        interview_status.continue_label = None
        interview_status.sought = None
        interview_status.orig_sought = None
        interview_status.selectcompute = {}


def test_review_continue_field_is_answerable_and_defaultable(
    tmp_path, monkeypatch, da_stubs
):
    """The continue button answers True, and --defaults presses it (#55)."""
    execution, _, _ = _execution(tmp_path, monkeypatch, da_stubs, ReviewInterview)

    started = execution.run(Start())
    assert started.ok, started.error
    assert started.result["question_type"] == "review", started.result
    assert started.result["fields"] == [
        {
            "variable": "review_ok",
            "type": "settrue",
            "visible": True,
            "required": False,
            "example": "review_ok=True",
        }
    ], started.result["fields"]

    answered = execution.run(Answer((("review_ok", "True"),)))
    assert answered.ok, answered.error
    assert answered.result["question_text"] == "Done", answered.result

    execution.reset()
    assert execution.run(Start()).ok
    defaults = execution.run(Answer((), defaults=True))
    assert defaults.ok, defaults.error
    assert defaults.result["question_text"] == "Done", defaults.result


class ContinueButtonInterview(FakeInterview):
    question_type = "fields"

    def assemble(self, namespace, interview_status):
        interview_status.question = SimpleNamespace(
            question_type=self.question_type,
            name="consent",
            validation_code=None,
            fields_saveas="consent_ok",
            fields=[
                SimpleNamespace(saveas="consent", datatype="yesno", required=False)
            ],
        )
        interview_status.question_text = (
            "Done" if namespace.get("consent_ok") else "Consent"
        )
        interview_status.subquestion_text = None
        interview_status.continue_label = None
        interview_status.sought = "consent_ok"
        interview_status.orig_sought = "consent_ok"
        interview_status.selectcompute = {}


@pytest.mark.parametrize(
    "question_type",
    ["fields", "yesno", "noyes", "yesnomaybe", "noyesmaybe", "signature"],
)
def test_continue_button_is_answerable_and_defaultable(
    tmp_path, monkeypatch, da_stubs, question_type
):
    monkeypatch.setattr(ContinueButtonInterview, "question_type", question_type)
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, ContinueButtonInterview
    )
    started = execution.run(Start())
    assert started.ok, started.error
    assert started.result["fields"][-1] == {
        "variable": "consent_ok",
        "type": "settrue",
        "visible": True,
        "required": False,
        "example": "consent_ok=True",
    }
    partial = execution.run(Answer((("consent", "True"),), partial=True))
    assert partial.ok, partial.error
    assert partial.result["question_text"] != "Done"
    undefined = execution.run(Evaluate("'consent_ok' in locals()"))
    assert undefined.ok, undefined.error
    assert undefined.result["value"] == "False"
    explicit = execution.run(Answer((("consent_ok", "True"),)))
    assert explicit.ok, explicit.error
    assert explicit.result["question_text"] == "Done"

    execution.reset()
    assert execution.run(Start()).ok
    ordinary = execution.run(Answer((("consent", "True"),)))
    assert ordinary.ok, ordinary.error
    assert ordinary.result["question_text"] == "Done"

    execution.reset()
    assert execution.run(Start()).ok
    defaults = execution.run(Answer((), defaults=True))
    assert defaults.ok, defaults.error
    assert defaults.result["question_text"] == "Done"


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

    REQUIRED: ClassVar[bool] = False

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
                    required=self.REQUIRED,
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


class RequiredEmptyChoiceCheckboxInterview(EmptyChoiceCheckboxInterview):
    """A zero-choice object group that is also a required field.

    A field with no rendered choices cannot be selected at all; the server
    clears and gathers it whether or not it is required, so the answer path
    must not reject it for having nothing to tick.
    """

    REQUIRED = True


def test_required_empty_choice_object_group_is_cleared_and_gathered(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, RequiredEmptyChoiceCheckboxInterview
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


class GenericObjectCheckboxInterview(FakeInterview):
    """A generic-object screen whose group lives under the placeholder root.

    The field is posted as ``x.shortlist`` while the resolved target is
    ``rav.shortlist``, so every object mutation must land on ``rav``.
    """

    CHOICES: ClassVar[dict[str, str]] = {}

    def assemble(self, namespace, interview_status):
        if "rav" not in namespace:
            from docassemble.base.util import DAObject

            namespace["rav"] = DAObject("rav")
        namespace["_internal"].setdefault("objselections", {})["x.shortlist"] = {
            key: namespace[key] for key in self.CHOICES
        }
        interview_status.question = SimpleNamespace(
            question_type="fields",
            name=None,
            validation_code=None,
            fields=[
                SimpleNamespace(
                    saveas="x.shortlist",
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
        interview_status.sought = "x.shortlist"
        interview_status.orig_sought = "rav.shortlist"
        interview_status.selectcompute = {}


def test_empty_choice_object_group_lands_on_the_resolved_target(
    tmp_path, monkeypatch, da_stubs
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, GenericObjectCheckboxInterview
    )
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("x.shortlist", "[]"),)))

    assert accepted.ok, accepted.error
    assert execution.run(Evaluate("len(rav.shortlist)")).result["value"] == "0"
    assert execution.run(Evaluate("rav.shortlist.gathered")).result["value"] == "True"
    assert (
        execution.run(Evaluate("globals().get('x') is None")).result["value"] == "True"
    )


class GenericObjectChoiceCheckboxInterview(GenericObjectCheckboxInterview):
    """The same generic screen with one rendered choice."""

    CHOICES: ClassVar[dict[str, str]] = {"alice": "Alice"}

    def assemble(self, namespace, interview_status):
        namespace.setdefault("alice", PicklableStubObject(instanceName="alice"))
        super().assemble(namespace, interview_status)


def test_object_choice_lands_on_the_resolved_target(tmp_path, monkeypatch, da_stubs):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, GenericObjectChoiceCheckboxInterview
    )
    monkeypatch.setattr(
        sys.modules["docassemble.base.util"], "DAObject", PicklableStubObject
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("x.shortlist", '["alice"]'),)))

    assert accepted.ok, accepted.error
    assert (
        execution.run(
            Evaluate("[item.instanceName for item in rav.shortlist.elements]")
        ).result["value"]
        == "['alice']"
    )
    assert execution.run(Evaluate("rav.shortlist.gathered")).result["value"] == "True"
    assert (
        execution.run(Evaluate("globals().get('x') is None")).result["value"] == "True"
    )


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


def test_object_checkbox_empty_answer_unticks_every_rendered_choice(
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


class PredefinedEmptyChoiceCheckboxInterview(FakeInterview):
    """A zero-choice group whose list already exists and only accepts
    mutations from inside the interview namespace."""

    def assemble(self, namespace, interview_status):
        namespace.setdefault("frame_probe", True)
        if "shortlist" not in namespace:
            namespace["shortlist"] = NamespaceAwareList(
                [PicklableStubObject(instanceName="bob")]
            )
        namespace["_internal"].setdefault("objselections", {})["shortlist"] = {}
        interview_status.question = SimpleNamespace(
            question_type="fields",
            name="empty shortlist",
            validation_code=None,
            fields=[
                SimpleNamespace(
                    saveas="shortlist",
                    datatype="object_checkboxes",
                    required=False,
                    choices=[],
                )
            ],
        )
        interview_status.question_text = "Who belongs on the shortlist?"
        interview_status.subquestion_text = None
        interview_status.continue_label = None
        interview_status.sought = "shortlist"
        interview_status.orig_sought = "shortlist"
        interview_status.selectcompute = {}


def test_empty_choice_object_group_clears_inside_the_interview_namespace(
    tmp_path, monkeypatch, da_stubs, legacy_frame_resolution
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, PredefinedEmptyChoiceCheckboxInterview
    )
    assert execution.run(Start()).ok

    accepted = execution.run(Answer((("shortlist", "[]"),)))

    assert accepted.ok, accepted.error
    assert execution.run(Evaluate("len(shortlist)")).result["value"] == "0"
    assert execution.run(Evaluate("shortlist.gathered")).result["value"] == "True"


def test_object_checkbox_rejects_a_ticked_unknown_choice(
    tmp_path, monkeypatch, da_stubs, legacy_frame_resolution
):
    execution, _, _ = _execution(
        tmp_path, monkeypatch, da_stubs, FrameAwareCheckboxInterview
    )
    assert execution.run(Start()).ok

    rejected = execution.run(Answer((("shortlist", '{"carol": true}'),)))

    assert not rejected.ok and rejected.error.kind == "answer-input"
    assert any("carol" in item for item in rejected.error.details["errors"])


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
