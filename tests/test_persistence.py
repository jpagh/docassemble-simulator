from __future__ import annotations

import subprocess
import sys

import pytest

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


def test_concurrent_session_mutations_do_not_lose_updates(tmp_path):
    from docassemble_simulator.execution import StateStore

    identity = "docassemble.pkg:data/questions/main.yml"
    store = StateStore(tmp_path, identity)
    store.save({"counter": 0}, {"kind": "executed"})
    script = """
import sys
from pathlib import Path
from docassemble_simulator.execution import StateStore

root, identity = sys.argv[1:]
store = StateStore(Path(root), identity)
for _ in range(20):
    with store.lock():
        payload = store.load()
        namespace = payload["namespace"]
        namespace["counter"] += 1
        store.save(namespace, {"kind": "executed"})
"""

    failures = _run_writers(
        script,
        [(str(tmp_path), identity) for _ in ("a", "b", "c", "d")],
    )

    assert failures == []
    assert store.load()["namespace"]["counter"] == 80


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


def test_corrupt_payload_has_saved_state_recovery_message(tmp_path):
    from docassemble_simulator.execution import ExecutionFailure

    identity = "docassemble.pkg:data/questions/main.yml"
    store = StateStore(tmp_path, identity)
    store.directory.mkdir(parents=True)
    store.path.write_bytes(b"not pickle")

    with pytest.raises(ExecutionFailure) as caught:
        store.load()

    assert str(caught.value).startswith(
        "saved state is unreadable; run `start` again ("
    )


def test_snapshot_with_another_interview_identity_is_rejected(tmp_path):
    import pickle

    from docassemble_simulator.execution import ExecutionFailure

    identity = "docassemble.pkg:data/questions/main.yml"
    snapshot = tmp_path / "snapshot.pkl"
    snapshot.write_bytes(
        pickle.dumps(
            {
                "schema": 1,
                "interview": "docassemble.other:data/questions/main.yml",
                "namespace": pickle.dumps({}),
            }
        )
    )

    with pytest.raises(ExecutionFailure) as caught:
        StateStore(tmp_path, identity).load_snapshot(snapshot)

    assert str(caught.value) == "snapshot belongs to another interview"


def test_flush_failure_preserves_destination_and_cleans_temporary(
    tmp_path, monkeypatch
):
    import docassemble_simulator._files as file_module

    destination = tmp_path / "state.pkl"
    destination.write_bytes(b"previous")
    monkeypatch.setattr(
        file_module.os,
        "fsync",
        lambda *args: (_ for _ in ()).throw(OSError("flush failed")),
    )

    with pytest.raises(OSError):
        file_module.atomic_replace(
            destination,
            lambda temporary: temporary.write_bytes(b"new"),
            lock_destination=False,
        )

    assert destination.read_bytes() == b"previous"
    assert not list(destination.parent.glob(".*.tmp"))


def test_write_failure_cleans_temporary(tmp_path):
    from docassemble_simulator._files import atomic_replace

    destination = tmp_path / "state.pkl"
    destination.write_bytes(b"previous")

    def fail(temporary):
        raise RuntimeError("serialization failed")

    with pytest.raises(RuntimeError):
        atomic_replace(destination, fail, lock_destination=False)

    assert destination.read_bytes() == b"previous"
    assert not list(destination.parent.glob(".*.tmp"))


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
