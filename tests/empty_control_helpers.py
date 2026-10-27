"""Shared workspace and persistence helpers for empty-control runtime journeys."""

from __future__ import annotations

import textwrap
from pathlib import Path


def start_empty_control_journey(
    root: Path, family, start_worker, package_name: str, interview: str
):
    package = root / "docassemble" / package_name
    questions = package / "data" / "questions"
    questions.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (questions / "main.yml").write_text(textwrap.dedent(interview))
    return start_worker(
        family.interpreter,
        root,
        family=family.probe,
        arguments=("--offline", "--seek-diagnostics", "off"),
    )


def saved_sessions(root: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in (root / ".simulator" / "sessions").glob("*.pkl")
    }
