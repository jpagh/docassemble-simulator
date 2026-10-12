"""Regression coverage for the two-family runtime gate (issue #11).

The gate is ``mise run test:runtime``: it provisions both Runtime compatibility
lanes, targets their interpreters explicitly, and must not report success when a
required family contributed no passing coverage. These tests use controlled
command substitutes, so they never provision a lane or run the real contract.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest
from gate_fixtures import fake_interpreter, write_executable

PROJECT = Path(__file__).resolve().parents[1]
GATE = PROJECT / "scripts" / "test-runtime"
PLUGIN_DIR = PROJECT / "tests"

MODERN_ENV = "DASIMULATOR_REAL_PYTHON"
LEGACY_ENV = "DASIMULATOR_REAL_PYTHON_19"
GATE_ENV = "DASIMULATOR_RUNTIME_GATE"


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
              printf 'modern=%s\\n' "${{{MODERN_ENV}:-}}"
              printf 'legacy=%s\\n' "${{{LEGACY_ENV}:-}}"
              printf 'gate=%s\\n' "${{{GATE_ENV}:-}}"
            }} > "{env_log}"
            exit "${{GATE_PROBE_UV_STATUS:-0}}"
            """
        ),
    )
    return bin_dir, args_log, env_log


def _fake_cpu_probe(
    bin_dir: Path, name: str, output: str = "", *, status: int = 0
) -> None:
    """Write a controlled ``getconf``/``sysctl`` substitute."""
    body = "#!/usr/bin/env bash\n"
    if output:
        body += f"printf '%s\\n' \"{output}\"\n"
    body += f"exit {status}\n"
    write_executable(bin_dir / name, body)


def _run_gate(
    tmp_path: Path,
    *,
    modern: Path,
    legacy: Path,
    extra_args: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
    workers: str | None = "2",
) -> subprocess.CompletedProcess:
    environment = {**os.environ}
    if workers is None:
        environment.pop("DASIMULATOR_RUNTIME_WORKERS", None)
    else:
        environment["DASIMULATOR_RUNTIME_WORKERS"] = workers
    environment.update(env or {})
    return subprocess.run(
        [
            "bash",
            str(GATE),
            "--modern",
            str(modern),
            "--legacy",
            str(legacy),
            *extra_args,
        ],
        cwd=PROJECT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_runtime_gate_task_provisions_lanes_before_running_the_gate():
    tasks = tomllib.loads((PROJECT / ".mise.toml").read_text())["tasks"]
    gate = tasks["test:runtime"]
    assert set(gate["depends"]) == {"sync:da19", "sync:da110"}
    assert gate["run"] == "scripts/test-runtime"

    for task_name, group, environment in (
        ("sync:da19", "da19", ".venv-da19"),
        ("sync:da110", "da110", ".venv-da110"),
    ):
        command = tasks[task_name]["run"]
        assert f"--group {group}" in command, command
        assert f"UV_PROJECT_ENVIRONMENT={environment}" in command, command

    # The aggregate lane keeps provisioning both families.
    assert set(tasks["test:all-da"]["depends"]) == {"sync:da19", "sync:da110"}


def test_gate_scripts_share_the_runtime_lane_preflight():
    shared = (PROJECT / "scripts" / "runtime-lane-preflight").read_text()
    corpus = (PROJECT / "scripts" / "test-corpus").read_text()
    runtime = GATE.read_text()

    assert "runtime_lane_preflight" in shared
    assert 'source "$repo_root/scripts/runtime-lane-preflight"' in runtime
    assert 'source "$repo_root/scripts/runtime-lane-preflight"' in corpus
    assert "runtime_lane_preflight" in runtime
    assert "runtime_lane_preflight" in corpus
    assert "def probe_lane" not in runtime
    assert "def probe_lane" not in corpus


def test_runtime_gate_selects_the_runtime_contract_without_the_external_corpus():
    script = GATE.read_text()
    assert "real_runtime and not corpus" in script


@pytest.mark.parametrize(
    "name, family, version",
    [("modern", "modern", "1.10.13"), ("legacy", "legacy", "1.9.13")],
)
def test_runtime_gate_wires_both_lane_interpreters(tmp_path, name, family, version):
    bin_dir, args_log, env_log = _fake_uv(tmp_path)
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
    legacy = fake_interpreter(tmp_path, "legacy-python", "legacy", "1.9.13")
    result = _run_gate(
        tmp_path,
        modern=modern,
        legacy=legacy,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{name} lane -> {family} (docassemble-base {version}" in result.stdout

    argv = args_log.read_text().splitlines()
    assert argv[0] == "run"
    assert argv[1] == "pytest"
    assert "--durations=15" in argv
    assert "-m" in argv
    selection = argv[argv.index("-m") + 1]
    assert selection == "real_runtime and not corpus"
    assert argv[argv.index("-n") + 1] == "2"
    assert "2 worker(s)" in result.stdout

    recorded = dict(
        line.split("=", 1) for line in env_log.read_text().splitlines() if line
    )
    assert recorded["modern"] == str(modern)
    assert recorded["legacy"] == str(legacy)
    assert recorded["gate"] == "1"


def test_runtime_gate_sizes_workers_to_the_machine(tmp_path):
    bin_dir, args_log, _ = _fake_uv(tmp_path)
    _fake_cpu_probe(bin_dir, "getconf", "16")
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
    legacy = fake_interpreter(tmp_path, "legacy-python", "legacy", "1.9.13")
    result = _run_gate(
        tmp_path,
        modern=modern,
        legacy=legacy,
        workers=None,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 worker(s)" in result.stdout
    argv = args_log.read_text().splitlines()
    assert argv[argv.index("-n") + 1] == "4"


def test_runtime_gate_scales_down_with_a_small_machine(tmp_path):
    bin_dir, args_log, _ = _fake_uv(tmp_path)
    _fake_cpu_probe(bin_dir, "getconf", "2")
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
    legacy = fake_interpreter(tmp_path, "legacy-python", "legacy", "1.9.13")
    result = _run_gate(
        tmp_path,
        modern=modern,
        legacy=legacy,
        workers=None,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 worker(s)" in result.stdout
    argv = args_log.read_text().splitlines()
    assert argv[argv.index("-n") + 1] == "2"


def test_runtime_gate_falls_back_to_a_bounded_default_without_a_cpu_probe(tmp_path):
    bin_dir, args_log, _ = _fake_uv(tmp_path)
    _fake_cpu_probe(bin_dir, "getconf", status=1)
    _fake_cpu_probe(bin_dir, "sysctl", status=1)
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
    legacy = fake_interpreter(tmp_path, "legacy-python", "legacy", "1.9.13")
    result = _run_gate(
        tmp_path,
        modern=modern,
        legacy=legacy,
        workers=None,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 worker(s)" in result.stdout
    argv = args_log.read_text().splitlines()
    assert argv[argv.index("-n") + 1] == "4"


def test_runtime_gate_uses_sysctl_when_getconf_is_unavailable(tmp_path):
    bin_dir, args_log, _ = _fake_uv(tmp_path)
    _fake_cpu_probe(bin_dir, "getconf", status=1)
    _fake_cpu_probe(bin_dir, "sysctl", "3")
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
    legacy = fake_interpreter(tmp_path, "legacy-python", "legacy", "1.9.13")
    result = _run_gate(
        tmp_path,
        modern=modern,
        legacy=legacy,
        workers=None,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 worker(s)" in result.stdout
    argv = args_log.read_text().splitlines()
    assert argv[argv.index("-n") + 1] == "3"


def test_runtime_gate_supports_serial_debugging(tmp_path):
    bin_dir, args_log, _ = _fake_uv(tmp_path)
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
    legacy = fake_interpreter(tmp_path, "legacy-python", "legacy", "1.9.13")
    result = _run_gate(
        tmp_path,
        modern=modern,
        legacy=legacy,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "DASIMULATOR_RUNTIME_WORKERS": "1",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 worker(s)" in result.stdout
    assert "-n" not in args_log.read_text().splitlines()


def test_runtime_gate_rejects_an_unbounded_worker_override(tmp_path):
    bin_dir, args_log, _ = _fake_uv(tmp_path)
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
    legacy = fake_interpreter(tmp_path, "legacy-python", "legacy", "1.9.13")
    result = _run_gate(
        tmp_path,
        modern=modern,
        legacy=legacy,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "DASIMULATOR_RUNTIME_WORKERS": "9",
        },
    )
    assert result.returncode == 2
    assert "from 1 to 8" in result.stderr
    assert not args_log.exists()


def test_runtime_gate_fails_before_pytest_when_a_lane_is_missing(tmp_path):
    bin_dir, args_log, _ = _fake_uv(tmp_path)
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
    result = _run_gate(
        tmp_path,
        modern=modern,
        legacy=tmp_path / "absent" / "python",
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode != 0
    assert "mise run sync:da19" in result.stderr
    assert not args_log.exists()


def test_runtime_gate_fails_before_pytest_when_a_lane_cannot_import_docassemble(
    tmp_path,
):
    bin_dir, args_log, _ = _fake_uv(tmp_path)
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
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
        modern=modern,
        legacy=broken,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode != 0
    assert "cannot import the docassemble runtime" in result.stderr
    assert "ModuleNotFoundError" in result.stderr
    assert "mise run sync:da19" in result.stderr
    assert not args_log.exists()


def test_runtime_gate_rejects_a_lane_from_the_wrong_family(tmp_path):
    bin_dir, args_log, _ = _fake_uv(tmp_path)
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
    wrong_family = fake_interpreter(tmp_path, "wrong-python", "modern", "1.10.13")
    result = _run_gate(
        tmp_path,
        modern=modern,
        legacy=wrong_family,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode != 0
    assert "legacy" in result.stderr and "modern" in result.stderr
    assert "mise run sync:da19" in result.stderr
    assert not args_log.exists()


def test_runtime_gate_propagates_pytest_failures(tmp_path):
    bin_dir, _, _ = _fake_uv(tmp_path)
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
    legacy = fake_interpreter(tmp_path, "legacy-python", "legacy", "1.9.13")
    result = _run_gate(
        tmp_path,
        modern=modern,
        legacy=legacy,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GATE_PROBE_UV_STATUS": "1",
        },
    )
    assert result.returncode == 1


def test_runtime_gate_explains_an_empty_selection(tmp_path):
    bin_dir, _, _ = _fake_uv(tmp_path)
    modern = fake_interpreter(tmp_path, "modern-python", "modern", "1.10.13")
    legacy = fake_interpreter(tmp_path, "legacy-python", "legacy", "1.9.13")
    result = _run_gate(
        tmp_path,
        modern=modern,
        legacy=legacy,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GATE_PROBE_UV_STATUS": "5",
        },
    )
    assert result.returncode == 5
    assert "selected no tests" in result.stderr


PROBE_TESTS = textwrap.dedent(
    """\
    import os

    import pytest


    @pytest.fixture
    def real_python():
        return "modern"


    @pytest.fixture
    def real_python_19():
        return "legacy"


    @pytest.fixture(params=["modern", "legacy"])
    def family_python(request, real_python, real_python_19):
        if request.param == "legacy" and os.environ.get("PROBE_SKIP_LEGACY") == "1":
            pytest.skip("legacy lane unavailable")
        return request.param


    def test_modern_contract(real_python):
        assert real_python == "modern"


    def test_family_contract(family_python):
        assert family_python in ("modern", "legacy")
    """
)


def _run_plugin_probe(
    tmp_path: Path,
    *,
    tests: str = PROBE_TESTS,
    env: dict[str, str] | None = None,
    extra_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    root = tmp_path / "probe"
    root.mkdir()
    (root / "test_gate_probe.py").write_text(tests)
    environment = {
        **os.environ,
        "PYTHONPATH": str(PLUGIN_DIR),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_ADDOPTS": "",
        MODERN_ENV: str(tmp_path / "modern-python"),
        LEGACY_ENV: str(tmp_path / "legacy-python"),
        "PROBE_SKIP_LEGACY": "",
        **(env or {}),
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "_runtime_gate",
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


def _active_gate_env() -> dict[str, str]:
    return {GATE_ENV: "1"}


def test_gate_plugin_accepts_a_session_where_both_families_pass(tmp_path):
    result = _run_plugin_probe(tmp_path, env=_active_gate_env())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "runtime gate coverage:" in result.stdout
    assert "modern: passed=" in result.stdout
    assert "legacy: passed=" in result.stdout


def test_gate_plugin_aggregates_reports_from_parallel_workers(tmp_path):
    pytest.importorskip("xdist")
    result = _run_plugin_probe(
        tmp_path,
        env=_active_gate_env(),
        extra_args=("-p", "xdist.plugin", "-n", "2"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "runtime gate coverage:" in result.stdout
    assert "modern: passed=" in result.stdout
    assert "legacy: passed=" in result.stdout


def test_gate_plugin_rejects_a_session_where_one_family_never_ran(tmp_path):
    result = _run_plugin_probe(
        tmp_path, env={**_active_gate_env(), "PROBE_SKIP_LEGACY": "1"}
    )
    assert result.returncode != 0
    assert "legacy" in result.stdout
    assert "coverage" in result.stdout


def test_gate_plugin_preserves_individually_skipped_capability_tests(tmp_path):
    tests = PROBE_TESTS + textwrap.dedent(
        """\

        @pytest.mark.skip(reason="capability boundary")
        def test_optional_legacy_capability(real_python_19):
            raise AssertionError("must stay skipped")
        """
    )
    result = _run_plugin_probe(tmp_path, tests=tests, env=_active_gate_env())
    assert result.returncode == 0, result.stdout + result.stderr


def test_gate_plugin_rejects_a_missing_lane_interpreter_environment(tmp_path):
    result = _run_plugin_probe(
        tmp_path,
        env={**_active_gate_env(), LEGACY_ENV: ""},
    )
    assert result.returncode != 0
    assert LEGACY_ENV in result.stdout


def test_gate_plugin_rejects_an_empty_selection(tmp_path):
    tests = textwrap.dedent(
        """\
        def test_unrelated():
            pass
        """
    )
    result = _run_plugin_probe(
        tmp_path,
        tests=tests,
        env=_active_gate_env(),
        extra_args=("-m", "real_runtime"),
    )
    assert result.returncode != 0
    assert "no collected test exercises the modern runtime family" in result.stdout


def test_gate_plugin_stays_inert_without_the_gate_environment(tmp_path):
    tests = textwrap.dedent(
        """\
        import pytest


        @pytest.fixture(params=["modern", "legacy"])
        def family_python(request):
            if request.param == "legacy":
                pytest.skip("legacy lane unavailable")
            return request.param


        def test_modern(family_python):
            pass
        """
    )
    result = _run_plugin_probe(tmp_path, tests=tests)
    assert result.returncode == 0, result.stdout + result.stderr
