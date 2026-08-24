import pickle
import sys
import types
from types import SimpleNamespace

import pytest

from docassemble_simulator.detect import guess_main_interview, resolve_interview
from docassemble_simulator.session import Session


class TestGuessMainInterview:
    def test_prefers_top_level_main(self):
        interviews = [
            "docassemble.pkg:data/questions/intake.yml",
            "docassemble.pkg:data/questions/main.yml",
        ]
        assert (
            guess_main_interview(interviews)
            == "docassemble.pkg:data/questions/main.yml"
        )

    def test_accepts_main_yaml_spelling(self):
        interviews = [
            "docassemble.pkg:data/questions/main.yaml",
            "docassemble.pkg:data/questions/other.yml",
        ]
        assert (
            guess_main_interview(interviews)
            == "docassemble.pkg:data/questions/main.yaml"
        )

    def test_falls_back_to_nested_main_before_first(self):
        interviews = [
            "docassemble.pkg:data/questions/sub/main.yml",
            "docassemble.pkg:data/questions/aaa.yml",
        ]
        assert (
            guess_main_interview(interviews)
            == "docassemble.pkg:data/questions/sub/main.yml"
        )

    def test_no_main_returns_first(self):
        interviews = [
            "docassemble.pkg:data/questions/b.yml",
            "docassemble.pkg:data/questions/a.yml",
        ]
        assert guess_main_interview(interviews) == "docassemble.pkg:data/questions/b.yml"

    def test_empty_returns_none(self):
        assert guess_main_interview([]) is None


@pytest.fixture
def pkg_root(tmp_path):
    for pkg in ("pkg1", "pkg2"):
        pkg_dir = tmp_path / "docassemble" / pkg
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("")
        qdir = pkg_dir / "data" / "questions"
        qdir.mkdir(parents=True)
        (qdir / "main.yml").write_text("---\nquestion: x\n")
        (qdir / "shared.yml").write_text("---\nquestion: y\n")
    return tmp_path


class TestResolveInterview:
    def test_auto_detect_picks_main(self, pkg_root):
        chosen, all_interviews = resolve_interview(pkg_root)
        assert chosen == "docassemble.pkg1:data/questions/main.yml"
        assert len(all_interviews) == 4

    def test_unique_filename_resolves_across_suffix(self, pkg_root):
        (pkg_root / "docassemble" / "pkg1" / "data" / "questions" / "only1.yml").write_text("---")
        chosen, _ = resolve_interview(pkg_root, "only1.yml")
        assert chosen == "docassemble.pkg1:data/questions/only1.yml"

    def test_ambiguous_filename_raises_with_candidates(self, pkg_root):
        with pytest.raises(SystemExit) as exc:
            resolve_interview(pkg_root, "shared.yml")
        message = str(exc.value)
        assert "ambiguous" in message
        assert "pkg1:data/questions/shared.yml" in message
        assert "pkg2:data/questions/shared.yml" in message

    def test_full_reference_passthrough(self, pkg_root):
        chosen, all_interviews = resolve_interview(
            pkg_root, "docassemble.pkg2:data/questions/shared.yml"
        )
        assert chosen == "docassemble.pkg2:data/questions/shared.yml"
        assert len(all_interviews) == 4

    def test_unknown_name_raises_listing_available(self, pkg_root):
        with pytest.raises(SystemExit) as exc:
            resolve_interview(pkg_root, "nope.yml")
        assert "not found" in str(exc.value)

    def test_no_interviews_raises_hint(self, tmp_path):
        (tmp_path / "docassemble" / "emptypkg").mkdir(parents=True)
        with pytest.raises(SystemExit) as exc:
            resolve_interview(tmp_path)
        assert "--interview" in str(exc.value)


class TestSaveRoundTrip:
    def test_save_is_atomic_and_round_trips(self, tmp_path):
        session = Session("docassemble.pkg:main.yml", tmp_path)
        user_dict = {"answer": 42}
        screen = {"kind": "question", "question_name": "q1"}

        session.save(user_dict, screen)

        assert session.state_file.exists()
        assert not list(tmp_path.rglob("*.tmp"))
        payload = pickle.loads(session.state_file.read_bytes())
        assert payload["interview_path"] == "docassemble.pkg:main.yml"
        assert payload["user_dict"] == {"answer": 42}
        assert payload["screen"] == screen
        assert payload["origin"] == "flow"

    def test_reset_removes_state_file(self, tmp_path):
        session = Session("docassemble.pkg:main.yml", tmp_path)
        session.save({}, None)
        session.reset()
        assert not session.state_file.exists()

    def test_snapshot_round_trip_drops_unpicklable_entries(self, tmp_path):
        session = Session("docassemble.pkg:main.yml", tmp_path)
        snapshot = tmp_path / "state.pkl"

        session.save_snapshot(snapshot, {"answer": 42, "callback": lambda: None})

        assert session.load_snapshot(snapshot) == {"answer": 42}


def _fake_interview(question=None):
    return SimpleNamespace(
        source=SimpleNamespace(path="/tmp/main.yml"),
        questions_by_name={} if question is None else {question.name: question},
    )


class TestCheckboxAssignments:
    def test_json_dict_is_coerced_to_dadict_for_checkbox_field(self, tmp_path, da_stubs, monkeypatch):
        class FakeDADict:
            def __init__(self, *, elements):
                self.elements = elements

        util = types.ModuleType("docassemble.base.util")
        util.DADict = FakeDADict
        monkeypatch.setitem(sys.modules, "docassemble.base.util", util)

        session = Session("docassemble.pkg:main.yml", tmp_path)
        user_dict = {"documents": SimpleNamespace()}
        screen = {
            "fields": [
                {"variable": "documents.selected_documents", "type": "checkboxes"}
            ]
        }

        errors = session.apply_assignments(
            _fake_interview(),
            user_dict,
            [("documents.selected_documents", '{"family_parenting_plan": true}')],
            use_code=False,
            screen=screen,
        )

        assert errors == []
        assert isinstance(user_dict["documents"].selected_documents, FakeDADict)
        assert user_dict["documents"].selected_documents.elements == {
            "family_parenting_plan": True
        }

    def test_code_assignments_bypass_checkbox_coercion(self, tmp_path, da_stubs, monkeypatch):
        class FakeDADict:
            def __init__(self, *, elements):
                self.elements = elements

        util = types.ModuleType("docassemble.base.util")
        util.DADict = FakeDADict
        monkeypatch.setitem(sys.modules, "docassemble.base.util", util)

        session = Session("docassemble.pkg:main.yml", tmp_path)
        user_dict = {"documents": SimpleNamespace(), "DADict": FakeDADict}
        screen = {
            "fields": [
                {"variable": "documents.selected_documents", "type": "checkboxes"}
            ]
        }

        errors = session.apply_assignments(
            _fake_interview(),
            user_dict,
            [("documents.selected_documents", "DADict(elements={})")],
            use_code=True,
            screen=screen,
        )

        assert errors == []
        assert isinstance(user_dict["documents"].selected_documents, FakeDADict)


class TestValidateScreenFallback:
    """The pickled-screen fallback must honor visible/required flags."""

    def _session(self, tmp_path):
        return Session("docassemble.pkg:main.yml", tmp_path)

    def _screen(self, *fields, question_name=None):
        return {
            "kind": "question",
            "question_name": question_name,
            "fields": list(fields),
        }

    def test_missing_required_field_warns(self, tmp_path):
        session = self._session(tmp_path)
        screen = self._screen({"variable": "M.missing", "type": "text", "required": True})
        result = session.validate_screen(object(), {}, screen)
        assert result["errors"] == []
        assert any("M.missing" in w for w in result["warnings"])

    def test_optional_field_skipped(self, tmp_path):
        session = self._session(tmp_path)
        screen = self._screen({"variable": "M.opt", "type": "text", "required": False})
        result = session.validate_screen(object(), {}, screen)
        assert result == {"errors": [], "warnings": []}

    def test_hidden_field_skipped_even_if_required(self, tmp_path):
        session = self._session(tmp_path)
        screen = self._screen(
            {"variable": "M.hidden", "type": "text", "required": True, "visible": False}
        )
        result = session.validate_screen(object(), {}, screen)
        assert result == {"errors": [], "warnings": []}

    def test_signature_field_skipped(self, tmp_path):
        session = self._session(tmp_path)
        screen = self._screen(
            {"variable": "M.sig", "type": "signature", "required": True}
        )
        result = session.validate_screen(object(), {}, screen)
        assert result == {"errors": [], "warnings": []}

    def test_validation_error_and_warning_reported_separately(self, tmp_path, da_stubs):
        session = self._session(tmp_path)

        class FakeQuestion:
            name = "q1"
            validation_code = "raise DAValidationError('bad answer')"
            fields = None

        interview = _fake_interview(FakeQuestion())
        user_dict = {"DAValidationError": da_stubs.DAValidationError}
        screen = self._screen(
            {"variable": "M.gone", "type": "text"}, question_name="q1"
        )
        result = session.validate_screen(interview, user_dict, screen)
        assert result["errors"] == ["bad answer"]
        assert len(result["warnings"]) == 1
        assert "M.gone" in result["warnings"][0]

    def test_validation_code_crash_reports_error(self, tmp_path, da_stubs):
        session = self._session(tmp_path)

        class FakeQuestion:
            name = "q1"
            validation_code = "raise RuntimeError('boom')"

        interview = _fake_interview(FakeQuestion())
        screen = self._screen(question_name="q1")
        result = session.validate_screen(interview, {}, screen)
        assert len(result["errors"]) == 1
        assert "crashed" in result["errors"][0]
        assert "RuntimeError" in result["errors"][0]
