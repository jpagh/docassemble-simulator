"""Discover, stage, and exercise the docassemble example corpus.

The corpus runner deliberately keeps process execution outside the simulator
runtime.  docassemble installs process-global hooks and its examples include
non-terminating and intentionally failing interviews, so one subprocess per
case and phase is the safety boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Self


class CorpusError(Exception):
    """A corpus, runtime, staging, or runner configuration error."""


@dataclass(frozen=True, init=False)
class FixtureCase:
    """One YAML fixture and its canonical package path, once resolved."""

    path: Path
    relative_path: Path
    sha256: str
    package: str | None
    canonical_package_path: str | None
    runtime_drift: bool

    def __init__(
        self,
        path: Path,
        relative_path: Path,
        sha256: str,
        package: str | None = None,
        canonical_package_path: str | None = None,
        runtime_drift: bool = False,
    ) -> None:
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "relative_path", relative_path)
        object.__setattr__(self, "sha256", sha256)
        object.__setattr__(self, "package", package)
        object.__setattr__(
            self,
            "canonical_package_path",
            canonical_package_path,
        )
        object.__setattr__(self, "runtime_drift", runtime_drift)


@dataclass(frozen=True)
class RuntimePackage:
    """An installed docassemble package available to the selected interpreter."""

    name: str
    path: Path
    version: str | None = None


@dataclass(frozen=True)
class ProvenanceEntry:
    """Reviewed fixture provenance recorded independently of runtime discovery."""

    fixture: str
    sha256: str
    package: str
    identity: str
    runtime_sha256: str


class RunPhase(StrEnum):
    COMPILE = "compile"
    START = "start"


class CorpusClassification(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    PROTOCOL_ERROR = "protocol_error"
    CRASH = "crash"
    TIMEOUT = "timeout"
    RUNTIME_DRIFT = "runtime_drift"


class CorpusErrorKind(StrEnum):
    PROTOCOL = "protocol"
    CRASH = "crash"
    TIMEOUT = "timeout"
    RUNTIME_DRIFT = "runtime-drift"


@dataclass(frozen=True)
class CaseResult:
    """Serializable result of one isolated subprocess invocation."""

    interview: str
    fixture: str
    fixture_sha256: str
    phase: RunPhase | str
    command: tuple[str, ...]
    returncode: int | None
    elapsed_seconds: float
    classification: CorpusClassification | str
    error_kind: CorpusErrorKind | str | None = None
    message: str | None = None
    envelope: dict | None = None
    timed_out: bool = False
    expectation: str | None = None
    expectation_category: str | None = None
    runtime_drift: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase", RunPhase(self.phase))
        object.__setattr__(
            self, "classification", CorpusClassification(self.classification)
        )

    def as_dict(self) -> dict:
        return {
            "interview": self.interview,
            "fixture": self.fixture,
            "fixture_sha256": self.fixture_sha256,
            "phase": self.phase,
            "command": list(self.command),
            "returncode": self.returncode,
            "elapsed_seconds": round(self.elapsed_seconds, 6),
            "classification": self.classification,
            "error_kind": self.error_kind,
            "message": self.message,
            "envelope": self.envelope,
            "timed_out": self.timed_out,
            "expectation": self.expectation,
            "expectation_category": self.expectation_category,
            "reproduction": " ".join(shlex.quote(part) for part in self.command),
            "runtime_drift": self.runtime_drift,
        }


@dataclass(frozen=True)
class StagedCorpus:
    """A staged package tree and its resolved cases."""

    root: Path
    cases: tuple[FixtureCase, ...]
    temporary: bool = True

    def cleanup(self) -> None:
        if self.temporary:
            for path in self.root.rglob("*"):
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode | 0o700)
            self.root.chmod(self.root.stat().st_mode | 0o700)
            shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args) -> None:
        self.cleanup()


DEFAULT_PROVENANCE_OVERRIDES = {
    # The flattened LSP corpus contains this identical file from both
    # upstream packages.  The base copy is the canonical source for it.
    "questionless.yml": "base",
}


@dataclass(frozen=True)
class SubprocessResult:
    returncode: int | None
    stdout: str
    stderr: str
    elapsed_seconds: float
    timed_out: bool = False


@dataclass(frozen=True)
class Expectation:
    interview: str
    phase: RunPhase | str
    classification: CorpusClassification | str | None = None
    returncode: int | None = None
    error_kind: CorpusErrorKind | str | None = None
    message: str | None = None
    reason: str = ""
    category: str = "capability-boundary"

    @property
    def key(self) -> tuple[str, str]:
        return self.interview, self.phase

    def matches(self, result: CaseResult) -> bool:
        return (
            result.classification == "failure"
            and result.interview == self.interview
            and result.phase == self.phase
            and (
                self.classification is None
                or result.classification == self.classification
            )
            and (self.returncode is None or result.returncode == self.returncode)
            and (self.error_kind is None or result.error_kind == self.error_kind)
            and (self.message is None or self.message in (result.message or ""))
        )


def fixture_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_fixture_cases(fixtures_root: str | Path) -> tuple[FixtureCase, ...]:
    """Discover all YAML examples, in stable relative-path order."""
    root = Path(fixtures_root).expanduser().resolve()
    examples = root / "examples"
    if not examples.is_dir():
        raise CorpusError(f"fixture examples directory does not exist: {examples}")
    paths = sorted(
        path
        for path in examples.rglob("*.yml")
        if path.is_file() and not any(p.startswith(".") for p in path.parts)
    )
    return tuple(
        FixtureCase(path, path.relative_to(examples), fixture_digest(path))
        for path in paths
    )


def _runtime_example_path(runtime: RuntimePackage, relative: Path) -> Path:
    return runtime.path / "data" / "questions" / "examples" / relative


def resolve_provenance(
    case: FixtureCase,
    packages: Mapping[str, RuntimePackage],
    *,
    overrides: Mapping[str, str] | None = None,
) -> FixtureCase:
    """Resolve a fixture to a package using content and package membership.

    Exact content is preferred.  A single package containing the filename is
    accepted as a drifted fixture so old corpus revisions can be run against a
    newer installed runtime.  Ambiguous names require an explicit override.
    """
    relative_key = case.relative_path.as_posix()
    override = (overrides or {}).get(relative_key) or (overrides or {}).get(
        case.relative_path.name
    )
    candidates = [
        name
        for name, package in packages.items()
        if _runtime_example_path(package, case.relative_path).is_file()
    ]
    if override is not None:
        if override not in packages or override not in candidates:
            raise CorpusError(
                f"provenance override for {relative_key!r} names unavailable package {override!r}"
            )
        selected = override
    else:
        exact = [
            name
            for name in candidates
            if fixture_digest(_runtime_example_path(packages[name], case.relative_path))
            == case.sha256
        ]
        if len(exact) == 1:
            selected = exact[0]
        elif len(exact) > 1:
            raise CorpusError(
                f"ambiguous runtime provenance for {relative_key!r}; use an override"
            )
        elif len(candidates) == 1:
            selected = candidates[0]
        elif not candidates:
            raise CorpusError(
                f"fixture {relative_key!r} is not present in the selected runtime"
            )
        else:
            raise CorpusError(
                f"ambiguous runtime provenance for {relative_key!r}; use an override"
            )
    runtime_path = _runtime_example_path(packages[selected], case.relative_path)
    return replace(
        case,
        package=selected,
        canonical_package_path=f"docassemble.{selected}:data/questions/examples/{relative_key}",
        runtime_drift=fixture_digest(runtime_path) != case.sha256,
    )


def resolve_cases(
    fixtures_root: str | Path,
    packages: Mapping[str, RuntimePackage],
    *,
    overrides: Mapping[str, str] | None = None,
    manifest: Iterable[ProvenanceEntry] | None = None,
) -> tuple[FixtureCase, ...]:
    discovered = discover_fixture_cases(fixtures_root)
    if manifest is not None:
        manifest = tuple(manifest)
        entries = {entry.fixture: entry for entry in manifest}
        if len(entries) != len(manifest):
            raise CorpusError("provenance manifest contains duplicate fixtures")
        discovered_names = {case.relative_path.as_posix() for case in discovered}
        missing = [name for name in sorted(discovered_names - entries.keys())]
        extra = sorted(entries.keys() - discovered_names)
        if missing or extra:
            details = []
            if missing:
                details.append("missing " + ", ".join(missing[:5]))
            if extra:
                details.append("unknown " + ", ".join(extra[:5]))
            raise CorpusError(
                "corpus/runtime drift: provenance manifest " + "; ".join(details)
            )
        resolved = []
        for case in discovered:
            entry = entries[case.relative_path.as_posix()]
            package = packages.get(entry.package)
            if package is None:
                raise CorpusError(
                    f"corpus/runtime drift: manifest package is unavailable: {entry.package}"
                )
            runtime_path = _runtime_example_path(package, case.relative_path)
            runtime_sha256 = (
                fixture_digest(runtime_path)
                if runtime_path and runtime_path.is_file()
                else None
            )
            drift = (
                entry.sha256 != case.sha256
                or entry.identity
                != f"docassemble.{entry.package}:data/questions/examples/{case.relative_path.as_posix()}"
                or runtime_sha256 != entry.runtime_sha256
            )
            resolved.append(
                replace(
                    case,
                    package=entry.package,
                    canonical_package_path=entry.identity,
                    runtime_drift=drift,
                )
            )
        return tuple(resolved)
    selected_overrides = dict(DEFAULT_PROVENANCE_OVERRIDES)
    selected_overrides.update(overrides or {})
    return tuple(
        resolve_provenance(case, packages, overrides=selected_overrides)
        for case in discovered
    )


def load_provenance_manifest(path: str | Path) -> tuple[ProvenanceEntry, ...]:
    manifest_path = Path(path).expanduser().resolve()
    try:
        data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise CorpusError(f"could not load provenance manifest: {error}") from error
    rows = data.get("fixtures", [])
    if not isinstance(rows, list):
        raise CorpusError("provenance manifest fixtures must be an array of tables")
    try:
        entries = tuple(ProvenanceEntry(**row) for row in rows)
    except TypeError as error:
        raise CorpusError(f"invalid provenance manifest entry: {error}") from error
    if any(
        not entry.fixture or not entry.package or not entry.identity
        for entry in entries
    ):
        raise CorpusError(
            "provenance manifest entries require fixture, package, and identity"
        )
    if len({entry.fixture for entry in entries}) != len(entries):
        raise CorpusError("provenance manifest contains duplicate fixtures")
    return entries


def write_provenance_manifest(
    path: str | Path,
    cases: Iterable[FixtureCase],
    packages: Mapping[str, RuntimePackage],
) -> None:
    entries = []
    for case in sorted(cases, key=lambda item: item.relative_path.as_posix()):
        if case.package is None or case.canonical_package_path is None:
            raise CorpusError(f"case has unresolved provenance: {case.relative_path}")
        runtime_path = _runtime_example_path(packages[case.package], case.relative_path)
        entries.append(
            ProvenanceEntry(
                case.relative_path.as_posix(),
                case.sha256,
                case.package,
                case.canonical_package_path,
                fixture_digest(runtime_path),
            )
        )
    lines = [
        "# Reviewed canonical provenance for the demo interview corpus.",
        "# Regenerate with scripts/test-demo-corpus --write-provenance-manifest PATH.",
        "",
    ]
    for entry in entries:
        lines.extend(
            [
                "[[fixtures]]",
                f"fixture = {json.dumps(entry.fixture)}",
                f"sha256 = {json.dumps(entry.sha256)}",
                f"package = {json.dumps(entry.package)}",
                f"identity = {json.dumps(entry.identity)}",
                f"runtime_sha256 = {json.dumps(entry.runtime_sha256)}",
                "",
            ]
        )
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8")


def _package_probe(interpreter: Path) -> dict:
    script = """
import importlib.util, json
result = {}
for name in ('base', 'demo', 'webapp'):
    full = 'docassemble.' + name
    try:
        spec = importlib.util.find_spec(full)
    except (ImportError, ModuleNotFoundError):
        continue
    if spec is None:
        continue
    entry = {'version': None}
    if spec.origin and name in ('base', 'demo'):
        path = spec.origin
        if path.endswith('__init__.py'):
            path = path[:-12]
        entry['path'] = path
    try:
        module = __import__(full, fromlist=['*'])
        entry['version'] = getattr(module, '__version__', None)
    except Exception:
        pass
    result[name] = entry
print(json.dumps(result))
"""
    completed = subprocess.run(
        [str(interpreter), "-c", script], capture_output=True, text=True, check=False
    )
    if completed.returncode:
        raise CorpusError(
            f"could not inspect runtime with {interpreter}: {(completed.stderr or completed.stdout).strip()}"
        )
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise CorpusError(f"runtime probe returned malformed JSON: {error}") from error
    return data


def discover_runtime_packages(interpreter: str | Path) -> dict[str, RuntimePackage]:
    """Locate the base and demo packages using the target interpreter."""
    # Do not resolve a virtualenv's interpreter symlink: the symlink path is
    # what selects the virtualenv's site-packages when it is executed.
    interpreter_path = Path(interpreter).expanduser().absolute()
    if not interpreter_path.is_file() or not os.access(interpreter_path, os.X_OK):
        raise CorpusError(f"target interpreter is not executable: {interpreter_path}")
    data = _package_probe(interpreter_path)
    packages = {
        name: RuntimePackage(name, Path(value["path"]).resolve(), value.get("version"))
        for name, value in data.items()
        if value.get("path") and Path(value["path"]).is_dir()
    }
    missing = {"base", "demo"} - packages.keys()
    if missing:
        raise CorpusError(
            "selected runtime is missing package(s): " + ", ".join(sorted(missing))
        )
    return packages


def runtime_versions(interpreter: str | Path) -> dict[str, str | None]:
    """Return versions reported by the target interpreter's runtime probe."""
    interpreter_path = Path(interpreter).expanduser().absolute()
    return {
        name: value.get("version")
        for name, value in _package_probe(interpreter_path).items()
    }


_NLTK_PACKAGES = ("omw-1.4", "wordnet", "wordnet_ic", "sentiwordnet")
_NLTK_CACHE_SCHEMA = 1


def _default_nltk_cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")).expanduser()
    return base / "docassemble-simulator" / "nltk"


def _nltk_cache_identity(interpreter: str | Path) -> dict:
    return {
        "schema": _NLTK_CACHE_SCHEMA,
        "python": _python_version(interpreter),
        "runtime": runtime_versions(interpreter),
        "packages": list(_NLTK_PACKAGES),
    }


def _nltk_cache_key(identity: dict) -> str:
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def _nltk_corpora_are_present(path: Path) -> bool:
    return all((path / "corpora" / package).is_dir() for package in _NLTK_PACKAGES)


def _nltk_data_is_valid(path: Path, identity: dict) -> bool:
    marker = path / ".complete.json"
    if not marker.is_file():
        return False
    try:
        completed_identity = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return completed_identity == identity and _nltk_corpora_are_present(path)


def _download_nltk_data(interpreter: str | Path, cache: Path) -> bool:
    script = f"""
import nltk
from pathlib import Path
from zipfile import ZipFile
cache = Path({str(cache)!r})
for package in {_NLTK_PACKAGES!r}:
    if not nltk.download(package, download_dir=str(cache), quiet=True):
        raise SystemExit(1)
    archive = cache / 'corpora' / (package + '.zip')
    if archive.is_file():
        with ZipFile(archive) as zipped:
            zipped.extractall(cache / 'corpora')
"""
    try:
        completed = subprocess.run(
            [str(Path(interpreter).expanduser().absolute()), "-c", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False
    return completed.returncode == 0


def _acquire_nltk_cache_lock(lock: Path) -> bool:
    deadline = time.monotonic() + 120
    while True:
        try:
            lock.mkdir()
            (lock / "owner").write_text(str(os.getpid()), encoding="ascii")
            return True
        except FileExistsError:
            try:
                stale = time.time() - lock.stat().st_mtime > 180
            except OSError:
                stale = False
            if stale:
                shutil.rmtree(lock, ignore_errors=True)
                continue
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def _prepare_nltk_data(
    interpreter: str | Path,
    cache_dir: str | Path | None = None,
    *,
    refresh: bool = False,
) -> Path | None:
    """Prepare optional linguistic data in a reusable, complete cache."""
    identity = _nltk_cache_identity(interpreter)
    root = (
        Path(cache_dir).expanduser().resolve()
        if cache_dir
        else _default_nltk_cache_dir().resolve()
    )
    cache_root = root / _nltk_cache_key(identity)
    generations = cache_root / "generations"
    current = cache_root / "current"
    lock = cache_root / ".lock"
    generations.mkdir(parents=True, exist_ok=True)
    if not refresh and current.is_symlink():
        candidate = current.resolve()
        if _nltk_data_is_valid(candidate, identity):
            return candidate
    if not _acquire_nltk_cache_lock(lock):
        return None
    staging = None
    try:
        if not refresh and current.is_symlink():
            candidate = current.resolve()
            if _nltk_data_is_valid(candidate, identity):
                return candidate
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=generations))
        if not _download_nltk_data(interpreter, staging):
            return None
        if not _nltk_corpora_are_present(staging):
            return None
        (staging / ".complete.json").write_text(
            json.dumps(identity, sort_keys=True), encoding="utf-8"
        )
        if not _nltk_data_is_valid(staging, identity):
            return None
        published = generations / f"generation-{time.time_ns()}"
        staging.rename(published)
        staging = None
        link = cache_root / f".current-{os.getpid()}-{time.time_ns()}"
        link.symlink_to(published.relative_to(cache_root), target_is_directory=True)
        os.replace(link, current)
        return published
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(lock, ignore_errors=True)


def _python_version(interpreter: str | Path) -> str:
    completed = subprocess.run(
        [
            str(Path(interpreter).expanduser().absolute()),
            "-c",
            "import platform; print(platform.python_version())",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise CorpusError(
            f"could not inspect Python version: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def stage_corpus(
    fixtures_root: str | Path,
    packages: Mapping[str, RuntimePackage],
    *,
    cases: Iterable[FixtureCase] | None = None,
    destination: str | Path | None = None,
) -> StagedCorpus:
    """Copy runtime packages once and overlay resolved fixtures."""
    resolved = tuple(
        cases if cases is not None else resolve_cases(fixtures_root, packages)
    )
    if any(
        case.package is None or case.canonical_package_path is None for case in resolved
    ):
        raise CorpusError("all cases must have resolved provenance before staging")
    if destination is None:
        root = Path(tempfile.mkdtemp(prefix="docassemble-demo-corpus-"))
        temporary = True
    else:
        root = Path(destination).expanduser().resolve()
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        temporary = False
    da_root = root / "docassemble"
    da_root.mkdir()
    for name, package in packages.items():
        target = da_root / name
        shutil.copytree(package.path, target, symlinks=False)
    for case in resolved:
        target = (
            da_root
            / case.package
            / "data"
            / "questions"
            / "examples"
            / case.relative_path
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(case.path, target)
    for path in (da_root, *da_root.rglob("*")):
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        path.chmod(mode & ~0o222)
    return StagedCorpus(root, resolved, temporary)


def _isolated_root(staged_root: Path, parent: Path) -> Path:
    root = parent / "workspace"
    root.mkdir()
    (root / "docassemble").mkdir()
    for package in (staged_root / "docassemble").iterdir():
        os.symlink(
            package, root / "docassemble" / package.name, target_is_directory=True
        )
    return root


def _terminate_process(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
        else:
            process.terminate()
            process.wait(timeout=1)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except ProcessLookupError:
            pass


def run_subprocess(
    command: Iterable[str],
    *,
    environment: Mapping[str, str] | None = None,
    timeout: float = 120,
) -> SubprocessResult:
    """Run a command with a process-group timeout and captured text output."""
    if timeout <= 0:
        raise CorpusError("subprocess timeout must be positive")
    started = time.monotonic()
    process = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(environment) if environment is not None else None,
        start_new_session=(os.name == "posix"),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        _terminate_process(process)
        stdout, stderr = process.communicate()
        timed_out = True
    return SubprocessResult(
        process.returncode,
        stdout,
        stderr,
        time.monotonic() - started,
        timed_out,
    )


_START_OUTCOME_KINDS = frozenset({"question", "continue", "finished"})


def _parse_result(
    stdout: str, returncode: int, *, phase: str
) -> tuple[str, str | None, str | None, dict | None]:
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return "protocol_error", "protocol", "simulator returned malformed JSON", None
    if not isinstance(envelope, dict) or not isinstance(envelope.get("ok"), bool):
        return (
            "protocol_error",
            "protocol",
            "simulator returned an invalid JSON envelope",
            envelope if isinstance(envelope, dict) else None,
        )
    if envelope["ok"]:
        if returncode != 0:
            return (
                CorpusClassification.PROTOCOL_ERROR,
                CorpusErrorKind.PROTOCOL,
                "successful JSON envelope had a nonzero exit code",
                envelope,
            )
        if phase == RunPhase.START.value:
            result = envelope.get("result")
            outcome_kind = result.get("kind") if isinstance(result, dict) else None
            if outcome_kind not in _START_OUTCOME_KINDS:
                return (
                    CorpusClassification.PROTOCOL_ERROR,
                    CorpusErrorKind.PROTOCOL,
                    f"simulator returned an unrecognized outcome kind: {outcome_kind!r}",
                    envelope,
                )
        return CorpusClassification.SUCCESS, None, None, envelope
    error = envelope.get("error") or {}
    kind = error.get("kind") if isinstance(error, dict) else None
    message = error.get("message") if isinstance(error, dict) else str(error)
    if returncode == 0:
        return (
            "protocol_error",
            "protocol",
            "failure envelope had a zero exit code",
            envelope,
        )
    return "failure", kind, message, envelope


def _network_sandbox_command(
    command: Iterable[str], writable_paths: Iterable[str | Path] = ()
) -> tuple[str, ...]:
    """Wrap a command in a backend that denies network syscalls."""
    command = tuple(command)
    if sys.platform == "darwin":
        sandbox = shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec"
        if not Path(sandbox).is_file():
            raise CorpusError("macOS network sandbox executable is unavailable")
        profile = "(version 1) (allow default) (deny network*)"
        probe = subprocess.run(
            [
                sandbox,
                "-p",
                profile,
                sys.executable,
                "-c",
                "import socket; s=socket.socket(); s.connect(('127.0.0.1', 1))",
            ],
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0 or b"Operation not permitted" not in probe.stderr:
            raise CorpusError(
                "macOS network sandbox is unavailable or not enforcing denial"
            )
        return (sandbox, "-p", profile, *command)
    bwrap = shutil.which("bwrap")
    if bwrap:
        try:
            root = Path(command[command.index("--root") + 1])
        except (ValueError, IndexError) as error:
            raise CorpusError(
                "sandbox command is missing its workspace root"
            ) from error
        writable = []
        for path in writable_paths:
            path = Path(path).resolve()
            writable.extend(("--bind", str(path), str(path)))
        return (
            bwrap,
            "--die-with-parent",
            "--unshare-net",
            "--ro-bind",
            "/",
            "/",
            "--bind",
            str(root),
            str(root),
            *writable,
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            *command,
        )
    raise CorpusError("no supported network-denial sandbox backend is available")


def _reproduction_command(
    case: FixtureCase,
    interpreter: str | Path,
    phase: str,
    timeout: float,
    source_root: str | Path | None,
    provenance_manifest: str | Path | None,
    prepare_runtime_data: bool,
) -> tuple[str, ...]:
    repo = Path(source_root or Path(__file__).resolve().parents[2]).resolve()
    script = repo / "scripts" / "test-demo-corpus"
    fixtures = case.path.parents[1]
    command = [
        str(script),
        "--fixtures",
        str(fixtures),
        "--python",
        str(Path(interpreter).expanduser().absolute()),
        "--match",
        f"^{re.escape(case.relative_path.as_posix())}$",
        f"--{phase}",
        "--timeout",
        f"{timeout:g}",
    ]
    if provenance_manifest is not None:
        command.extend(
            ("--provenance-manifest", str(Path(provenance_manifest).resolve()))
        )
    if prepare_runtime_data:
        command.append("--prepare-runtime-data")
    return tuple(command)


def run_case(
    case: FixtureCase,
    staged_root: str | Path,
    interpreter: str | Path,
    phase: RunPhase | str,
    *,
    timeout: float = 120,
    source_root: str | Path | None = None,
    nltk_data: str | Path | None = None,
    provenance_manifest: str | Path | None = None,
    prepare_runtime_data: bool = False,
) -> CaseResult:
    """Run one case in a fresh subprocess and classify its protocol result."""
    if case.canonical_package_path is None:
        raise CorpusError(f"case has no canonical package path: {case.relative_path}")
    phase_value = RunPhase(phase).value
    reproduction = _reproduction_command(
        case,
        interpreter,
        phase_value,
        timeout,
        source_root,
        provenance_manifest,
        prepare_runtime_data,
    )
    if case.runtime_drift:
        return CaseResult(
            case.canonical_package_path,
            case.relative_path.as_posix(),
            case.sha256,
            phase_value,
            reproduction,
            None,
            0,
            CorpusClassification.RUNTIME_DRIFT,
            CorpusErrorKind.RUNTIME_DRIFT,
            "fixture content differs from the selected runtime; corpus/runtime drift",
            runtime_drift=True,
        )
    # Preserve the virtualenv path for subprocess execution; resolving it can
    # switch to the global interpreter and lose the target runtime.
    interpreter_path = Path(interpreter).expanduser().absolute()
    stage_path = Path(staged_root).expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="docassemble-demo-case-") as temporary:
        case_root = _isolated_root(stage_path, Path(temporary))
        command = [
            str(interpreter_path),
            "-m",
            "docassemble_simulator",
            "--root",
            str(case_root),
            "--interview",
            case.canonical_package_path,
            "--json",
            "--offline",
            "--seek-diagnostics",
            "off",
        ]
        if phase_value == RunPhase.START.value:
            command += ["--background-actions", "disabled", "start"]
        else:
            command.append("check")
        environment = os.environ.copy()
        case_home = Path(temporary) / "home"
        case_home.mkdir()
        environment["HOME"] = str(case_home)
        environment["XDG_CONFIG_HOME"] = str(case_home / ".config")
        if nltk_data is not None:
            environment["NLTK_DATA"] = str(Path(nltk_data).resolve())
        source = Path(source_root or Path(__file__).resolve().parents[2]).resolve()
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, [str(source), environment.get("PYTHONPATH", "")])
        )
        sandboxed_command = _network_sandbox_command(command, (case_home,))
        process_result = run_subprocess(
            sandboxed_command, environment=environment, timeout=timeout
        )
        stdout, stderr = process_result.stdout, process_result.stderr
        if process_result.timed_out:
            return CaseResult(
                case.canonical_package_path,
                case.relative_path.as_posix(),
                case.sha256,
                phase_value,
                reproduction,
                process_result.returncode,
                process_result.elapsed_seconds,
                CorpusClassification.TIMEOUT,
                CorpusErrorKind.TIMEOUT,
                f"exceeded {timeout:g}s",
                None,
                True,
                runtime_drift=case.runtime_drift,
            )
        if process_result.returncode is None or process_result.returncode < 0:
            return CaseResult(
                case.canonical_package_path,
                case.relative_path.as_posix(),
                case.sha256,
                phase_value,
                reproduction,
                process_result.returncode,
                process_result.elapsed_seconds,
                CorpusClassification.CRASH,
                CorpusErrorKind.CRASH,
                (stderr or stdout).strip()[:500],
                None,
                runtime_drift=case.runtime_drift,
            )
        if stderr.strip():
            # Runtime logging on stderr is useful diagnostic context but does not
            # invalidate an otherwise valid JSON protocol.
            message_suffix = f" stderr: {stderr.strip()[:300]}"
        else:
            message_suffix = ""
        classification, error_kind, message, envelope = _parse_result(
            stdout, process_result.returncode, phase=phase_value
        )
        if process_result.returncode == 3:
            classification, error_kind = CorpusClassification.CRASH, "fault"
        return CaseResult(
            case.canonical_package_path,
            case.relative_path.as_posix(),
            case.sha256,
            phase_value,
            reproduction,
            process_result.returncode,
            process_result.elapsed_seconds,
            classification,
            error_kind,
            (message or "") + message_suffix or None,
            envelope,
            runtime_drift=case.runtime_drift,
        )


def _validate_expectation(expectation: Expectation) -> None:
    forbidden = {"timeout", "crash", "protocol_error"}
    if (
        expectation.classification in forbidden
        or expectation.error_kind in forbidden
        or expectation.returncode == 3
    ):
        raise CorpusError(
            f"expectation cannot mask a simulator fault: {expectation.interview} {expectation.phase}"
        )
    if expectation.category not in {"deliberate-error", "capability-boundary"}:
        raise CorpusError(
            f"expectation has unsupported category: {expectation.category}"
        )
    if expectation.classification not in {None, "failure"}:
        raise CorpusError(
            f"expectation has unsupported classification: {expectation.classification}"
        )
    if any(
        value is None or (isinstance(value, str) and not value.strip())
        for value in (
            expectation.returncode,
            expectation.error_kind,
            expectation.message,
        )
    ):
        raise CorpusError(
            "expectation must constrain returncode, error_kind, and message: "
            f"{expectation.interview} {expectation.phase}"
        )
    if not expectation.reason.strip():
        raise CorpusError(
            f"expectation needs a reason: {expectation.interview} {expectation.phase}"
        )


def load_expectations(path: str | Path | None) -> tuple[Expectation, ...]:
    if path is None:
        return ()
    expectation_path = Path(path).expanduser().resolve()
    try:
        data = tomllib.loads(expectation_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise CorpusError(f"could not load expectations: {error}") from error
    rows = data.get("expectations", [])
    if not isinstance(rows, list):
        raise CorpusError("expectations must be an array of tables")
    try:
        expectations = tuple(Expectation(**row) for row in rows)
    except TypeError as error:
        raise CorpusError(f"invalid expectation table: {error}") from error
    seen: set[tuple[str, str]] = set()
    for expectation in expectations:
        _validate_expectation(expectation)
        if expectation.key in seen:
            raise CorpusError(
                f"duplicate expectation: {expectation.interview} {expectation.phase}"
            )
        seen.add(expectation.key)
    return expectations


def apply_expectations(
    results: Iterable[CaseResult], expectations: Iterable[Expectation]
) -> tuple[CaseResult, ...]:
    expectation_list = tuple(expectations)
    for expectation in expectation_list:
        _validate_expectation(expectation)
    used: set[tuple[str, str]] = set()
    output = []
    for result in results:
        matches = [
            expectation
            for expectation in expectation_list
            if expectation.matches(result)
        ]
        if len(matches) > 1:
            raise CorpusError(
                f"overlapping expectations: {result.interview} {result.phase}"
            )
        if matches:
            used.add(matches[0].key)
            output.append(
                replace(
                    result,
                    expectation=matches[0].reason,
                    expectation_category=matches[0].category,
                )
            )
        else:
            output.append(result)
    unused = [
        expectation for expectation in expectation_list if expectation.key not in used
    ]
    if unused:
        names = ", ".join(f"{item.interview} {item.phase}" for item in unused)
        raise CorpusError(f"unused expectations: {names}")
    return tuple(output)


def run_corpus(
    cases: Iterable[FixtureCase],
    staged_root: str | Path,
    interpreter: str | Path,
    phases: Iterable[RunPhase | str],
    *,
    timeout: float = 120,
    source_root: str | Path | None = None,
    expectations: Iterable[Expectation] = (),
    nltk_data: str | Path | None = None,
    provenance_manifest: str | Path | None = None,
    prepare_runtime_data: bool = False,
    jobs: int = 1,
) -> tuple[CaseResult, ...]:
    if jobs <= 0:
        raise CorpusError("jobs must be positive")
    selected_phases = tuple(phases)
    work = tuple((case, phase) for case in cases for phase in selected_phases)

    def execute(item: tuple[FixtureCase, RunPhase | str]) -> CaseResult:
        case, phase = item
        return run_case(
            case,
            staged_root,
            interpreter,
            phase,
            timeout=timeout,
            source_root=source_root,
            nltk_data=nltk_data,
            provenance_manifest=provenance_manifest,
            prepare_runtime_data=prepare_runtime_data,
        )

    if jobs == 1:
        results = tuple(map(execute, work))
    else:
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            # executor.map retains input order while bounding active workers.
            results = tuple(executor.map(execute, work))
    return apply_expectations(results, expectations)


def run_demo_package_smoke(
    fixtures_root: str | Path,
    interpreter: str | Path,
    *,
    timeout: float = 120,
    source_root: str | Path | None = None,
    nltk_data: str | Path | None = None,
    prepare_runtime_data: bool = False,
) -> dict | None:
    """Compile the small package/include fixture without counting its YAML files."""
    package_source = Path(fixtures_root).expanduser().resolve() / "demo_package"
    if not package_source.is_dir():
        return None
    reproduction = (
        str(
            Path(source_root or Path(__file__).resolve().parents[2]).resolve()
            / "scripts"
            / "test-demo-corpus"
        ),
        "--fixtures",
        str(Path(fixtures_root).expanduser().resolve()),
        "--package-smoke",
        "--timeout",
        f"{timeout:g}",
        "--python",
        str(Path(interpreter).expanduser().absolute()),
    )
    if prepare_runtime_data:
        reproduction += ("--prepare-runtime-data",)
    with tempfile.TemporaryDirectory(prefix="docassemble-demo-package-") as temporary:
        root = Path(temporary) / "package"
        shutil.copytree(
            package_source,
            root,
            ignore=shutil.ignore_patterns(".simulator", "__pycache__"),
        )
        command = [
            str(Path(interpreter).expanduser().absolute()),
            "-m",
            "docassemble_simulator",
            "--root",
            str(root),
            "--json",
            "--offline",
            "check",
        ]
        environment = os.environ.copy()
        home = Path(temporary) / "home"
        home.mkdir()
        environment["HOME"] = str(home)
        environment["XDG_CONFIG_HOME"] = str(home / ".config")
        if nltk_data is not None:
            environment["NLTK_DATA"] = str(Path(nltk_data).resolve())
        source = Path(source_root or Path(__file__).resolve().parents[2]).resolve()
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, [str(source), environment.get("PYTHONPATH", "")])
        )
        result = run_subprocess(
            _network_sandbox_command(command, (home,)),
            environment=environment,
            timeout=timeout,
        )
        if result.timed_out:
            return {
                "classification": "timeout",
                "message": f"exceeded {timeout:g}s",
                "reproduction": reproduction,
            }
        classification, error_kind, message, envelope = _parse_result(
            result.stdout, result.returncode or 0, phase=RunPhase.COMPILE.value
        )
        return {
            "classification": classification,
            "error_kind": error_kind,
            "message": message,
            "envelope": envelope,
            "reproduction": reproduction,
        }


def write_reports(
    results: Iterable[CaseResult],
    output: str | Path,
    *,
    metadata: Mapping[str, object] | None = None,
) -> dict:
    destination = Path(output).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    rows = [result.as_dict() for result in results]
    counts: dict[str, int] = {}
    error_kinds: dict[str, int] = {}
    outcome_kinds: dict[str, int] = {}
    expectation_categories: dict[str, int] = {}
    for row in rows:
        counts[row["classification"]] = counts.get(row["classification"], 0) + 1
        if row["error_kind"]:
            error_kinds[row["error_kind"]] = error_kinds.get(row["error_kind"], 0) + 1
        if row["expectation_category"]:
            category = row["expectation_category"]
            expectation_categories[category] = (
                expectation_categories.get(category, 0) + 1
            )
        result = (row["envelope"] or {}).get("result")
        if isinstance(result, dict) and result.get("kind"):
            kind = result["kind"]
            outcome_kinds[kind] = outcome_kinds.get(kind, 0) + 1
    report_metadata = dict(metadata or {})
    package_smoke = report_metadata.get("demo_package")
    package_unexpected = (
        isinstance(package_smoke, dict)
        and package_smoke.get("classification") != "success"
    )
    summary = {
        "metadata": report_metadata,
        "total": len(rows),
        "counts": dict(sorted(counts.items())),
        "error_kinds": dict(sorted(error_kinds.items())),
        "outcome_kinds": dict(sorted(outcome_kinds.items())),
        "expectation_categories": dict(sorted(expectation_categories.items())),
        "runtime_drift": sum(row["runtime_drift"] for row in rows),
        "package_smoke": package_smoke,
        "unexpected": sum(
            row["classification"] not in {"success", "runtime_drift"}
            and not row["expectation"]
            for row in rows
        )
        + package_unexpected,
    }
    (destination / "results.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    (destination / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    markdown = [
        "# Demo corpus results",
        "",
        f"Total: {summary['total']}",
        "",
        "| Classification | Count |",
        "| --- | ---: |",
    ]
    markdown.extend(f"| {key} | {value} |" for key, value in summary["counts"].items())
    markdown.extend(
        ["", "## Error kinds", "", "| Error kind | Count |", "| --- | ---: |"]
    )
    markdown.extend(
        f"| {key} | {value} |" for key, value in summary["error_kinds"].items()
    )
    markdown.extend(
        ["", "## Outcome kinds", "", "| Outcome kind | Count |", "| --- | ---: |"]
    )
    markdown.extend(
        f"| {key} | {value} |" for key, value in summary["outcome_kinds"].items()
    )
    (destination / "summary.md").write_text(
        "\n".join(markdown) + "\n", encoding="utf-8"
    )
    return summary


def select_cases(
    cases: Iterable[FixtureCase],
    *,
    matcher: re.Pattern[str] | None = None,
    shard: tuple[int, int] | None = None,
) -> tuple[FixtureCase, ...]:
    """Apply stable path filtering and 1-based hash sharding."""
    selected = tuple(
        case
        for case in cases
        if matcher is None or matcher.search(case.relative_path.as_posix())
    )
    if shard is None:
        return selected
    index, count = shard
    return tuple(
        case
        for case in selected
        if int(hashlib.sha256(case.canonical_package_path.encode()).hexdigest(), 16)
        % count
        == index - 1
    )


def _git_revision(path: str | Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(Path(path).expanduser().resolve()), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    revision = completed.stdout.strip()
    return revision if completed.returncode == 0 and revision else None


def _validate_output_path(
    output: str | Path,
    fixtures_root: str | Path,
    packages: Mapping[str, RuntimePackage],
) -> None:
    destination = Path(output).expanduser().resolve()
    authored = [Path(fixtures_root).expanduser().resolve() / "examples"]
    authored.extend(package.path / "data" for package in packages.values())
    for path in authored:
        try:
            destination.relative_to(path.resolve())
        except ValueError:
            continue
        raise CorpusError(f"output path is inside authored resources: {destination}")


def _parse_shard(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", value)
    if match is None:
        raise argparse.ArgumentTypeError(
            "shard must be INDEX/COUNT, using 1-based INDEX"
        )
    index, count = map(int, match.groups())
    if index > count:
        raise argparse.ArgumentTypeError("shard index must not exceed shard count")
    return index, count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run docassemble demo interviews in isolated subprocesses"
    )
    default_fixtures = os.environ.get(
        "DASIMULATOR_DEMO_FIXTURES",
        str(
            Path(__file__).resolve().parents[3]
            / "docassemble-yaml"
            / "lsp"
            / "tests"
            / "fixtures"
        ),
    )
    parser.add_argument("--fixtures", default=default_fixtures)
    parser.add_argument("--python", dest="interpreter", default=sys.executable)
    parser.add_argument("--compile", action="store_true")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--package-smoke", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--match", help="regular expression matched against fixture relative paths"
    )
    parser.add_argument("--shard", type=_parse_shard)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument(
        "--prepare-runtime-data",
        action="store_true",
        help="download missing NLTK data before entering the network-denied sandbox",
    )
    parser.add_argument(
        "--nltk-cache-dir",
        help="directory for reusable prepared NLTK data",
    )
    parser.add_argument(
        "--refresh-nltk-cache",
        action="store_true",
        help="prepare a new NLTK cache generation instead of reusing one",
    )
    parser.add_argument("--output", default=".simulator/demo-corpus-results")
    default_manifest = (
        Path(__file__).resolve().parents[2] / "tests" / "demo_corpus_provenance.toml"
    )
    parser.add_argument("--provenance-manifest", default=str(default_manifest))
    parser.add_argument("--write-provenance-manifest")
    default_expectations = (
        Path(__file__).resolve().parents[2] / "tests" / "demo_corpus_expectations.toml"
    )
    parser.add_argument(
        "--expectations",
        default=str(default_expectations) if default_expectations.is_file() else None,
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    if args.jobs <= 0:
        parser.error("jobs must be positive")
    if args.refresh_nltk_cache and not args.prepare_runtime_data:
        parser.error("--refresh-nltk-cache requires --prepare-runtime-data")
    try:
        matcher = re.compile(args.match) if args.match else None
        packages = discover_runtime_packages(args.interpreter)
        _validate_output_path(args.output, args.fixtures, packages)
        if args.package_smoke:
            result = run_demo_package_smoke(
                args.fixtures,
                args.interpreter,
                timeout=args.timeout,
                prepare_runtime_data=args.prepare_runtime_data,
            )
            if result is None:
                raise CorpusError("demo_package fixture directory does not exist")
            if result["classification"] != "success":
                print(
                    f"UNEXPECTED demo_package: {result.get('message') or result['classification']}\n"
                    f"REPRODUCE: {shlex.join(result['reproduction'])}",
                    file=sys.stderr,
                )
                return 1
            return 0
        if args.write_provenance_manifest:
            discovered_cases = resolve_cases(args.fixtures, packages)
            write_provenance_manifest(
                args.write_provenance_manifest, discovered_cases, packages
            )
            return 0
        manifest = load_provenance_manifest(args.provenance_manifest)
        cases = resolve_cases(args.fixtures, packages, manifest=manifest)
        cases = select_cases(cases, matcher=matcher, shard=args.shard)
        if not cases:
            raise CorpusError("selection contains no fixtures")
        phases = []
        if args.compile or not args.start:
            phases.append(RunPhase.COMPILE)
        if args.start or not args.compile:
            phases.append(RunPhase.START)
        expectations = load_expectations(args.expectations)
        selected_keys = {
            (case.canonical_package_path, phase.value)
            for case in cases
            for phase in phases
        }
        selected_expectations = tuple(
            expectation
            for expectation in expectations
            if expectation.key in selected_keys
        )
        nltk_data = (
            _prepare_nltk_data(
                args.interpreter,
                args.nltk_cache_dir,
                refresh=args.refresh_nltk_cache,
            )
            if args.prepare_runtime_data
            else None
        )
        if args.prepare_runtime_data and nltk_data is None:
            raise CorpusError("could not prepare reusable NLTK data")
        package_smoke = run_demo_package_smoke(
            args.fixtures,
            args.interpreter,
            timeout=args.timeout,
            nltk_data=nltk_data,
            prepare_runtime_data=args.prepare_runtime_data,
        )
        with stage_corpus(args.fixtures, packages, cases=cases) as staged:
            results = run_corpus(
                cases,
                staged.root,
                args.interpreter,
                phases,
                timeout=args.timeout,
                expectations=selected_expectations,
                nltk_data=nltk_data,
                provenance_manifest=args.provenance_manifest,
                prepare_runtime_data=args.prepare_runtime_data,
                jobs=args.jobs,
            )
            summary = write_reports(
                results,
                args.output,
                metadata={
                    "fixtures": str(Path(args.fixtures).resolve()),
                    "interpreter": str(Path(args.interpreter).expanduser().absolute()),
                    "phases": [phase.value for phase in phases],
                    "jobs": args.jobs,
                    "runtime_versions": runtime_versions(args.interpreter),
                    "python_version": _python_version(args.interpreter),
                    "fixture_revision": _git_revision(args.fixtures),
                    "provenance_manifest": str(
                        Path(args.provenance_manifest).resolve()
                    ),
                    "demo_package": package_smoke,
                },
            )
        for result in results:
            if result.classification == CorpusClassification.RUNTIME_DRIFT:
                print(
                    f"DRIFT {result.interview} {result.phase}: {result.message or result.classification}\n"
                    f"REPRODUCE: {shlex.join(result.command)}",
                    file=sys.stderr,
                )
            elif result.classification != "success" and not result.expectation:
                print(
                    f"UNEXPECTED {result.interview} {result.phase}: {result.message or result.classification}\n"
                    f"REPRODUCE: {shlex.join(result.command)}",
                    file=sys.stderr,
                )
        package_failed = (
            package_smoke is not None and package_smoke["classification"] != "success"
        )
        if package_failed:
            print(
                f"UNEXPECTED demo_package: {package_smoke.get('message') or package_smoke['classification']}\n"
                f"REPRODUCE: {shlex.join(package_smoke['reproduction'])}",
                file=sys.stderr,
            )
        return (
            0
            if summary["unexpected"] == 0
            and summary["runtime_drift"] == 0
            and not package_failed
            else 1
        )
    except (CorpusError, re.error) as error:
        if str(error).startswith("corpus/runtime drift"):
            try:
                drift_command = (
                    str(
                        Path(__file__).resolve().parents[2]
                        / "scripts"
                        / "test-demo-corpus"
                    ),
                    "--fixtures",
                    str(Path(args.fixtures).expanduser().resolve()),
                    "--provenance-manifest",
                    str(Path(args.provenance_manifest).expanduser().resolve()),
                )
                write_reports(
                    [
                        CaseResult(
                            "corpus",
                            "provenance-manifest",
                            "",
                            RunPhase.COMPILE,
                            drift_command,
                            None,
                            0,
                            CorpusClassification.RUNTIME_DRIFT,
                            CorpusErrorKind.RUNTIME_DRIFT,
                            str(error),
                            runtime_drift=True,
                        )
                    ],
                    args.output,
                    metadata={"error": str(error)},
                )
            except (OSError, ValueError):
                pass
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
