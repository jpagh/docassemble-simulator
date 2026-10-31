from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from docassemble_simulator.demo_corpus import (
    CaseResult,
    CorpusBlocker,
    CorpusClassification,
    CorpusError,
    CorpusErrorKind,
    Expectation,
    FixtureCase,
    RuntimePackage,
    _network_sandbox_command,
    _prepare_nltk_data,
    apply_expectations,
    derive_blocker,
    discover_fixture_cases,
    load_expectations,
    resolve_provenance,
    run_case,
    run_corpus,
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


def test_run_corpus_preserves_input_order_when_cases_finish_out_of_order(
    monkeypatch, tmp_path: Path
) -> None:
    cases = tuple(
        FixtureCase(
            tmp_path / f"{name}.yml",
            Path(f"{name}.yml"),
            name,
            canonical_package_path=name,
        )
        for name in ("slow", "fast")
    )
    import threading

    started = []
    slow_started = threading.Event()

    def fake_run_case(case, _staged_root, _interpreter, phase, **_kwargs):
        started.append(case.relative_path.name)
        if case.relative_path.name == "slow.yml":
            slow_started.set()
            slow_started.wait(1)
        else:
            assert slow_started.wait(1)
        return CaseResult(
            case.canonical_package_path,
            case.relative_path.as_posix(),
            case.sha256,
            phase,
            ("python",),
            0,
            0,
            "success",
        )

    monkeypatch.setattr("docassemble_simulator.demo_corpus.run_case", fake_run_case)

    results = run_corpus(cases, tmp_path, "python", ["start"], jobs=2)

    assert started == ["slow.yml", "fast.yml"]
    assert [result.fixture for result in results] == ["slow.yml", "fast.yml"]


def test_run_corpus_bounds_active_workers(monkeypatch, tmp_path: Path) -> None:
    import threading
    import time

    cases = tuple(
        FixtureCase(
            tmp_path / f"{index}.yml",
            Path(f"{index}.yml"),
            str(index),
            canonical_package_path=str(index),
        )
        for index in range(5)
    )
    active = 0
    maximum = 0
    guard = threading.Lock()

    def fake_run_case(case, _staged_root, _interpreter, phase, **_kwargs):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with guard:
            active -= 1
        return CaseResult(
            case.canonical_package_path,
            case.relative_path.as_posix(),
            case.sha256,
            phase,
            ("python",),
            0,
            0,
            "success",
        )

    monkeypatch.setattr("docassemble_simulator.demo_corpus.run_case", fake_run_case)
    run_corpus(cases, tmp_path, "python", ["start"], jobs=2)

    assert maximum <= 2


def test_run_corpus_rejects_nonpositive_jobs(tmp_path: Path) -> None:
    with pytest.raises(CorpusError, match="jobs must be positive"):
        run_corpus([], tmp_path, "python", ["start"], jobs=0)


def test_nltk_data_is_reused_and_refresh_keeps_active_generation(
    monkeypatch, tmp_path: Path
) -> None:
    identity = {"schema": 1, "python": "3.14", "runtime": {}, "packages": []}
    downloads = []

    def fake_download(_interpreter, cache):
        downloads.append(cache)
        for package in ("omw-1.4", "wordnet", "wordnet_ic", "sentiwordnet"):
            (cache / "corpora" / package).mkdir(parents=True)
        return True

    monkeypatch.setattr(
        "docassemble_simulator.demo_corpus._nltk_cache_identity", lambda _: identity
    )
    monkeypatch.setattr(
        "docassemble_simulator.demo_corpus._download_nltk_data", fake_download
    )

    first = _prepare_nltk_data("python", tmp_path / "nltk")
    warm = _prepare_nltk_data("python", tmp_path / "nltk")
    assert first is not None
    assert warm == first

    (first / ".complete.json").unlink()
    recovered = _prepare_nltk_data("python", tmp_path / "nltk")
    refreshed = _prepare_nltk_data("python", tmp_path / "nltk", refresh=True)

    assert recovered is not None
    assert recovered != first
    assert refreshed is not None
    assert refreshed != recovered
    assert first.is_dir()
    assert len(downloads) == 3


def test_nltk_data_preparation_is_shared_by_concurrent_callers(
    monkeypatch, tmp_path: Path
) -> None:
    import time
    from concurrent.futures import ThreadPoolExecutor

    identity = {"schema": 1, "python": "3.14", "runtime": {}, "packages": []}
    downloads = []

    def fake_download(_interpreter, cache):
        downloads.append(cache)
        time.sleep(0.02)
        for package in ("omw-1.4", "wordnet", "wordnet_ic", "sentiwordnet"):
            (cache / "corpora" / package).mkdir(parents=True)
        return True

    monkeypatch.setattr(
        "docassemble_simulator.demo_corpus._nltk_cache_identity", lambda _: identity
    )
    monkeypatch.setattr(
        "docassemble_simulator.demo_corpus._download_nltk_data", fake_download
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda _: _prepare_nltk_data("python", tmp_path / "nltk"), range(2)
            )
        )

    assert results[0] is not None
    assert results[1] == results[0]
    assert len(downloads) == 1


def test_nltk_data_failed_preparation_does_not_publish_current(
    monkeypatch, tmp_path: Path
) -> None:
    identity = {"schema": 1, "python": "3.14", "runtime": {}, "packages": []}
    monkeypatch.setattr(
        "docassemble_simulator.demo_corpus._nltk_cache_identity", lambda _: identity
    )
    monkeypatch.setattr(
        "docassemble_simulator.demo_corpus._download_nltk_data", lambda *_: False
    )

    assert _prepare_nltk_data("python", tmp_path / "nltk") is None
    assert not list((tmp_path / "nltk").rglob("current"))


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
    assert report["blockers"] == {}
    assert (tmp_path / "report/results.jsonl").read_text().count("\n") == 1


def _failure_result(
    message: str,
    *,
    classification: str = "failure",
    error_kind: str = "execution",
    traceback_tail: str | None = None,
    returncode: int = 2,
) -> CaseResult:
    details = {"message": message}
    if traceback_tail is not None:
        details["traceback_tail"] = traceback_tail
    return CaseResult(
        "docassemble.base:data/questions/examples/sample.yml",
        "sample.yml",
        "hash",
        "start",
        ("python",),
        returncode,
        0.1,
        classification,
        error_kind,
        message,
        {"ok": False, "error": {"kind": error_kind, "details": details}},
    )


OPENAI_AUTH_TRACEBACK = (
    '  File ".../openai/_base_client.py", line 471, in _build_headers\n'
    "    self._validate_headers(headers_dict, custom_headers)\n"
    'TypeError: "Could not resolve authentication method. Expected either api_key '
    'or admin_api_key to be set."'
)
OAUTH_CONFIG_TRACEBACK = (
    '  File ".../docassemble/base/util.py", line 13716, in _get_flow\n'
    "    raise DAError('The application ' + self.appname + "
    '" is not configured in the Configuration")\n'
    "docassemble.base.error.DAError: The application mygoogle is not configured "
    "in the Configuration"
)
GOOGLE_CREDENTIALS_TRACEBACK = (
    '  File ".../docassemble/base/util.py", line 9894, in http\n'
    "    return google_auth_httplib2.AuthorizedHttp(self.cloud_credentials("
    "scopes=[scope]), http=httplib2.Http())\n"
    '  File ".../docassemble/base/util.py", line 9925, in cloud_credentials\n'
    "    return google_api().google_cloud_credentials(scopes=scopes)\n"
    "AttributeError: 'NoneType' object has no attribute 'google_cloud_credentials'"
)
DUMMY_OBJECT_TRACEBACK = (
    '  File ".../docassemble/base/parse.py", line 10187, in exec_with_trap\n'
    "    exec(the_question.compute, the_dict)\n"
    "AttributeError: 'DummyObject' object has no attribute 'predict'"
)
FAKE_REDIS_TRACEBACK = (
    '  File ".../docassemble/base/util.py", line 13801, in '
    "_get_random_unique_id\n"
    "    if r.setnx('da:' + self.appname + ':status:uniqueid:' + key, 'None'):\n"
    '  File ".../docassemble/base/util.py", line 9748, in __getattr__\n'
    "    return getattr(get_server_redis_user(), funcname)\n"
    "AttributeError: 'FakeRedis' object has no attribute 'setnx'"
)
REDIS_DECODE_TRACEBACK = (
    '  File ".../docassemble/base/util.py", line 9748, in __getattr__\n'
    "    return getattr(get_server_redis_user(), funcname)\n"
    "AttributeError: 'NoneType' object has no attribute 'decode'"
)
INDEX_CURSOR_TRACEBACK = (
    '  File ".../docassemble/demo/index.py", line 49, in report\n'
    "    with conn.cursor() as cur:\n"
    "         ^^^^^^^^^^^\n"
    "AttributeError: 'NoneType' object has no attribute 'cursor'"
)
SESSION_SECRET_TRACEBACK = (
    '  File ".../docassemble/base/functions.py", line 5271, in interview_list\n'
    "    (the_list, start_id) = user_interviews(user_id=user_id, "
    "secret=this_thread.current_info['secret'], exclude_invalid=exclude_invalid, "
    "action=action, filename=filename, session=session)\n"
    "KeyError: 'secret'"
)
TASK_PERFORMED_TRACEBACK = (
    '  File ".../docassemble/base/util.py", line 13478, in task_performed\n'
    "    if task in tasks and tasks[task]:\n"
    "TypeError: argument of type 'NoneType' is not a container or iterable"
)
GET_TIME_MESSAGE = (
    "HTTPConnectionPool(host='worldclockapi.com', port=80): Max retries exceeded "
    'with url: /api/json/est/now (Caused by NameResolutionError("HTTPConnection('
    "host='worldclockapi.com', port=80): Failed to resolve 'worldclockapi.com' "
    '([Errno 8] nodename nor servname provided, or not known)"))'
)


@pytest.mark.parametrize(
    ("message", "traceback_tail"),
    [
        (
            (
                "namespace rehydration failed: ModuleNotFoundError: "
                "No module named 'ics'"
            ),
            None,
        ),
        (
            (
                "namespace rehydration failed: ModuleNotFoundError: "
                "No module named 'google_drive_downloader'"
            ),
            None,
        ),
        (
            (
                '"Could not resolve authentication method. Expected either api_key '
                'or admin_api_key to be set."'
            ),
            OPENAI_AUTH_TRACEBACK,
        ),
        (
            "The application mygoogle is not configured in the Configuration",
            OAUTH_CONFIG_TRACEBACK,
        ),
        (
            "'NoneType' object has no attribute 'google_cloud_credentials'",
            GOOGLE_CREDENTIALS_TRACEBACK,
        ),
        (
            (
                "Error loading wordnet: <urlopen error [Errno 8] "
                "nodename nor servname provided>"
            ),
            None,
        ),
    ],
)
def test_derive_blocker_attributes_environment_evidence(
    message: str, traceback_tail: str | None
) -> None:
    result = _failure_result(message, traceback_tail=traceback_tail)

    assert derive_blocker(result) == CorpusBlocker.ENVIRONMENT
    assert result.as_dict()["blocker"] == "environment"


@pytest.mark.parametrize(
    ("message", "traceback_tail"),
    [
        (
            (
                "Got error 'DummyObject' object has no attribute 'predict' "
                "Traceback (most recent call last):"
            ),
            DUMMY_OBJECT_TRACEBACK,
        ),
        (
            "'FakeRedis' object has no attribute 'setnx'",
            FAKE_REDIS_TRACEBACK,
        ),
        (
            "Got error 'NoneType' object has no attribute 'decode'",
            REDIS_DECODE_TRACEBACK,
        ),
        (
            (
                "namespace rehydration failed: AttributeError: 'NoneType' object "
                "has no attribute 'Model'"
            ),
            None,
        ),
        (
            (
                "Got error 'NoneType' object has no attribute 'cursor' "
                "Traceback (most recent call last):"
            ),
            INDEX_CURSOR_TRACEBACK,
        ),
        ("'secret'", SESSION_SECRET_TRACEBACK),
        (
            "argument of type 'NoneType' is not a container or iterable",
            TASK_PERFORMED_TRACEBACK,
        ),
    ],
)
def test_derive_blocker_attributes_stubbed_service_evidence(
    message: str, traceback_tail: str | None
) -> None:
    result = _failure_result(message, traceback_tail=traceback_tail)

    assert derive_blocker(result) == CorpusBlocker.SERVICE
    assert result.as_dict()["blocker"] == "service"


@pytest.mark.parametrize(
    ("message", "traceback_tail"),
    [
        (GET_TIME_MESSAGE, None),
        (
            "urllib.error.URLError: <urlopen error [Errno 111] Connection refused>",
            None,
        ),
        (
            "socket.gaierror: [Errno 8] nodename nor servname provided, or not known",
            None,
        ),
    ],
)
def test_derive_blocker_attributes_network_evidence(
    message: str, traceback_tail: str | None
) -> None:
    result = _failure_result(message, traceback_tail=traceback_tail)

    assert derive_blocker(result) == CorpusBlocker.NETWORK
    assert result.as_dict()["blocker"] == "network"


@pytest.mark.parametrize(
    "message",
    [
        "division by zero",
        "There appears to be an infinite loop.  Variables in stack are bar, foo.",
        (
            "Interview has an error.  There was a reference to a variable "
            "'favorite_number' that could not be looked up in the question file"
        ),
        "'NoneType' object is not iterable",
        "'NoneType' object is not subscriptable",
        "object of type 'NoneType' has no len()",
        "'NoneType' object has no attribute 'help'",
        "failed to resolve 'friend'",
        "'NoneType' object has no attribute 'decode'",
        "'NoneType' object has no attribute 'cursor'",
        "'NoneType' object has no attribute 'Model'",
        "'NoneType' object has no attribute 'google_cloud_credentials'",
        "ModuleNotFoundError: No module named 'ics'",
        (
            "namespace rehydration failed: ModuleNotFoundError: "
            "No module named 'docassemble_simulator._runtime'"
        ),
        (
            "namespace rehydration failed: ModuleNotFoundError: "
            "No module named 'docassemble.base.util'"
        ),
        (
            "namespace rehydration failed: ModuleNotFoundError: "
            "No module named 'docassemble_mako'"
        ),
        "urlopen error unknown url type: 'file:///tmp/data.json'",
        "'secret'",
        "'discovery'",
        "Connection refused",
        "certificate verify failed",
        "[Errno 2] No such file or directory: 'https://docassemble.org/logo.jpg'",
    ],
)
def test_derive_blocker_without_observed_evidence_returns_none(message: str) -> None:
    result = _failure_result(message)

    assert derive_blocker(result) is None
    assert result.as_dict()["blocker"] is None


@pytest.mark.parametrize(
    "classification",
    [
        CorpusClassification.CRASH.value,
        CorpusClassification.TIMEOUT.value,
        CorpusClassification.PROTOCOL_ERROR.value,
        CorpusClassification.RUNTIME_DRIFT.value,
    ],
)
def test_derive_blocker_never_attributes_simulator_faults(
    classification: str,
) -> None:
    result = _failure_result(
        "namespace rehydration failed: ModuleNotFoundError: No module named 'ics'",
        classification=classification,
        returncode=3,
    )

    assert derive_blocker(result) is None


@pytest.mark.parametrize(
    "error_kind",
    [
        CorpusErrorKind.CRASH.value,
        CorpusErrorKind.PROTOCOL.value,
        CorpusErrorKind.TIMEOUT.value,
        CorpusErrorKind.RUNTIME_DRIFT.value,
        "fault",
    ],
)
def test_derive_blocker_never_attributes_harness_error_kinds(
    error_kind: str,
) -> None:
    result = _failure_result(
        "namespace rehydration failed: ModuleNotFoundError: No module named 'ics'",
        error_kind=error_kind,
    )

    assert derive_blocker(result) is None


def test_summary_reports_blockers_without_changing_expectations(
    tmp_path: Path,
) -> None:
    expected = _failure_result(
        "namespace rehydration failed: ModuleNotFoundError: No module named 'ics'"
    )
    expected = replace(
        expected,
        expectation="Reviewed optional package.",
        expectation_category="capability-boundary",
    )
    unexpected_network = _failure_result(GET_TIME_MESSAGE)
    unexpected_service = _failure_result(
        "Got error 'DummyObject' object has no attribute 'predict'",
        traceback_tail=DUMMY_OBJECT_TRACEBACK,
    )
    unexpected_plain = _failure_result("division by zero")
    success = CaseResult(
        "docassemble.base:data/questions/examples/ok.yml",
        "ok.yml",
        "hash",
        "start",
        ("python",),
        0,
        0.1,
        "success",
    )

    report = write_reports(
        [expected, unexpected_network, unexpected_service, unexpected_plain, success],
        tmp_path / "report",
    )

    assert report["blockers"] == {
        "environment": 1,
        "network": 1,
        "service": 1,
    }
    assert report["expected"] == 1
    assert report["unexpected"] == 3
    assert report["expectation_categories"] == {"capability-boundary": 1}
    assert report["counts"] == {"failure": 4, "success": 1}
    rows = [
        json.loads(line)
        for line in (tmp_path / "report/results.jsonl").read_text().splitlines()
    ]
    assert [row["blocker"] for row in rows] == [
        "environment",
        "network",
        "service",
        None,
        None,
    ]
    summary_markdown = (tmp_path / "report/summary.md").read_text()
    assert "Expected: 1" in summary_markdown
    assert "Unexpected: 3" in summary_markdown
    assert "| environment | 1 |" in summary_markdown
    assert "| network | 1 |" in summary_markdown
    assert "| service | 1 |" in summary_markdown


@pytest.mark.parametrize(
    "fields",
    [
        f'classification = "{CorpusClassification.TIMEOUT.value}"',
        f'classification = "{CorpusClassification.CRASH.value}"',
        f'classification = "{CorpusClassification.PROTOCOL_ERROR.value}"',
        f'error_kind = "{CorpusErrorKind.TIMEOUT.value}"',
        f'error_kind = "{CorpusErrorKind.CRASH.value}"',
        f'error_kind = "{CorpusErrorKind.PROTOCOL.value}"',
        f'error_kind = "{CorpusErrorKind.RUNTIME_DRIFT.value}"',
        'error_kind = "fault"',
        "returncode = 3",
    ],
)
def test_load_expectations_rejects_fault_masks(tmp_path: Path, fields: str) -> None:
    path = tmp_path / "expectations.toml"
    path.write_text(
        "[[expectations]]\n"
        'interview = "x"\n'
        'phase = "start"\n'
        f"{fields}\n"
        'reason = "bad"\n'
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


@pytest.mark.parametrize("explicit_environment", [False, True])
def test_run_subprocess_disables_onnx_telemetry_before_child_imports(
    monkeypatch, explicit_environment
):
    monkeypatch.setenv("ORT_DISABLE_TELEMETRY", "0")
    environment = dict(os.environ) if explicit_environment else None
    result = run_subprocess(
        [sys.executable, "-c", "import os; print(os.environ['ORT_DISABLE_TELEMETRY'])"],
        environment=environment,
    )

    assert result.returncode == 0
    assert result.stdout == "1\n"
    assert result.stderr == ""
