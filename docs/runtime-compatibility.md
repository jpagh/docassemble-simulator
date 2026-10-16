# docassemble / AssemblyLine runtime compatibility

The simulator runs the real docassemble compiler inside the target package's
interpreter. This document is the executable compatibility contract: which
dependency combinations are tested, what the simulator verifies, and what a
failure means.

## Tested matrix

A dependency lower bound is not evidence that every later release works
together. The matrix below is provisioned from the `da19` / `da110` dependency
groups in `pyproject.toml` and exercised by the runtime gate
(`mise run test:runtime`, contract files `tests/test_real_runtime.py` and
`tests/test_trace_real_runtime.py`).

| Runtime family | docassemble-base | docassemble-webapp | docassemble.AssemblyLine | docassemble.ALToolbox |
| --- | --- | --- | --- | --- |
| legacy 1.9.x | 1.9.13 | 1.9.13 | 4.8.0 | 0.19.0 |
| modern 1.10.x | 1.10.13 | 1.10.13 | 4.8.0 | 0.19.0 |

Both lanes run on CPython 3.12+; the AssemblyLine release requires it. The
`da19` / `da110` groups attach the AssemblyLine packages only on Python 3.12+
for that reason. Anything outside a tested row is unclaimed: the simulator
reports the installed pair instead of guessing.

The matrix deliberately keeps a family split. `docassemble.AssemblyLine 4.8.0`
reaches `docassemble.ALToolbox 0.19.0`, whose `markitdown[pdf]` chain needs a
`pdfminer-six` release newer than the exact pin in `docassemble-base 1.9.13`;
the lock keeps the legacy lane installable by selecting a compatible
`markitdown` and constraining `onnxruntime`. Reproduce the lanes with:

```sh
mise run test:runtime # provision both lanes and run the compatibility gate
mise run test:all-da  # aggregate lane: whole suite with both families wired
```

## Native build prerequisites

`docassemble-base` pins `numpy==2.2.6` and `scipy==1.15.3` in both families,
and neither pin publishes a cp314 wheel. With the CPython 3.14 interpreter the
lanes currently provision, both are built from source, and scipy's meson
configure needs a BLAS/LAPACK it can find through pkg-config or CMake.
Homebrew's `openblas` is keg-only, so its `openblas.pc` is not on the default
pkg-config search path. Export the path (or configure it in the machine's
global mise config) before provisioning:

```sh
PKG_CONFIG_PATH=/opt/homebrew/opt/openblas/lib/pkgconfig uv sync --group da110
```

Without it, provisioning fails while building `scipy==1.15.3` with
`Dependency "OpenBLAS" not found`. The first cold provision compiles numpy and
scipy (minutes) and caches the built wheels; later syncs reuse them. A lane
interpreter of CPython 3.12 or 3.13 avoids the source build entirely, because
both pins ship cp313 wheels. None of this reaches the published package: the
simulator itself depends only on `pyyaml`, and the target project's own
`docassemble-base` pin decides whether an install has to build scipy.

## Runtime gate

`mise run test:runtime` is the compatibility gate. It provisions both lanes,
exports `DASIMULATOR_REAL_PYTHON` and `DASIMULATOR_REAL_PYTHON_19`, and selects
the `real_runtime` contract tests, including screen-trace acceptance, with the
external corpus excluded. The gate uses `min(4, CPU count)` bounded
pytest-xdist workers by default, falling back to two when neither CPU probe
answers; set `DASIMULATOR_RUNTIME_WORKERS` to 1 for serial debugging or
comparison, or to any count in the accepted range (1–8). Every worker inherits
both explicit interpreter paths, and the contract's per-test temporary
workspaces keep sessions, files, traces, and packages isolated. `scripts/test-runtime` performs that selection
and preflights both lanes; it fails before pytest when a lane interpreter is
missing, cannot import the runtime, or reports the wrong family, and prints the
`mise run sync:*` command that provisions it. `tests/_runtime_gate.py` then
fails a session in which a required family contributed no passing test, or in
which a lane interpreter variable is absent, so a run cannot satisfy the gate
by skipping the coverage it exists to exercise. Individually skipped
capability tests (for example a lane without AssemblyLine) stay legitimate
skips. The gate reports pass/fail/skip counts for each family so serial and
parallel runs can be compared without treating a missing lane as a speedup.
On 2026-10-10, on the same macOS Apple Silicon machine with warmed runtime
caches, the full selection took 952.27 seconds serially (1 worker) and 517.19
seconds with 2 workers. Both runs reported 23 modern and 19 legacy passes with
no skips or failures; the difference is an observation, not a promised speedup.

`mise run test:all-da` remains the aggregate lane: it runs the whole suite
(fast tests, both runtime families, and corpus tests when their fixtures are
available) inside the modern lane interpreter. Prefer the gate for the
compatibility contract and `mise run test:corpus` for the external corpus
canary; neither is part of the other. The corpus gate provisions the modern
lane itself, validates the external fixture checkout before pytest, and fails
when the canary would otherwise pass by skipping (see README).

## ONNX Runtime telemetry teardown

The Linux aarch64 evidence below was revalidated with docassemble-base and
docassemble-webapp both at 1.10.13 in the modern lane and both at 1.9.13 in the
legacy lane, with ONNX Runtime 1.30.0 in each. The original modern observation
used a stale 1.10.10 environment, not the 1.10.13 matrix pin.

ONNX Runtime 1.30.0 (AssemblyLine → ALToolbox → markitdown → magika)
can race a telemetry HTTP callback during native static destruction on macOS,
then abort with `recursive_mutex lock failed: Invalid argument` after the worker
has acknowledged shutdown. Calling `onnxruntime.disable_telemetry_events()`
after import is insufficient: in both provisioned lanes on Linux aarch64,
plain import and import followed by that API still create
`$XDG_CACHE_HOME/Microsoft/DeveloperTools/.onnxruntime/deviceid` and
`onnxruntime.db` in a fresh cache home.

The simulator instead forces `ORT_DISABLE_TELEMETRY=1` before runtime imports:
in persistent-worker and corpus child environments, and at CLI entry for both
worker and in-process one-shot commands (including a macOS library-path
re-exec). This deliberately overrides an explicitly exported value: enabling
telemetry is not a safe opt-out from the teardown-race mitigation. Other
inherited environment values are preserved. This prevents the **1DS SDK
manager/uploader initialization**, not the existence of ONNX Runtime's
`PosixTelemetry` provider object.

Callers bypassing the CLI/runner launch seams — direct in-process bootstrap,
custom test harnesses, and `sitecustomize`/`.pth` startup hooks — must set
`ORT_DISABLE_TELEMETRY=1` **before any runtime bootstrap or `onnxruntime`
import**. Startup hooks run before CLI entry, so they must inherit the switch
from the launching environment if they load the runtime. The simulator cannot
undo native initialization that already happened. An import-order regression
in `tests/test_environment.py` pins that importing `docassemble_simulator.cli`
does not itself import `docassemble.*` or `onnxruntime`, allowing `cli.main()`
to apply the switch before runtime imports.

With that pre-import switch, neither the telemetry directory nor its files are
created on Linux, demonstrating prevention of the SDK initialization implicated
in the crash. The isolated-cache regression in
`tests/test_onnx_telemetry_real_runtime.py` verifies this through the actual
runner, worker CLI, and one-shot CLI launch paths in both lanes, without a
network service or a dependency-pin change. Its observation path follows
upstream `posix/device_id.cc`: Linux uses
`$XDG_CACHE_HOME/Microsoft/DeveloperTools/.onnxruntime`, while Apple ignores XDG
and uses `$HOME/Library/Application Support/Microsoft/DeveloperTools/.onnxruntime`.
The regression supports both platforms.

Positive controls (plain import and import plus `disable_telemetry_events()`
create `deviceid` and `onnxruntime.db`; the pre-import switch creates neither)
are **opt-in diagnostics**, skipped by default and isolated in subprocesses.
They deliberately enable telemetry, can emit network traffic, and can themselves
trigger the macOS abort (#73; microsoft/onnxruntime#33251). They are not part of
the default gate. To opt in with provisioned lanes:

```sh
DASIMULATOR_TELEMETRY_DIAGNOSTICS=1 \
DASIMULATOR_REAL_PYTHON="$PWD/.venv-da110/bin/python" \
DASIMULATOR_REAL_PYTHON_19="$PWD/.venv-da19/bin/python" \
uv run pytest -q tests/test_onnx_telemetry_real_runtime.py -k diagnostic -m real_runtime
```

The macOS SIGABRT was **not reproduced** on the Linux aarch64 development host.
Two separate Linux campaigns were recorded (these are not interchangeable
workload counts):

- The revalidation recorded here completed **three worker journeys plus three
  fresh-worker saved-session reloads per lane**, both idle and under two-process
  CPU load: complete screen traces, drained authored diagnostics, reloadable
  sessions, acknowledged shutdown, exit 0, no abort stderr, and no surviving
  children in both lanes.
- #73's closing comment records a separate run of **five idle plus five
  CPU-loaded journeys per lane**, fresh-worker saved-session reloads, and ten
  bare imports per lane, all exiting 0 without abort stderr.

Neither campaign proves that macOS exits are fixed. The exact
acknowledged-shutdown/SIGABRT/stderr exemption, its warning
attribution, and all negative tests in `tests/test_real_runtime.py` remain;
macOS repeated-journey/load validation is still required. Neither immediate
process exit nor blanket stderr suppression is used.

## Dependency pinning and security alerts

`docassemble-base` and `docassemble-webapp` pin their entire dependency tree
with `==`, so each lane installs the versions upstream chose rather than the
newest compatible ones. GitHub's dependency graph reports the resulting
advisories against `uv.lock`; they belong to the emulated runtime, not to the
simulator.

- The published package depends only on `pyyaml`, so no advisory reaching the
  lock through `da19`/`da110` reaches an installed `docassemble-simulator`.
- The lanes deliberately keep upstream's exact pins. Overriding them would make
  the real-runtime lane exercise a dependency set that no docassemble server
  runs, which is the one thing that lane exists to prevent.
- Refreshing a lane is a deliberate act, not a scheduled bump. The group
  specifiers float within `<1.11`, so `uv lock --upgrade` silently moves the
  lane: re-run `mise run test:runtime`, then update the matrix row above.

Four of those advisories have no patched release at all, so a fully green
security tab is not reachable while the matrix exists. The accepted-risk
decision, with the measured effect of each alternative, is recorded in
[ADR-0015](adr/0015-compatibility-matrix-alerts-are-accepted.md).

## Legacy namespace shims and setuptools

Some installed Interview packages still ship a `docassemble/__init__.py`
containing `__import__('pkg_resources').declare_namespace(__name__)` (or the
separate import/call equivalent). `setuptools>=81` no longer provides
`pkg_resources`; docassemble-base/webapp releases that pin setuptools 83.x
can therefore satisfy their dependency pins while every docassemble import
fails at that legacy shim. This is a packaging conflict, not a missing core
runtime or native-library problem.

Preflight identifies this failure from the import traceback and the installed
distribution's RECORD, reports the shim owner and installed setuptools version,
and leaves the environment unchanged. To recover reproducibly in a uv-managed
target project, add an explicit override to that project's `pyproject.toml`:

```toml
[tool.uv]
override-dependencies = ["setuptools<81"]
```

Merge the constraint into any existing override list, then run `uv lock` and
`uv sync` in the target project and retry `sim check --json`. This deliberately
overrides upstream's exact setuptools pin; it is a target-project recovery,
not a change to the simulator's tested compatibility lanes. A manual downgrade
alone is not durable: a plain environment sync restores the lock's exact pin
and can remove `pkg_resources` again. Alternatively, rebuild/reinstall the
identified shim distribution using native namespaces (without the legacy
`docassemble/__init__.py`). Do not edit the installed shim in place.

A missing `pkg_resources` outside an installed namespace shim remains an
ordinary broken-import error with its underlying cause; preflight does not
prescribe this override for unrelated failures.

## What the simulator verifies

At bootstrap the simulator mirrors the docassemble webapp's startup import
pass: installed `docassemble.<package>` modules that define classes (or opt in
with `# pre-load`, or call `docassemble.base.util.update`) are imported before
any interview is parsed. That pass is what registers custom datatypes such as
ALToolbox's `BirthDate`. It honors the effective `preloaded modules`, `module
whitelist`, and `module blacklist` configuration and skips `# do not pre-load`
modules exactly as the webapp does. Import failures are logged and skipped, not
fatal.

Before compiling the target Interview, the simulator compiles an in-memory
probe containing the standard AssemblyLine birthdate field metadata:

```yaml
fields:
  - Birthdate: simulator_probe_birthdate
    datatype: BirthDate
    alMonthLabel: ${ word('Month') }
    alDayLabel: ${ word('Day') }
    alYearLabel: ${ word('Year') }
```

The probe runs only when an AssemblyLine/ALToolbox distribution is installed in
the target interpreter. It never resolves a package file, installs, upgrades,
downgrades, or edits anything, and it never creates Working state or a Saved
session. Its result is memoized per process.

You can inspect the runtime family, installed versions, and tested matrix with
`sim info --json` (the `runtime_compatibility` object). The probe itself runs on
`check`, `questions`, `index`, and every execution command.

## Session persistence

AssemblyLine's baseline includes `al_saved_sessions.yml`, whose `initial: True`
block writes session metadata through `update_session_metadata`. That function
is PostgreSQL-only (`pg_advisory_xact_lock`, `jsonb`, `||`, `CAST(... AS
jsonb)`), and docassemble itself does not support SQLite for its server
database. The simulator therefore substitutes no database at all:

- the simulator's generated defaults contain no `db` section, so nothing
  silently points at a nonexistent PostgreSQL server;
- `assembly line.update session metadata` defaults to `false`, matching a local
  run that has no server session store. The CLI's own saved state remains
  `.simulator/sessions/`;
- the AssemblyLine initial block, saved answer sets, and the interview list are
  deployment-only features. The simulator does not rewrite installed
  AssemblyLine functions to make them work locally.

A project can opt back in when it has a real docassemble database:

```toml
# .config/simulator/config.toml
["assembly line"]
"update session metadata" = true

[db]
prefix = "postgresql+psycopg2://"
name = "docassemble"
user = "docassemble"
password = "..."
host = "localhost"
port = 5432
```

Point this at an existing docassemble database (the `jsonstorage` table must
already exist); the simulator does not create schemas. Without that database,
leaving the default off keeps complete main journeys — start, questions,
documents, downloads — free of database access.

## Failure taxonomy

| Condition | Command result | What to do |
| --- | --- | --- |
| Missing `docassemble-base`/`webapp` | `input` error, exit 1 | Let preflight acquire them, or install into the target interpreter; `--offline` refuses acquisition |
| Installed legacy namespace shim imports missing `pkg_resources` | `input` error, exit 1 naming the shim distribution and setuptools version | Override `setuptools<81` in the target project or rebuild the shim for native namespaces |
| Installed runtime fails to import (native library, broken dep) | `input` error, exit 1 naming the native-library boundary | Fix the interpreter's native libraries; this is not a missing-package problem |
| Missing include (`docassemble.AssemblyLine:...`) | `compile` error, exit 2 with the definition and include path | Fix the authored include or install a package that provides it |
| Authored Interview syntax error | `compile` error, exit 2 | Fix the Interview definition; the simulator does not downgrade or rewrite it |
| Unsupported docassemble/AssemblyLine pair | `runtime-compatibility` error, exit 2 | Provision a tested pair, or run in a target interpreter that has one |

A `runtime-compatibility` failure carries the runtime family, interpreter,
installed package versions, failing capability, the tested matrix, the
underlying parser error, and a recovery direction. It is emitted as the normal
JSON envelope and as human-readable `error:` / detail lines; no traceback is
part of the contract. A compatibility failure never creates or mutates a Saved
session.

The probe specifically does not reclassify authored Interview errors: once the
probe passes, an authored parse error keeps its `compile` result. Missing
includes, missing core runtime packages, and native-library import failures keep
their own distinct messages.

## Recovery

Compatibility failures are environment problems, not Interview defects. Do not
edit `site-packages`, fork the parser, or strip `alMonthLabel` / `alDayLabel` /
`alYearLabel` metadata. Instead:

1. Install a tested pair into the target interpreter, for example
   `docassemble-assemblyline==4.8.*` with `docassemble-ALToolbox>=0.19,<0.20`,
   using the same interpreter that runs the simulator.
2. Re-run `sim check --json`. A compatible environment compiles the probe,
   compiles the target's AssemblyLine include, and `sim start` reaches the first
   Screen outcome.
3. If the pair is still rejected, capture the `runtime-compatibility` error;
   it names the exact runtime family, versions, and failing capability so the
   incompatibility can be reported upstream with a minimal reproduction.
