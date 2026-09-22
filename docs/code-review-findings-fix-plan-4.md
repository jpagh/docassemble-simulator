# Code-review findings fix plan 4 (PR #2)

Status: implemented — historical record; the findings were fixed before PR #2
merged.

Review scope: `main...HEAD` on `support-da19x` (12 commits), two-axis review
against issue #1 (spec) and `CONTEXT.md` + `docs/adr/0001`–`0006` (standards).

No hard documented-standard breaches were found this round; all Standards
findings are baseline-smell judgement calls. The one Spec finding with code
impact is a deliberate, commented deviation from the issue's letter that needs
an ADR record, not a behavior change.

Excluded by decision:

- Scope creep of committed `docs/code-review-findings-fix-plan*.md` artifacts:
  kept — the repo already commits these plans (this file continues the series).
- The `da19`/`da110` dependency groups, uv `conflicts`, and mise tasks: kept —
  entailed by issue #1 stories 23/25.

## Findings being fixed

Standards (judgement calls, smell baseline):

- S1: `_missing_or_raise`'s `or names[-1]` fallback is unreachable
  (`src/docassemble_simulator/_runtime.py:99`) — misleading robustness.
- S2: `_missing_or_raise` used for its raise side effect with the return value
  discarded in `_patch_legacy_background_seam` and `_legacy_functions_or_none`
  (`_runtime.py:1180-1197`) — command–query confusion.
- S3: `runtime_context` imports `docassemble.base.thread_context` twice
  (from-import, then `importlib.import_module` in the handler) to distinguish
  absence from a broken dependency (`_runtime.py:197-216`) — convoluted shape.
- S4: the `_question`/`_package` option-style normalization is duplicated
  between `_SimulatorRuntimeBindings.url_finder` and the legacy
  `server.file_finder` wrapper (`_runtime.py:660-690`, `:940-960`).
- S5: the legacy `server.file_number_finder` wrapper accepts `**kwargs` and
  silently drops them (`_runtime.py:962-975`) — signature drift.
- S6: `_register_legacy_runtime_bindings` sets each `default_*` snapshot
  attribute one call at a time (`_runtime.py:913-924`), and the freshness
  asymmetry (defaults snapshotted at install, `daconfig` refreshed per
  operation entry) is undocumented (`_runtime.py:171-193`).
- S7: `blocked_docassemble_imports` deletes `sys.modules` entries directly with
  no restore (`tests/stub_runtime.py:186-192`), while the adjacent
  `purge_docassemble_modules` uses `monkeypatch` — inconsistent test hygiene.

Spec (issue #1):

- P1 (worst): the real 1.9.x lane has not been run yet in this environment;
  the PR must not merge before `mise run test:all-da` passes (issue #1 testing
  decisions; README names it the manual regression gate).
- P2: `_LegacyContextAdapter` keeps the freshly published `daconfig` when the
  first activation had no prior value — a deliberate deviation from the
  spec's "restore prior state in a `finally` path" letter that is only
  recorded in a code comment (`_runtime.py:189-192`). It needs an ADR.

## 1. Simplify `_missing_or_raise` and its callers (S1, S2)

File: `src/docassemble_simulator/_runtime.py`.

- Delete the dead fallback: `return getattr(error, "name", None)` is
  sufficient, since the function only proceeds when `_is_missing_module`
  returned true (so `error.name` is one of `names`). Update the docstring to
  state the invariant instead of implying a fallback.
- Rename the side-effect-only call sites to read honestly:
  `_patch_legacy_background_seam` and `_legacy_functions_or_none` only need
  the raise; replace `_missing_or_raise(error, ...)` + `return` with
  `if not _is_missing_module(error, ...): raise error` and drop the
  unreachable trailing `return`. Keep `_missing_or_raise` for the
  value-consuming sites (`get_configuration`, `_install_relationship_methods`,
  `_register_pluggy_runtime_bindings`, `_install_background_action_fallback`).

No behavior change; existing broken-dep propagation tests
(`TestIncompleteRuntime`, `test_legacy_functions_or_none_broken_dep_propagates`)
must still pass unchanged.

## 2. Single import in `runtime_context` (S3)

File: `src/docassemble_simulator/_runtime.py`, `runtime_context`.

Replace the try/from-import → except ImportError → re-import shape with one
`importlib.import_module("docassemble.base.thread_context")` guarded by
`except ModuleNotFoundError as missing:` → `_missing_or_raise(missing,
"docassemble.base.thread_context")` → legacy path (calling
`_ensure_runtime_present()` first, as today, so a missing runtime reports
"No docassemble runtime" rather than an incomplete one). Behavior-preserving:
both shapes only treat the module-absence case as legacy; broken dependencies
propagate either way.

Covered by existing tests: `test_runtime_context_reports_missing_runtime`,
`TestLegacyContext`, cross-family compile tests. Add one test that a broken
dependency *inside* `thread_context` (stub raising `ImportError` with a
foreign `name`) propagates unconverted, mirroring
`test_configuration_broken_dep_propagates`.

## 3. One option-style normalizer (S4, S5)

File: `src/docassemble_simulator/_runtime.py`.

- Extract `_legacy_lookup_options(source)` (or fold into `bindings.url_finder`
  and `bindings.file_finder`): a single helper that maps
  `question`/`_question` and `package`/`_package` to canonical names and pops
  them from a kwargs dict. Use it in both `_SimulatorRuntimeBindings.url_finder`
  and the legacy `server.file_finder` wrapper.
- Remove `**kwargs` from the legacy `file_number_finder` wrapper: it forwards
  nothing, so the parameter only hides signature drift. Callers that pass
  unexpected kwargs to 1.9's `file_number_finder` are outside the tested
  contract; if one appears, extend the wrapper explicitly.

Tests: `test_legacy_runtime_normalizes_server_keywords_and_defaults` and
`test_legacy_url_finder_normalizes_option_styles` already pin the option
styles; assert the canonical file_finder path with `_question`/`_package`
kwargs in the same test.

## 4. Snapshot defaults as a table; document the freshness rule (S6)

File: `src/docassemble_simulator/_runtime.py`.

- Replace the six individual `server.default_* = bindings.get_default_*()`
  assignments with a loop over one name→getter mapping (e.g. a module-level
  `_LEGACY_DEFAULT_FIELDS = {"default_voice": "get_default_voice", ...}`) so
  modern/legacy parity is auditable in one place. Same for the
  `get_default_*` attribute installs.
- Add a short comment (or extend the existing one) stating the freshness
  contract: legacy `default_*` values are install-time snapshots and are
  constants in the simulator, so the snapshot cannot drift; `daconfig` is the
  only per-operation-refreshed server value because it is workspace-dependent.
  This closes the asymmetry flagged in review without changing behavior.

No test change expected; `test_legacy_runtime_normalizes_server_keywords_and_defaults`
pins the snapshot values.

## 5. Monkeypatch-clean import blocker (S7)

File: `tests/stub_runtime.py`, `blocked_docassemble_imports`.

Route the module purge through the caller's `monkeypatch` (accept it as a
parameter and reuse `purge_docassemble_modules(monkeypatch)`), keeping the
meta-path blocker as the context-managed part. Update the two call sites
(`tests/test_cli.py` missing-runtime test, `tests/test_bootstrap.py` if any)
to pass `monkeypatch` in. No assertion changes.

## 6. ADR for the first-activation daconfig rule (P2)

New file: `docs/adr/0007-legacy-daconfig-restore-semantics.md` (status:
accepted). Record:

- Decision: on legacy context exit, a prior `server.daconfig` is restored
  verbatim; a first activation with no prior value keeps the freshly published
  config, matching install-time binding. Rationale: with no prior value there
  is nothing to leak outward, and removing the published config would regress
  the install-time binding that install/refresh flows rely on (story 11's
  freshness assertions apply within an operation, not across the first one).
- Consequences: nested operations with different roots/configurations never
  leak outward (story 8); the deviation from the issue's "restore prior state"
  letter is intentional and tested by
  `test_context_entry_refreshes_server_config` /
  `test_nested_contexts_restore_outer_server_config`.

Cross-reference from `_LegacyContextAdapter.activate`'s existing comment.

## 7. Run the real 1.9.x lane (P1)

- Provision the 1.9 family: `mise run sync:da19`.
- Run the full cross-family gate: `mise run test:all-da` (real 1.9 + 1.10
  lanes, no skips). This must pass before merge; if it exposes 1.9-specific
  failures, fix them before this plan is complete.
- Fast gates after the code changes: `mise run lint`, `mise run test`.

## Order of work

1 (S1/S2) → 2 (S3) → 3 (S4/S5) → 4 (S6) are independent runtime-module edits;
do them as one focused commit each or one batch commit, fast suite green
between steps. 5 (S7) is test-only and can land any time. 6 (ADR) is docs-only.
7 runs last, on the final state of the branch.
