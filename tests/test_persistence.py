from __future__ import annotations

import subprocess
import sys

from docassemble_simulator.execution import StateStore


def _run_writers(script, arguments):
    writers = [
        subprocess.Popen(
            [sys.executable, "-c", script, *writer_arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for writer_arguments in arguments
    ]
    failures = []
    for writer in writers:
        stdout, stderr = writer.communicate(timeout=30)
        if writer.returncode:
            failures.append(stdout + stderr)
    return failures


def test_concurrent_snapshot_writers_install_complete_payloads(tmp_path):
    destination = tmp_path / "shared.snapshot"
    identity = "docassemble.pkg:data/questions/main.yml"
    script = """
import sys
from pathlib import Path
from docassemble_simulator.execution import StateStore

root, destination, identity, marker = sys.argv[1:]
store = StateStore(Path(root), identity)
for index in range(20):
    store.save_snapshot(
        Path(destination),
        {"marker": marker, "index": index, "padding": marker * 200000},
    )
"""
    failures = _run_writers(
        script,
        [
            (str(tmp_path), str(destination), identity, marker)
            for marker in ("a", "b", "c", "d")
        ],
    )

    assert failures == []
    snapshot = StateStore(tmp_path, identity).load_snapshot(destination)
    assert snapshot["marker"] in {"a", "b", "c", "d"}
    assert snapshot["index"] == 19
    assert snapshot["padding"] == snapshot["marker"] * 200000


def test_concurrent_artifact_writers_install_complete_files(tmp_path):
    destination = tmp_path / "artifact.docx"
    script = """
import sys
from pathlib import Path
from docassemble_simulator.render import write_artifact

class Template:
    def __init__(self, marker):
        self.marker = marker

    def save(self, path):
        Path(path).write_bytes(self.marker.encode() * 200000)

destination, marker = sys.argv[1:]
for _ in range(20):
    write_artifact(Template(marker), Path(destination))
"""

    failures = _run_writers(
        script,
        [(str(destination), marker) for marker in ("a", "b", "c", "d")],
    )

    assert failures == []
    contents = destination.read_bytes()
    assert contents in {marker.encode() * 200000 for marker in ("a", "b", "c", "d")}
