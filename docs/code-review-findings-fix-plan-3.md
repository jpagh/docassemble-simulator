# Code-review findings fix plan (PR #2)

Review scope: `origin/main...HEAD` on `support-da19x` (11 commits), non-empty diff.
Spec source: GitHub issue #1 (support end-to-end simulator execution on docassemble 1.9.x).
Standards sources: `CONTEXT.md`, `docs/adr/0001`–`0005`.
Validation at review time: `mise run lint` clean, full suite 182 passed, cross-family lanes green.

Excluded by decision: the `.mise.toml` `DYLD_FALLBACK_LIBRARY_PATH` change is
intentional (needed to get testing working locally) and stays as-is.

## Findings being fixed

Standards (hard, documented ADR breaches):

- S1: tests toggle private install globals (`_BACKGROUND_INSTALLED`,
  `_ATTACHMENT_FALLBACK_INSTALLED`) instead of activating a runtime
  (`tests/stub_runtime.py:173`, `tests/test_bootstrap.py:715`) — ADR-0005.
- S2: runtime-compatibility tests in `tests/test_bootstrap.py` assert private
  seams / implementation structure rather than observable behavior — ADR-0002.
- S3: PDF-only finalization now raises `PDFConversionUnavailable`, but ADR-0001
  says PDF-only attachments "produce no PDF artifact and do not fail the
  interview" and explicitly rejected "fail when PDF is requested"
  (`tests/test_bootstrap.py:712-728`, `_runtime.py` `finalize_with_filename`).

Spec (issue #1):

- P1 (worst): `_LegacyContextAdapter.activate()` overwrites
  `functions.server.daconfig` on entry and never restores it, so nested
  operations leak inner configuration outward — violates the adapter
  "must restore prior state in a `finally` path" decision and story 8.
- P2: the cross-family start/answer contract stops at `answer → ok`; the spec
  requires asserting "the next outcome or completion" (story 3, testing
  decisions). Secondary: no automated gate runs the 1.9 lane.

Smells (judgement calls, baseline):

- J1: `_patch_legacy_background_seam()` and the modern branch of
  `_install_background_action_fallback()` repeat the same
  `bg_action`/`background_action`/`util` patch shape (`_runtime.py:1164-1180`,
  `:1232-1240`).
- J2: the "delete every `docassemble` module" loop repeats across
  `tests/test_bootstrap.py` (4 tests) and `tests/test_cli.py:350`.
- J3: `_query_family(..., description="")` parameter is never used
  (`tests/test_real_runtime.py:48`).

## 1. Restore `server.daconfig` on legacy context exit (P1)

File: `src/docassemble_simulator/_runtime.py`, `_LegacyContextAdapter.activate()`.

The adapter captures `previous = dict(vars(thread))` but sets
`functions.server.daconfig = self.config_loader()` outside that capture, so
the prior config is lost. Fix inside the same `try/finally`:

- Before overwriting, capture `previous_daconfig = functions.server.daconfig`
  (defensive `getattr`, since the install path always sets it but a stub may
  not).
- In the `finally` block, after `restore_thread_variables(previous)`, restore
  `functions.server.daconfig = previous_daconfig`.

Preserves story 11 (per-entry republish gives modern-equivalent freshness)
while making nesting symmetric: an inner operation with a different root or
configuration no longer leaks its config to the surrounding operation.

Tests (`tests/test_bootstrap.py`, adapter-level, stub modules):

- Nested `runtime_context()` calls with different config loaders: after the
  inner block exits, the outer `server.daconfig` is visible again.
- Failure path: an exception inside the inner context still restores the
  outer config (mirrors the existing failed-operation restore test).

## 2. Resolve the PDF-only contract via a superseding ADR (S3)

The PR's behavior change was deliberate (commit cites story 15: PDF conversion
"explicitly unavailable"), and silently producing *nothing* for a PDF-only
attachment is arguably worse than an actionable error. But ADR-0001 is an
accepted ADR that explicitly rejected failing; code must not contradict it.

Fix: write `docs/adr/0006-pdf-only-attachments-fail.md` superseding ADR-0001's
PDF-only clause, and update ADR-0001 with a "Partially superseded by ADR-0006"
note. ADR-0006 records:

- Mixed `pdf` + `docx` attachments still drop generated PDF silently (ADR-0001
  unchanged): DOCX validation is never blocked.
- PDF-only attachments (nothing left after PDF removal) raise
  `PDFConversionUnavailable` at finalization — "explicitly unavailable"
  per issue #1 story 15 — instead of yielding an outcome with no usable
  artifact.
- Rationale: the silent-omission option was chosen when the only alternative
  was failing mixed-format interviews; with 1.9/1.10 parity and a typed
  failure vocabulary in place, an explicit, catchable error beats an empty
  artifact set.

No production code change; the existing test
(`test_pdf_only_attachment_raises_unavailable`) becomes the ADR-0006 evidence
and its docstring/comment cites the ADR.

## 3. Stop toggling install globals in tests (S1)

Two sites reset `runtime_module._BACKGROUND_INSTALLED` /
`_ATTACHMENT_FALLBACK_INSTALLED` to force a fresh install. Per ADR-0005,
tests should activate a runtime, not toggle module globals.

Fix in `tests/stub_runtime.py` (test-only helper, no production change):
add `reset_runtime_install_state(monkeypatch)` that resets the install flags
*as part of installing a stubbed runtime* — the helper already swaps
`sys.modules`, so resetting the flags there is "activating a fresh stub
runtime", not reaching into internals at each test. Then:

- `stub_legacy_without_background()` calls it instead of each test
  monkeypatching `_BACKGROUND_INSTALLED`.
- `test_pdf_only_attachment_raises_unavailable` (and the sibling fallback
  tests) use a fixture from `stub_runtime` that installs the stub parse
  module + fresh attachment state together.

If flags can be made derivable instead (e.g. the attachment fallback already
self-detects via the `_dasimulator_filename_fallback` marker), prefer dropping
the flag read in the test path entirely; keep the flag only as a process-level
idempotency cache.

## 4. Reframe private-seam assertions as observable behavior (S2)

ADR-0002: "Tests use the catalog, execution, render, and CLI interfaces;
narrowly focused persistence tests may exercise a private internal seam, but
private runtime seams are not public adapters." Issue #1 separately demands
adapter-level verification (idempotent installation, legacy bindings, context
restoration), which is impossible through the CLI without a real runtime.
Reconcile: adapter unit tests stay, but each must assert *observable runtime
state or behavior*, never private structure.

Audit all 43 tests in `tests/test_bootstrap.py`:

- Keep tests that go through module entry points (`register_hooks()`,
  `runtime_context()`, `install_attachment_filename_fallback()`) and assert
  effects on the stub modules (bindings present, thread state restored, config
  republished). These are behavioral.
- Rewrite structure assertions: e.g. `assert server.bg_action is
  _foreground_background_action` becomes "calling `server.bg_action(...)` in a
  stubbed interview context returns the foreground task value".
- Drop or relocate tests that only verify a private helper's internal wiring
  with no behavioral consequence; where the helper has a real contract
  (`_missing_or_raise`, `_legacy_functions_or_none` classification), keep it
  as a narrowly focused seam test per ADR-0002's persistence-test carve-out,
  with a comment citing the ADR.

## 5. Extract the shared background-seam patch (J1)

File: `src/docassemble_simulator/_runtime.py`.

`_patch_legacy_background_seam()` and the modern branch of
`_install_background_action_fallback()` both set
`functions.bg_action`, `functions.background_action`, `util.background_action`,
and `server.bg_action`. Extract one helper,
`_patch_background_dispatch(functions, util)` (or taking the modules to patch),
that applies the shared seams; the legacy branch additionally patches
`server.bg_action` (its distinguishing seam) and the modern branch additionally
patches `background.bg_action`. Behavior-preserving; covered by the existing
legacy/modern background tests.

## 6. Deduplicate the docassemble-module purge in tests (J2)

The "delete every `docassemble*` entry from `sys.modules`" loop appears in four
`tests/test_bootstrap.py` tests and `tests/test_cli.py:350`. Extract
`purge_docassemble_modules(monkeypatch)` into `tests/stub_runtime.py` (next to
the existing stub installers) and call it everywhere. Pure refactor.

## 7. Remove the unused `description` parameter (J3)

`tests/test_real_runtime.py`: drop `description=""` from `_query_family()` and
the pass-through at its call site in `_probe_interpreter()`. The parameter has
no effect on probing or diagnostics.

## 8. Strengthen the cross-family start/answer contract (P2)

File: `tests/test_real_runtime.py`,
`test_minimal_start_answer_contract_across_families`.

Today the test asserts `answer` returns `ok` and stops. Extend the shared
contract so both families prove the full spec sentence: compile → start →
question screen outcome → answer → **the next outcome or completion**
(story 3, testing decisions). Reuse the existing per-family screen/family
assert helpers; where the minimal fixture cannot complete, assert the concrete
next question screen rather than just `ok`.

Secondary: no automated gate runs the 1.9 lane. Out of diff scope, but record
it — either wire `mise test:all-da` into CI (no workflows exist yet) or note
the manual gate in README next to the 1.9 floor documentation (story 25).

## Order and validation

1. P1 (correctness) → 2. S3 ADR → 3–4. test hygiene (S1, S2) → 5–7. refactors
   (J1–J3) → 8. contract strengthening (P2).

Gates after each step: `mise run lint`; `mise run test` (expect 182+ passing);
cross-family lanes via `.venv-da19` / `.venv-da110` (`mise test:all-da`) for
steps 1 and 8, which touch runtime behavior or the shared contract.
