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
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Self


class CorpusError(Exception):
    """A corpus, runtime, staging, or runner configuration error."""


@dataclass(frozen=True)
class FixtureCase:
    """One YAML fixture and, after provenance resolution, its canonical identity."""

    path: Path
    relative_path: Path
    sha256: str
    package: str | None = None
    identity: str | None = None
    runtime_drift: bool = False


@dataclass(frozen=True)
class RuntimePackage:
    """An installed docassemble package available to the selected interpreter."""

    name: str
    path: Path
    version: str | None = None


class RunPhase(StrEnum):
    COMPILE = "compile"
    START = "start"


@dataclass(frozen=True)
class CaseResult:
    """Serializable result of one isolated subprocess invocation."""

    interview: str
    fixture: str
    fixture_sha256: str
    phase: str
    command: tuple[str, ...]
    returncode: int | None
    elapsed_seconds: float
    classification: str
    error_kind: str | None = None
    message: str | None = None
    envelope: dict | None = None
    timed_out: bool = False
    expectation: str | None = None
    runtime_drift: bool = False

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
    phase: str
    classification: str | None = None
    returncode: int | None = None
    error_kind: str | None = None
    message: str | None = None
    reason: str = ""

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
        identity=f"docassemble.{selected}:data/questions/examples/{relative_key}",
        runtime_drift=fixture_digest(runtime_path) != case.sha256,
    )


def resolve_cases(
    fixtures_root: str | Path,
    packages: Mapping[str, RuntimePackage],
    *,
    overrides: Mapping[str, str] | None = None,
) -> tuple[FixtureCase, ...]:
    selected_overrides = dict(DEFAULT_PROVENANCE_OVERRIDES)
    selected_overrides.update(overrides or {})
    return tuple(
        resolve_provenance(case, packages, overrides=selected_overrides)
        for case in discover_fixture_cases(fixtures_root)
    )


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
    if any(case.package is None or case.identity is None for case in resolved):
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
                "protocol_error",
                "protocol",
                "successful JSON envelope had a nonzero exit code",
                envelope,
            )
        return "success", None, None, envelope
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


def run_case(
    case: FixtureCase,
    staged_root: str | Path,
    interpreter: str | Path,
    phase: RunPhase | str,
    *,
    timeout: float = 120,
    source_root: str | Path | None = None,
) -> CaseResult:
    """Run one case in a fresh subprocess and classify its protocol result."""
    if case.identity is None:
        raise CorpusError(f"case has no canonical identity: {case.relative_path}")
    phase_value = RunPhase(phase).value
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
            case.identity,
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
        source = Path(source_root or Path(__file__).resolve().parents[2]).resolve()
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, [str(source), environment.get("PYTHONPATH", "")])
        )
        process_result = run_subprocess(
            command, environment=environment, timeout=timeout
        )
        stdout, stderr = process_result.stdout, process_result.stderr
        if process_result.timed_out:
            return CaseResult(
                case.identity,
                case.relative_path.as_posix(),
                case.sha256,
                phase_value,
                tuple(command),
                process_result.returncode,
                process_result.elapsed_seconds,
                "timeout",
                "timeout",
                f"exceeded {timeout:g}s",
                None,
                True,
                runtime_drift=case.runtime_drift,
            )
        if process_result.returncode is None or process_result.returncode < 0:
            return CaseResult(
                case.identity,
                case.relative_path.as_posix(),
                case.sha256,
                phase_value,
                tuple(command),
                process_result.returncode,
                process_result.elapsed_seconds,
                "crash",
                "crash",
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
            classification, error_kind = "crash", "fault"
        return CaseResult(
            case.identity,
            case.relative_path.as_posix(),
            case.sha256,
            phase_value,
            tuple(command),
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
    if expectation.classification not in {None, "failure"}:
        raise CorpusError(
            f"expectation has unsupported classification: {expectation.classification}"
        )
    if all(
        value is None
        for value in (
            expectation.returncode,
            expectation.error_kind,
            expectation.message,
        )
    ):
        raise CorpusError(
            f"expectation must constrain the observed failure: {expectation.interview} {expectation.phase}"
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
            output.append(replace(result, expectation=matches[0].reason))
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
) -> tuple[CaseResult, ...]:
    selected_phases = tuple(phases)
    results = tuple(
        run_case(
            case,
            staged_root,
            interpreter,
            phase,
            timeout=timeout,
            source_root=source_root,
        )
        for case in cases
        for phase in selected_phases
    )
    return apply_expectations(results, expectations)


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
    for row in rows:
        counts[row["classification"]] = counts.get(row["classification"], 0) + 1
    summary = {
        "metadata": dict(metadata or {}),
        "total": len(rows),
        "counts": dict(sorted(counts.items())),
        "runtime_drift": sum(row["runtime_drift"] for row in rows),
        "unexpected": sum(
            row["classification"] != "success" and not row["expectation"]
            for row in rows
        ),
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
        if int(hashlib.sha256(case.identity.encode()).hexdigest(), 16) % count
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
    parser.add_argument(
        "--match", help="regular expression matched against fixture relative paths"
    )
    parser.add_argument("--shard", type=_parse_shard)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--output", default=".simulator/demo-corpus-results")
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
    try:
        matcher = re.compile(args.match) if args.match else None
        packages = discover_runtime_packages(args.interpreter)
        _validate_output_path(args.output, args.fixtures, packages)
        cases = resolve_cases(args.fixtures, packages)
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
            (case.identity, phase.value) for case in cases for phase in phases
        }
        selected_expectations = tuple(
            expectation
            for expectation in expectations
            if expectation.key in selected_keys
        )
        with stage_corpus(args.fixtures, packages, cases=cases) as staged:
            results = run_corpus(
                cases,
                staged.root,
                args.interpreter,
                phases,
                timeout=args.timeout,
                expectations=selected_expectations,
            )
            summary = write_reports(
                results,
                args.output,
                metadata={
                    "fixtures": str(Path(args.fixtures).resolve()),
                    "interpreter": str(Path(args.interpreter).expanduser().absolute()),
                    "phases": [phase.value for phase in phases],
                    "runtime_versions": runtime_versions(args.interpreter),
                    "fixture_revision": _git_revision(args.fixtures),
                },
            )
        for result in results:
            if result.classification != "success" and not result.expectation:
                print(
                    f"UNEXPECTED {result.interview} {result.phase}: {result.message or result.classification}",
                    file=sys.stderr,
                )
        return 0 if summary["unexpected"] == 0 else 1
    except (CorpusError, re.error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
