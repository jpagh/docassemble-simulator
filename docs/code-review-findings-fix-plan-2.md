# Code-review findings fix plan (follow-up)

Review scope: `main...HEAD` on `support-da19x` (5 commits), non-empty diff.
Spec source: GitHub issue #1 (support end-to-end simulator execution on docassemble 1.9.x).
Validation at review time: `ruff check` clean, `ruff format` clean, `tests/test_bootstrap.py` 34 passed.

## Findings being fixed

Standards (no documented standards in repo; all judgement calls):

- S1: duplicated capability probe in `tests/test_real_runtime.py` (`_probe_interpreter` vs `_runtime_family`).
- S2: repeated legacy-import/server-check shape in `_install_background_action_fallback()`.
- S3: inline missing-`server` message instead of `_incomplete_runtime_error()`.

Spec (issue #1):

- P1 (worst): story 16 background-action behavior proven at seam level only, no end-to-end 1.9 execution.
- P2: stories 14/23/24 render/attachment/date lanes parameterized but legacy-skipped without `DASIMULATOR_REAL_PYTHON_19`.
- P3: story 18 CLI-envelope evidence gap plus lazy `docassemble.base.config` import outside the hardened path.
- P4: tests assert private implementation structure (`server.bg_action is _foreground_background_action`).
- Scope boundary: `.mise.toml` hardcodes `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`; plan artifacts committed to `docs/`.

## 1. Unify the capability probe (S1)

File: `tests/test_real_runtime.py:13-85`.

`_probe_interpreter()` and `_runtime_family()` shell out the same
`find_spec('docassemble.base.thread_context')` snippet with different
failure semantics (fail/skip vs `"unknown"`).

Fix: extract one `_family_probe_cmd()` / `_query_family(interpreter, *, strict)`
helper. Strict mode preserves the fixture-setup fail-vs-skip behavior;
non-strict returns `"unknown"` on nonzero exit. Update the three call sites.
No behavior change.

## 2. Unify the legacy import/server check (S2)

File: `src/docassemble_simulator/_runtime.py:1131-1170`.

The `from docassemble.base import functions as …` plus
`getattr(…, "server", None)` shape repeats across the installed,
modern-missing, and legacy branches of `_install_background_action_fallback()`.

Fix: extract `_legacy_functions_or_none()` returning the module or `None`
(swallowing only `ModuleNotFoundError`/`ImportError`) and call it from all
three branches. Preserve the `_BACKGROUND_INSTALLED` re-assert behavior for
replaced legacy server objects.

## 3. Reuse the error helper (S3)

File: `src/docassemble_simulator/_runtime.py:855-865`.

`_register_legacy_runtime_bindings()` builds its missing-`server` message
inline. Replace with
`_incomplete_runtime_error("docassemble.base.functions.server")`.
Covered by the existing missing-server test.

## 4. End-to-end background action on both families (P1)

Story 16 requires foreground background actions to retain local semantics
under 1.9.x. The seam is installed and stub-tested, but no real-runtime
interview executes a background action end-to-end on 1.9.

Fix: add a minimal background-action block to the real-runtime fixture
(action ending in `background_response(...)`), then drive it through the
existing `family_python` lane: start, trigger the action, assert a completed
task value on both families. Preserve ADR-0003 (current working state, no
Celery queue/latency/retry emulation). Skips cleanly without
`DASIMULATOR_REAL_PYTHON_19`. Existing stub tests remain as installation
coverage only.

## 5. Make legacy coverage provable (P2)

Stories 14/23/24 (render-source, attachment, date lanes "under 1.9.x" /
"for both runtime families") are parameterized over `family_python`, but the
legacy lane skips when no 1.9 interpreter is provisioned.

Fix in two parts: (a) document and provision the 1.9 interpreter via
`DASIMULATOR_REAL_PYTHON_19` and the `scripts/test-real-runtime` second
argument so the lanes can run green on legacy; (b) add always-run stub-level
tests for the legacy render/attachment paths that do not need the
interpreter, so logic is covered even when the e2e lane skips.

Acceptance: modern lane always green; legacy e2e green when provisioned;
stub coverage green always.

## 6. Close the actionable-error gaps (P3)

Story 18 and the testing decisions require compatibility failures to surface
as structured, actionable CLI errors rather than raw tracebacks.

Fix: (a) add a CLI-envelope test that stubs a broken/incomplete runtime,
drives the CLI (or `cmd_execution`) path, and asserts a structured error
envelope naming the missing capability with no traceback — covering both the
`register_hooks()` and `runtime_context()` failure paths; (b) harden
`get_configuration()` (`_runtime.py:675-686`, called per context entry):
wrap `import docassemble.base.config` in
`try/except (ModuleNotFoundError, ImportError)` and raise
`_incomplete_runtime_error("docassemble.base.config")` chained.

## 7. Assert behavior, not structure (P4)

The testing decisions discourage asserting private implementation structure.
Replace `server.bg_action is _foreground_background_action` assertions with
observable behavior: disabled mode returns a pending task; foreground mode
with stubbed interview/namespace/status returns a `SUCCESS` task; reinstall
with a replaced server object still dispatches. Keeps idempotence coverage
without pinning the private symbol.

## Scope boundary

`.mise.toml` hardcodes `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`, which
sits against the issue's native-library boundary ("beyond documenting the
required macOS environment configuration and preserving the simulator's
existing library-path handling"). Either revert to document-only or keep with
an explicit macOS-dev-only comment; do not expand provisioning. Do not commit
further plan artifacts to `docs/` (use `.scratch/` or chat-only).

## Validation gates

* `ruff check .`
* `ruff format --check .`
* `pytest -q tests/test_bootstrap.py`
* Fast suite excluding `tests/test_real_runtime.py`
* `scripts/test-real-runtime <1.10-python> [<1.9-python>]`, or equivalent
  `DASIMULATOR_REAL_PYTHON` + `DASIMULATOR_REAL_PYTHON_19` run
* `git diff --check`

## Suggested order

S3, S2, S1 (trivial, isolated), then P3b, P3a, P4, P1, P2 (P1/P2 need a
provisioned 1.9 interpreter).

## Out of scope

Per issue #1: no CLI vocabulary, JSON envelope, operation-type,
saved-session, snapshot, or render-source changes; no support beyond the
declared 1.9.x floor and current 1.10+ family; no Redis, Celery, Flask,
database, or PDF-converter infrastructure; no preflight or launcher rewrite.
