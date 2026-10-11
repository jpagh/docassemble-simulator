"""Regression coverage for the external corpus gate (issue #12).

The gate is ``mise run test:corpus``: it provisions the modern Runtime
compatibility lane, validates the external fixture checkout, and must not
report success when the corpus suite never ran or was entirely skipped. These
tests use controlled command substitutes, so they never provision a lane or
run the real corpus.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

from gate_fixtures import fake_interpreter, write_executable

PROJECT = Path(__file__).resolve().parents[1]
GATE = PROJECT / "scripts" / "test-corpus"
PLUGIN_DIR = PROJECT / "tests"

PYTHON_ENV = "DASIMULATOR_REAL_PYTHON"
JOBS_ENV = "DASIMULATOR_CORPUS_JOBS"
CACHE_ENV = "DASIMULATOR_NLTK_CACHE_DIR"
FIXTURES_ENV = "DASIMULATOR_DEMO_FIXTURES"
GATE_ENV = "DASIMULATOR_CORPUS_GATE"

DEFAULT_JOBS = "4"


def _fake_uv(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A controlled ``uv`` substitute: records argv/env and reports a status."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_log = tmp_path / "uv-args.txt"
    env_log = tmp_path / "uv-env.txt"
    write_executable(
        bin_dir / "uv",
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            : > "{args_log}"
            for argument in "$@"; do
              printf '%s\\n' "$argument" >> "{args_log}"
            done
            {{
              printf 'python=%s\\n' "${{{PYTHON_ENV}:-}}"
              printf 'fixtures=%s\\n' "${{{FIXTURES_ENV}:-}}"
              printf 'cache=%s\\n' "${{{CACHE_ENV}:-}}"
              printf 'jobs=%s\\n' "${{{JOBS_ENV}:-}}"
              printf 'gate=%s\\n' "${{{GATE_ENV}:-}}"
            }} > "{env_log}"
            exit "${{GATE_PROBE_UV_STATUS:-0}}"
            """
        ),
    )
    return bin_dir, args_log, env_log


def _fixture_tree(tmp_path: Path) -> Path:
    """A minimal corpus checkout: examples plus the demo package fixture."""
    fixtures = tmp_path / "fixtures"
    examples = fixtures / "examples"
    examples.mkdir(parents=True)
    (examples / "yesno.yml").write_text("question: Are you sure?\n")
    demo_package = fixtures / "demo_package"
    demo_package.mkdir()
    (demo_package / "demo_package.yml").write_text("metadata:\n  title: Demo\n")
    return fixtures


def _run_gate(
    tmp_path: Path,
    *,
    python: Path,
    fixtures: Path,
    extra_args: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "bash",
            str(GATE),
            "--python",
            str(python),
            "--fixtures",
            str(fixtures),
            *extra_args,
        ],
        cwd=PROJECT,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_corpus_task_provisions_the_modern_lane_and_runs_the_gate():
    tasks = tomllib.loads((PROJECT / ".mise.toml").read_text())["tasks"]
    corpus = tasks["test:corpus"]
    assert corpus["depends"] == ["sync:da110"]
    assert corpus["run"] == "scripts/test-corpus"


def test_corpus_gate_selects_only_corpus_marked_tests(tmp_path):
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
    fixtures = _fixture_tree(tmp_path)
    bin_dir, args_log, _ = _fake_uv(tmp_path)
    result = _run_gate(
        tmp_path,
        python=modern,
        fixtures=fixtures,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stdout + result.stderr

    argv = args_log.read_text().splitlines()
    assert argv[:2] == ["run", "pytest"]
    selection = argv[argv.index("-m") + 1]
    assert selection == "corpus"
    assert "-n" not in argv, "the corpus gate must not add pytest-xdist workers"


def test_corpus_gate_wires_lane_fixtures_cache_jobs_and_gate_environment(tmp_path):
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
    fixtures = _fixture_tree(tmp_path)
    bin_dir, _, env_log = _fake_uv(tmp_path)
    result = _run_gate(
        tmp_path,
        python=modern,
        fixtures=fixtures,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stdout + result.stderr

    recorded = dict(
        line.split("=", 1) for line in env_log.read_text().splitlines() if line
    )
    assert recorded["python"] == str(modern)
    assert recorded["fixtures"] == str(fixtures)
    assert recorded["jobs"] == DEFAULT_JOBS
    assert recorded["gate"] == "1"
    cache = Path(recorded["cache"])
    assert cache.is_absolute(), cache
    assert cache.parts[-2:] == (".simulator", "nltk-cache"), cache
    assert "modern" in result.stdout


def test_corpus_gate_honours_explicit_cache_and_worker_overrides(tmp_path):
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
    fixtures = _fixture_tree(tmp_path)
    shared_cache = tmp_path / "shared-nltk-cache"
    bin_dir, _, env_log = _fake_uv(tmp_path)
    result = _run_gate(
        tmp_path,
        python=modern,
        fixtures=fixtures,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            CACHE_ENV: str(shared_cache),
            JOBS_ENV: "2",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr

    recorded = dict(
        line.split("=", 1) for line in env_log.read_text().splitlines() if line
    )
    assert recorded["cache"] == str(shared_cache)
    assert recorded["jobs"] == "2"


def test_corpus_gate_rejects_an_invalid_worker_count_before_pytest(tmp_path):
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
    fixtures = _fixture_tree(tmp_path)
    bin_dir, args_log, _ = _fake_uv(tmp_path)
    result = _run_gate(
        tmp_path,
        python=modern,
        fixtures=fixtures,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            JOBS_ENV: "0",
        },
    )
    assert result.returncode != 0
    assert JOBS_ENV in result.stderr
    assert not args_log.exists(), "an invalid worker count must fail before pytest"


def test_corpus_gate_fails_before_pytest_when_the_lane_is_missing(tmp_path):
    fixtures = _fixture_tree(tmp_path)
    bin_dir, args_log, _ = _fake_uv(tmp_path)
    result = _run_gate(
        tmp_path,
        python=tmp_path / "absent" / "python",
        fixtures=fixtures,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode != 0
    assert "mise run sync:da110" in result.stderr
    assert not args_log.exists()


def test_corpus_gate_fails_before_pytest_when_the_lane_cannot_import_docassemble(
    tmp_path,
):
    fixtures = _fixture_tree(tmp_path)
    bin_dir, args_log, _ = _fake_uv(tmp_path)
    broken = write_executable(
        tmp_path / "broken-python",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            echo "ModuleNotFoundError: No module named 'docassemble'" >&2
            exit 1
            """
        ),
    )
    result = _run_gate(
        tmp_path,
        python=broken,
        fixtures=fixtures,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode != 0
    assert "cannot import the docassemble runtime" in result.stderr
    assert "ModuleNotFoundError" in result.stderr
    assert "mise run sync:da110" in result.stderr
    assert not args_log.exists()


def test_corpus_gate_rejects_a_lane_from_the_wrong_family(tmp_path):
    fixtures = _fixture_tree(tmp_path)
    bin_dir, args_log, _ = _fake_uv(tmp_path)
    legacy = fake_interpreter(tmp_path, "legacy-python", "legacy", "1.9.13")
    result = _run_gate(
        tmp_path,
        python=legacy,
        fixtures=fixtures,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode != 0
    assert "modern" in result.stderr and "legacy" in result.stderr
    assert "mise run sync:da110" in result.stderr
    assert not args_log.exists()


def test_corpus_gate_fails_before_pytest_when_fixtures_are_missing(tmp_path):
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
    bin_dir, args_log, _ = _fake_uv(tmp_path)
    result = _run_gate(
        tmp_path,
        python=modern,
        fixtures=tmp_path / "absent" / "fixtures",
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode != 0
    assert FIXTURES_ENV in result.stderr
    assert "docassemble-yaml" in result.stderr
    assert not args_log.exists()


def test_corpus_gate_rejects_a_fixture_checkout_without_examples(tmp_path):
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
    fixtures = tmp_path / "fixtures"
    (fixtures / "examples").mkdir(parents=True)
    (fixtures / "demo_package").mkdir()
    bin_dir, args_log, _ = _fake_uv(tmp_path)
    result = _run_gate(
        tmp_path,
        python=modern,
        fixtures=fixtures,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode != 0
    assert "examples" in result.stderr
    assert FIXTURES_ENV in result.stderr
    assert not args_log.exists()


def test_corpus_gate_rejects_a_fixture_checkout_without_the_demo_package(tmp_path):
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
    fixtures = tmp_path / "fixtures"
    (fixtures / "examples").mkdir(parents=True)
    (fixtures / "examples" / "yesno.yml").write_text("question: Are you sure?\n")
    bin_dir, args_log, _ = _fake_uv(tmp_path)
    result = _run_gate(
        tmp_path,
        python=modern,
        fixtures=fixtures,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode != 0
    assert "demo_package" in result.stderr
    assert FIXTURES_ENV in result.stderr
    assert not args_log.exists()


def test_corpus_gate_explains_an_empty_selection(tmp_path):
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
    fixtures = _fixture_tree(tmp_path)
    bin_dir, _, _ = _fake_uv(tmp_path)
    result = _run_gate(
        tmp_path,
        python=modern,
        fixtures=fixtures,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GATE_PROBE_UV_STATUS": "5",
        },
    )
    assert result.returncode == 5
    assert "selected no tests" in result.stderr


def test_corpus_gate_propagates_pytest_failures(tmp_path):
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
    fixtures = _fixture_tree(tmp_path)
    bin_dir, _, _ = _fake_uv(tmp_path)
    result = _run_gate(
        tmp_path,
        python=modern,
        fixtures=fixtures,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GATE_PROBE_UV_STATUS": "1",
        },
    )
    assert result.returncode == 1


CORPUS_PROBE_TESTS = textwrap.dedent(
    """\
    import pytest


    @pytest.mark.corpus
    def test_corpus_contract():
        pass


    @pytest.mark.corpus
    @pytest.mark.skip(reason="optional fixture unavailable")
    def test_corpus_optional_capability():
        raise AssertionError("must stay skipped")
    """
)


SKIPPED_CORPUS_PROBE_TESTS = textwrap.dedent(
    """\
    import pytest


    @pytest.mark.corpus
    @pytest.mark.skip(reason="fixtures unavailable")
    def test_corpus_contract():
        raise AssertionError("must stay skipped")
    """
)


def _run_plugin_probe(
    tmp_path: Path,
    *,
    tests: str = CORPUS_PROBE_TESTS,
    env: dict[str, str] | None = None,
    extra_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    root = tmp_path / "probe"
    root.mkdir()
    (root / "test_corpus_probe.py").write_text(tests)
    environment = {
        **os.environ,
        "PYTHONPATH": str(PLUGIN_DIR),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_ADDOPTS": "",
        **(env or {}),
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "_corpus_gate",
            "-q",
            *extra_args,
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_corpus_plugin_accepts_a_session_with_a_passing_corpus_test(tmp_path):
    result = _run_plugin_probe(tmp_path, env={GATE_ENV: "1"})
    assert result.returncode == 0, result.stdout + result.stderr


def test_corpus_plugin_rejects_an_entirely_skipped_corpus_suite(tmp_path):
    result = _run_plugin_probe(
        tmp_path, tests=SKIPPED_CORPUS_PROBE_TESTS, env={GATE_ENV: "1"}
    )
    assert result.returncode != 0
    assert "corpus gate" in result.stdout
    assert "no passing corpus coverage" in result.stdout
    assert FIXTURES_ENV in result.stdout


def test_corpus_plugin_stays_inert_without_the_gate_environment(tmp_path):
    result = _run_plugin_probe(tmp_path, tests=SKIPPED_CORPUS_PROBE_TESTS)
    assert result.returncode == 0, result.stdout + result.stderr


def test_corpus_plugin_preserves_optional_skips_alongside_a_passing_test(tmp_path):
    result = _run_plugin_probe(tmp_path, env={GATE_ENV: "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed, 1 skipped" in result.stdout
