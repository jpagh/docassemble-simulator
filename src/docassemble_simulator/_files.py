"""Private atomic filesystem effects shared by persistence and rendering."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path


def atomic_replace(
    destination: str | Path,
    write_temporary: Callable[[Path], None],
    *,
    lock_destination: bool,
) -> Path:
    """Install one complete sibling file, optionally serializing by destination."""
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if lock_destination:
        with _destination_lock(target):
            _replace(target, write_temporary)
    else:
        _replace(target, write_temporary)
    return target


def _replace(target: Path, write_temporary: Callable[[Path], None]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        write_temporary(temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _destination_lock(target: Path):
    import fcntl

    lock_directory = Path(tempfile.gettempdir()) / (
        f"docassemble-simulator-locks-{os.getuid()}"
    )
    lock_directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(target).encode()).hexdigest()
    with (lock_directory / f"{digest}.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
