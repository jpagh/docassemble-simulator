from __future__ import annotations

from pathlib import Path

from docassemble_simulator.runner import SimulatorRunner


def _project(tmp_path: Path) -> Path:
    package = tmp_path / "docassemble" / "example"
    (package / "data" / "questions").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "data" / "questions" / "main.yml").write_text("---\n", encoding="utf-8")
    return tmp_path


def test_runner_discovers_project_and_copies_source_for_isolation(tmp_path):
    project = _project(tmp_path)
    runner = SimulatorRunner(project, config={"simulator": {"offline": True}})

    assert runner.project_root == project.resolve()
    isolated = runner.root
    assert isolated != runner.project_root
    assert (
        isolated / "docassemble" / "example" / "data" / "questions" / "main.yml"
    ).is_file()
    assert runner.session_root != project / ".simulator" / "sessions"

    runner.close()
    assert not isolated.exists()
    assert (
        project / "docassemble" / "example" / "data" / "questions" / "main.yml"
    ).is_file()


def test_runner_shared_policy_does_not_copy_source(tmp_path):
    project = _project(tmp_path)
    runner = SimulatorRunner(project, isolate=False)

    assert runner.root == project.resolve()
    runner.close()
