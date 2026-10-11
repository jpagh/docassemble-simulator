"""Controlled executable substitutes shared by the gate test suites."""

from __future__ import annotations

import textwrap
from pathlib import Path


def write_executable(path: Path, body: str) -> Path:
    """Write an isolated executable substitute for a gate subprocess."""
    path.write_text(body)
    path.chmod(0o755)
    return path


def fake_interpreter(tmp_path: Path, name: str, family: str, version: str) -> Path:
    """Create an executable reporting one controlled runtime family/version."""
    return write_executable(
        tmp_path / name,
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            if [[ "${{1:-}}" != "-c" ]]; then
              echo "unsupported invocation: $*" >&2
              exit 2
            fi
            echo "{family} {version} {version}"
            """
        ),
    )
