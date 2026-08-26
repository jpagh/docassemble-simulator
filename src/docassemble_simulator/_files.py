"""Private atomic filesystem effects shared by persistence and rendering."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProtectedPath:
    path: Path
    reason: str
    directory: bool = False


class DestinationError(ValueError):
    """A render effect would replace an owned path or another effect."""


def validate_destinations(
    destinations: tuple[Path, ...], protected: tuple[ProtectedPath, ...]
) -> None:
    resolved = [path.expanduser().resolve() for path in destinations]
    if len(set(resolved)) != len(resolved):
        raise DestinationError("snapshot and artifact destinations must be different")
    for destination in resolved:
        for item in protected:
            protected_path = item.path.expanduser().resolve()
            if item.directory and destination.is_relative_to(protected_path):
                raise DestinationError(
                    f"render effects cannot write inside {item.reason}"
                )
            if not item.directory and destination == protected_path:
                raise DestinationError(f"render effects cannot replace {item.reason}")


@contextmanager
def flock(lock_path: Path):
    import fcntl

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def hashed_lock_file(target: Path) -> Path:
    lock_directory = Path(tempfile.gettempdir()) / (
        f"docassemble-simulator-locks-{os.getuid()}"
    )
    digest = hashlib.sha256(str(target).encode()).hexdigest()
    return lock_directory / f"{digest}.lock"


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
    with flock(hashed_lock_file(target)):
        yield
