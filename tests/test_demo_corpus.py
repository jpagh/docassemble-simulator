from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

from docassemble_simulator.demo_corpus import (
    CaseResult,
    CorpusError,
    Expectation,
    FixtureCase,
    RuntimePackage,
    _network_sandbox_command,
    apply_expectations,
    discover_fixture_cases,
    load_expectations,
    resolve_provenance,
    run_case,
    run_subprocess,
    select_cases,
    stage_corpus,
    write_reports,
)


def _runtime_package(root: Path, name: str, files: dict[str, str]) -> RuntimePackage:
    package = root / "docassemble" / name
    examples = package / "data" / "questions" / "examples"
    examples.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    for relative, text in files.items():
        destination = examples / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)
    return RuntimePackage(name=name, path=package)


def test_discover_fixture_cases_returns_sorted_yaml_files(tmp_path: Path) -> None:
    examples = tmp_path / "examples"
    examples.mkdir()
    (examples / "z.yml").write_text("question: Z\n")
    (examples / "a.yml").write_text("question: A\n")
    (examples / "ignored.yaml").write_text("question: ignored\n")

    cases = discover_fixture_cases(tmp_path)

    assert [case.relative_path.as_posix() for case in cases] == ["a.yml", "z.yml"]
    assert cases[0].sha256 == hashlib.sha256(b"question: A\n").hexdigest()


def test_resolve_provenance_uses_exact_runtime_content(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    (fixtures / "examples").mkdir(parents=True)
    fixture = fixtures / "examples" / "sample.yml"
    fixture.write_text("question: Demo\n")
    runtime = tmp_path / "runtime"
    packages = {
        "base": _runtime_package(runtime, "base", {"sample.yml": "question: Base\n"}),
        "demo": _runtime_package(runtime, "demo", {"sample.yml": "question: Demo\n"}),
    }

    case = resolve_provenance(discover_fixture_cases(fixtures)[0], packages)

    assert case.package == "demo"
    assert (
        case.canonical_package_path
        == "docassemble.demo:data/questions/examples/sample.yml"
    )
    assert case.runtime_drift is False


def test_resolve_provenance_assigns_unique_drifted_file(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    (fixtures / "examples").mkdir(parents=True)
    (fixtures / "examples" / "stage-one.yml").write_text("old content\n")
    runtime = tmp_path / "runtime"
    packages = {
        "base": _runtime_package(runtime, "base", {}),
        "demo": _runtime_package(runtime, "demo", {"stage-one.yml": "new content\n"}),
    }

    case = resolve_provenance(discover_fixture_cases(fixtures)[0], packages)

    assert case.package == "demo"
    assert case.runtime_drift is True


def test_resolve_provenance_rejects_unknown_fixture(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    (fixtures / "examples").mkdir(parents=True)
    (fixtures / "examples" / "unknown.yml").write_text("question: Unknown\n")
    runtime = tmp_path / "runtime"
    packages = {
        "base": _runtime_package(runtime, "base", {}),
        "demo": _runtime_package(runtime, "demo", {}),
    }

    with pytest.raises(CorpusError, match="not present in the selected runtime"):
        resolve_provenance(discover_fixture_cases(fixtures)[0], packages)


def test_stage_corpus_overlays_fixture_without_mutating_runtime(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    (fixtures / "examples").mkdir(parents=True)
    fixture = fixtures / "examples" / "sample.yml"
    fixture.write_text("question: Fixture\n")
    runtime = tmp_path / "runtime"
    package = _runtime_package(runtime, "base", {"sample.yml": "question: Runtime\n"})
    original = (package.path / "data/questions/examples/sample.yml").read_bytes()
    case = resolve_provenance(discover_fixture_cases(fixtures)[0], {"base": package})

    with stage_corpus(fixtures, {"base": package}, cases=[case]) as staged:
        target = staged.root / "docassemble/base/data/questions/examples/sample.yml"
        assert target.read_text() == "question: Fixture\n"
        assert staged.root / "docassemble/base/__init__.py"
        assert (target.stat().st_mode & 0o222) == 0

    assert (
        package.path / "data/questions/examples/sample.yml"
    ).read_bytes() == original


def test_select_cases_shards_stably_and_covers_the_input() -> None:
    cases = tuple(
        FixtureCase(
            Path(f"{name}.yml"),
            Path(f"{name}.yml"),
            name,
            canonical_package_path=name,
        )
        for name in ("a", "b", "c", "d", "e")
    )
    shards = [set(select_cases(cases, shard=(index, 2))) for index in (1, 2)]

    assert shards[0].isdisjoint(shards[1])
    assert shards[0] | shards[1] == set(cases)
    assert select_cases(cases, matcher=re.compile(r"^[ab]")) == cases[:2]


def _envelope_interpreter(tmp_path: Path, envelope: dict) -> Path:
    interpreter = tmp_path / "envelope-python"
    interpreter.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        f"print({json.dumps(json.dumps(envelope))})\n",
        encoding="utf-8",
    )
    interpreter.chmod(0o755)
    return interpreter


def test_start_rejects_unknown_outcome_kind(tmp_path: Path) -> None:
    case = FixtureCase(
        tmp_path / "sample.yml",
        Path("sample.yml"),
        "hash",
        package="base",
        canonical_package_path="docassemble.base:data/questions/examples/sample.yml",
    )
    staged_root = tmp_path / "staged"
    (staged_root / "docassemble/base").mkdir(parents=True)

    result = run_case(
        case,
        staged_root,
        _envelope_interpreter(tmp_path, {"ok": True, "result": {"kind": "mystery"}}),
        "start",
    )

    assert result.classification == "protocol_error"
    assert result.error_kind == "protocol"
    assert "outcome kind" in (result.message or "")


def test_runtime_drift_is_reported_without_running_interview(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    interpreter = tmp_path / "interpreter"
    interpreter.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    interpreter.chmod(0o755)
    case = FixtureCase(
        tmp_path / "sample.yml",
        Path("sample.yml"),
        "hash",
        package="base",
        canonical_package_path="docassemble.base:data/questions/examples/sample.yml",
        runtime_drift=True,
    )

    result = run_case(case, tmp_path, interpreter, "start")

    assert result.classification == "runtime_drift"
    assert not marker.exists()
    assert "docassemble-demo-case-" not in result.as_dict()["reproduction"]


def test_network_sandbox_denies_socket_access() -> None:
    if sys.platform != "darwin" and shutil.which("bwrap") is None:
        with pytest.raises(CorpusError, match="network-denial sandbox"):
            _network_sandbox_command(["python3", "-c", "pass"])
        return
    command = _network_sandbox_command(
        ["python3", "-c", "import socket; socket.socket().connect(('127.0.0.1', 1))"]
    )
    result = run_subprocess(command, timeout=5)

    assert result.returncode != 0
    assert "Operation not permitted" in result.stderr


def test_run_subprocess_terminates_the_process_group_on_timeout() -> None:
    result = run_subprocess(
        ["python3", "-c", "import time; time.sleep(10)"], timeout=0.05
    )

    assert result.timed_out is True
    assert result.elapsed_seconds < 2


def test_expectations_are_narrow_and_mark_only_matching_failures(
    tmp_path: Path,
) -> None:
    result = CaseResult(
        "docassemble.base:data/questions/examples/bad.yml",
        "bad.yml",
        "hash",
        "start",
        ("python",),
        2,
        0.1,
        "failure",
        "compile",
        "known problem",
    )
    expectation = Expectation(
        result.interview,
        result.phase,
        classification="failure",
        returncode=2,
        error_kind="compile",
        message="known problem",
        reason="The example intentionally uses a server-only feature.",
    )

    matched = apply_expectations([result], [expectation])
    assert matched[0].expectation == expectation.reason
    assert matched[0].expectation_category == expectation.category

    with pytest.raises(CorpusError, match="returncode, error_kind, and message"):
        apply_expectations(
            [result],
            [
                Expectation(
                    result.interview,
                    result.phase,
                    error_kind="compile",
                    reason="too broad",
                )
            ],
        )

    report = write_reports(matched, tmp_path / "report")
    assert report["error_kinds"] == {"compile": 1}
    assert report["outcome_kinds"] == {}
    assert report["expectation_categories"] == {"capability-boundary": 1}
    assert "reproduction" in (tmp_path / "report/results.jsonl").read_text()
    assert report["unexpected"] == 0
    assert (tmp_path / "report/results.jsonl").read_text().count("\n") == 1


def test_load_expectations_rejects_fault_masks(tmp_path: Path) -> None:
    path = tmp_path / "expectations.toml"
    path.write_text(
        '[[expectations]]\ninterview = "x"\nphase = "start"\n'
        'classification = "timeout"\nreason = "bad"\n'
    )

    with pytest.raises(CorpusError, match="cannot mask"):
        load_expectations(path)


def test_resolve_provenance_requires_override_for_identical_collision(
    tmp_path: Path,
) -> None:
    fixtures = tmp_path / "fixtures"
    (fixtures / "examples").mkdir(parents=True)
    (fixtures / "examples" / "same.yml").write_text("question: Same\n")
    runtime = tmp_path / "runtime"
    packages = {
        "base": _runtime_package(runtime, "base", {"same.yml": "question: Same\n"}),
        "demo": _runtime_package(runtime, "demo", {"same.yml": "question: Same\n"}),
    }

    with pytest.raises(CorpusError, match="ambiguous runtime provenance"):
        resolve_provenance(discover_fixture_cases(fixtures)[0], packages)

    case = resolve_provenance(
        discover_fixture_cases(fixtures)[0], packages, overrides={"same.yml": "base"}
    )
    assert case.package == "base"
