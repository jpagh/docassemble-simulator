# Architecture Refactor Review Follow-up Plan

Status: implemented
Date: 2026-08-26
Depends on: `docs/architecture-refactor-plan.md`, ADR-0001, and ADR-0002

## Goal

Close the architecture-review gaps without reopening the accepted direction of
the refactor. Keep interview execution and rendering as deep modules, make the
execution/render seam internal, preserve the documented CLI contract, and add
the missing durability and real-docassemble evidence.

This is a corrective stream, not another compatibility stream. Old command
names, state formats, and `Session` methods remain out of scope.

## Evidence gathered after review

The current fast suite passes (`70 passed`), so these are mostly contract and
coverage gaps rather than failures detected by existing tests. Focused probes
confirmed several failures that the suite misses:

- `docassemble-simulator --json answer` prints argparse text to stderr and exits
  `2`; it does not emit the JSON envelope or usage exit code `1`.
- An artifact targeted at the active saved-session path replaces the pickle;
  the next status/load reports unreadable saved state.
- A `PrepareRender.action` can return and retain the exact mutable working-state
  dictionary it receives.
- Two controlled writers using the fixed `<snapshot>.tmp` path produced a
  failed writer while that failed writer's payload became the destination. The
  fixed temp path therefore breaks both atomic outcome reporting and concurrent
  correctness.
- The tests contain no real calls to `currency`, `redact`, additional standard
  helpers, a declared-module function, or `DADateTime.format()` through the
  execution and render interfaces.

Exploration also found a related destination error: an artifact or snapshot can
currently target a file under `data/templates`, allowing render effects to
replace an authored source template. Snapshot and artifact targets can also be
the same path, so the later artifact write silently destroys the earlier
snapshot.

## Finding disposition

| Finding | Disposition | Assumption or error to address |
|---|---|---|
| Public render callback can leak working state | **Confirmed interface error** | Python cannot stop deliberately hostile in-process code from retaining an object it receives. The enforceable architecture rule is that no caller at an external seam receives the working state or supplies the callback. Make the callback a private seam used only by the render module. |
| Render effects can overwrite a saved session | **Confirmed error** | A hidden filename is not protection. Explicit paths must be checked against all saved-session storage, not only the selected interview's file. |
| Render returns ad-hoc dictionaries | **Confirmed interface error** | Dictionaries belong in CLI presentation only. The render interface needs typed success, result, and error values. |
| Tests use `execution._store` | **Partly confirmed; review was overbroad** | Feature tests should not create or inspect working state through `_store`. Focused tests of atomic persistence and schema failures may use a private internal seam; that does not justify a public storage port. Filesystem byte checks remain valid observations of the documented durable effect. |
| `RenderSource(kind, path)` and repeated kind switches | **Confirmed design smell** | The pair admits invalid states such as `saved` with a path and `snapshot` without one. Use source variants that carry exactly their required data. |
| Session and snapshot writers duplicate atomic-write logic | **Confirmed design smell** | Consolidate unique-temp creation, flush/fsync, replacement, locking, and cleanup in one internal filesystem implementation. Do not expose it as a storage interface. |
| Render orchestration remains in the CLI | **Partly rejected** | Mapping flags into a `RenderRequest` is explicitly a CLI adapter responsibility. Loading state, running fixtures, deciding semantic validity, assembly, snapshot timing, and rendering are not. Move mutually exclusive flag syntax into argparse and keep semantic validation in render; do not move argparse knowledge into render. |
| Argparse bypasses JSON and exit-code policy | **Confirmed error** | Parsing must produce a typed usage failure before bootstrap while preserving `--help` as a successful help action. |
| Concurrent snapshot writes share one temp path | **Confirmed error** | Unique temp files remove inode aliasing; a per-destination lock gives snapshot and artifact writes one explicit serialization policy. These locks must not be session transaction locks. |
| Render errors omit template identity | **Confirmed error with an attribution limit** | The requested top-level template is always known. Exact dynamic include identity may not survive docassemble's save/reload passes. Prefer upstream exception metadata when trustworthy; otherwise report the top-level template and render pass rather than inventing an included filename. |
| Real-docassemble fidelity tests are absent | **Confirmed evidence gap, not proof of a runtime defect** | Fake `populate_non_pickleable()` and plain `datetime` tests show call ordering only. They cannot establish server-equivalent utility restoration or date behavior. |

## Assumptions to make explicit

1. **Trusted process, guarded interface.** Simulator Python modules are trusted
   in-process code; this is not a sandbox against malicious imports or
   callbacks. Nevertheless, the external execution and render interfaces must
   make accidental working-state escape impossible.
2. **Trusted-local persistence, protected owned paths.** Pickles remain
   trusted-local. Explicit artifact and snapshot paths may replace ordinary
   user-selected files, but they may not alias module-owned saved sessions,
   authored templates, another effect in the same request, or an explicit
   render input. Path checks use resolved paths and protect against accidental
   aliases; defending against a hostile process swapping symlinks between check
   and replace is not in scope.
3. **Concurrent destination policy.** Writers to the same snapshot or artifact
   destination serialize. Each successful write installs one complete file;
   final-writer-wins is acceptable, but shared-temp failures and partial files
   are not. Session mutations retain their broader per-interview transaction
   lock.
4. **CLI adaptation is not lifecycle orchestration.** The CLI may parse flags,
   choose one typed render source, and construct a request. It may not load a
   saved session, execute a fixture, prepare a namespace, or decide effect
   timing.
5. **Best-available attribution is honest attribution.** Always identify the
   requested template. Report an included template only when docassemble or a
   private render trace supplies reliable identity.
6. **Two test layers are intentional.** Most behavior is tested through the
   external interfaces. A small internal persistence suite may inject write,
   flush, and replacement failures. This private test seam does not become an
   adapter or caller interface.
7. **Real-runtime tests are a separate lane.** `docassemble` remains absent from
   this project's dependencies. A target-package interpreter supplies the real
   runtime; the fast stub suite cannot satisfy real-runtime acceptance by
   itself.

## Selected interface and seam

Keep the two external interfaces:

```python
InterviewExecution.run(operation: ExecutionOperation) -> ExecutionOutcome
InterviewRenderer.render(request: RenderRequest) -> RenderOutcome
```

`PrepareRender` is removed from the exported `ExecutionOperation` union and from
`execution.__all__`. Rendering uses a package-private seam similar to:

```python
execution._with_render_state(preparation, render_action)
```

Only `InterviewRenderer` calls this seam. The render action receives working
state while docassemble context is active and returns a typed render result. The
working state is never placed in an external outcome. The private preparation
request also gives execution the effect destinations solely so the state store
can reject its owned session paths; render remains responsible for template and
input-path collisions. This is an internal seam, not a new public adapter and
not a runtime security mechanism.

ADR-0002 should be amended to clarify that callback-scoped render preparation
is internal to the execution and render implementations. The accepted choices
remain unchanged: callers cannot order lifecycle stages, and render remains a
separate module.

Replace the primitive source pair with variants:

```python
SavedSessionSource()
FreshSource()
SnapshotSource(path)
FixtureSource(path)
```

`RenderRequest` continues to hold orthogonal source, assembly, snapshot,
artifact, and expectation concerns. Fixture assembly remains semantically
invalid even if a non-CLI caller constructs it.

Add typed render values, for example:

```python
RenderResult(template, paragraphs, artifact=None)
RenderOutcome(ok, result=None, error=None)
RenderFailure(kind, message, details)
```

The exact names can follow the existing execution outcome vocabulary, but the
render interface must contain no untyped success/error dictionaries. The CLI is
the sole place that converts these values to the documented JSON envelope or
human output.

## Work plan

### 1. Pin the contracts with failing tests

Add focused failures before changing implementation:

- a public-interface test proving no exported execution operation accepts a
  caller callback or returns working state;
- render-interface tests asserting typed success and failure outcomes;
- source-variant construction tests proving invalid kind/path pairs are
  unrepresentable;
- subprocess CLI tests for missing arguments, unknown commands, conflicting
  render-source flags, `--json`, non-JSON usage, and `--help`;
- destination tests for saved-session paths, template directories, input/effect
  aliases, and snapshot/artifact collision;
- a deterministic concurrent snapshot regression matching the fixed-temp race;
- top-level and included-template error-attribution tests.

### 2. Internalize render preparation

- Remove public `PrepareRender` and its arbitrary `Callable[..., Any]` result.
- Introduce the private callback-scoped preparation method used only by
  `InterviewRenderer`.
- Keep source loading, rehydration, seed execution, fixture execution, optional
  assembly, and snapshot timing in execution.
- Keep template lookup, DOCX/Jinja evaluation, include passes, expectations, and
  artifact creation in render.
- Replace the current fake execution adapter in render contract tests with the
  real execution module over a temporary workspace. Do not publish an execution
  port solely to preserve that fake.

### 3. Type render requests and outcomes

- Introduce the four render-source variants and remove string-kind cascades from
  execution and render.
- Centralize render-owned request validation in `InterviewRenderer`: valid
  source paths, fixture non-assembly, distinct effects, and template/input
  protection. Let execution validate destinations against state-store paths at
  the private preparation seam.
- Return `RenderOutcome` and `RenderResult` throughout render.
- Translate execution preparation failures into typed render failures without a
  nested dictionary envelope.
- Teach CLI presentation to serialize typed values; do not add `to_json()` to
  domain outcomes because JSON remains a CLI concern.

### 4. Make destination effects safe and local

Create one private atomic-destination implementation used by saved sessions,
snapshots, and artifacts. It must:

1. create a unique sibling temp file;
2. acquire a lock derived from the destination for snapshot/artifact writes;
3. let the caller serialize pickle or save DOCX into the temp file;
4. flush and `fsync` the completed file;
5. atomically replace the destination;
6. clean the unique temp file after every failure.

The saved-session read-modify-write transaction keeps its existing
per-interview lock; the helper must not shorten that transaction to only the
file replacement.

Before either render effect occurs, validate resolved destinations. Execution
owns and applies the saved-session check; render owns the remaining checks:

- reject anything inside `.simulator/sessions/`;
- reject anything inside a package `data/templates/` tree;
- reject artifact and snapshot destinations that resolve to the same path;
- reject effects that alias the selected template, fixture, or snapshot input.

Ordinary explicit output paths remain replaceable. Cross-process tests must show
that concurrent same-destination snapshots and artifacts are always complete,
that both operations do not fail because of temp-name collision, and that no
session lock is acquired by an otherwise ephemeral render.

### 5. Bring usage failures under the CLI contract

- Use an `ArgumentParser` subclass whose `error()` raises a typed usage failure
  without first printing or exiting.
- Catch that failure around parsing, before workspace discovery or bootstrap.
- Detect the literal `--json` flag before any `--` terminator in the original
  argument vector so parse failures can still use the JSON envelope without
  mistaking positional text for a flag.
- Label a failure with the recognized subcommand; use `command: "cli"` when no
  command can be identified reliably.
- Return exit code `1` for every usage failure. Preserve `--help` as normal help
  with exit code `0` rather than wrapping it as an error.
- Use an argparse mutually exclusive group for `--fresh`, `--snapshot`, and
  `--fixture`. Keep the remaining conversion to typed source and request values
  in a small CLI adapter.
- Replace `_parse_assignments`'s `SystemExit` with the same typed input path.

### 6. Improve render attribution without fabricating precision

- Add template identity to the render error type and JSON details.
- Pass the resolved requested template through preparation and all render
  passes.
- Characterize the supported docassemble version to determine when Jinja or
  docassemble preserves a meaningful exception `filename` for included files.
- Prefer that filename when it maps to a real package template. Otherwise use
  the requested template, paragraph/line, error type, and render-pass number.
- Cover malformed included DOCX structure as well as Jinja failures, and assert
  that neither the top-level nor included source file changes.

### 7. Replace test leakage and add the real-runtime lane

Reorganize tests by seam:

- **Execution/render contract tests:** use temporary workspaces and only
  `run()`/`render()` for setup and behavior. Observe documented state effects by
  snapshotting session-file bytes from the temporary filesystem, not by calling
  `_store.save()` or `_store.load()`.
- **Internal persistence tests:** narrowly test schema validation, serialization,
  flush, replacement, lock contention, and cleanup. Keep this helper private.
- **Pure render tests:** retain focused DOCX/Jinja transformation tests where
  they describe render implementation invariants.
- **Real-runtime subprocess tests:** accept a target-package Python interpreter,
  run the simulator CLI in that interpreter, and rely on the CLI's early native
  library re-exec. This avoids adding docassemble as a project dependency.

Build a seed-free synthetic interview package for the real-runtime lane. It must
exercise:

- persisted-state rehydration of callable `currency`, `redact`, at least one
  additional date helper, number helper, and text helper;
- a function exported by an interview-declared `modules:` entry;
- saved, fresh, snapshot, fixture, and no-assemble render sources;
- a valid date answer becoming a real `DADateTime` and succeeding through
  `.format(...)` in evaluation and a DOCX template;
- invalid-date rollback, date-shaped text preservation, and `--code` bypass;
- no mandatory assembly solely to restore helper names.

The lane may be skipped by the default dependency-light suite when no target
interpreter is configured, but the repository must provide one explicit command
that runs it and fails clearly when the supplied runtime is unusable. Running
that command plus the real `docassemble-automatedpleading` flow/render/include
smoke test is a merge/release gate for this stream.

### 8. Documentation and cleanup

- Amend ADR-0002 with the private render-seam clarification.
- Update README usage-error behavior, destination protections, concurrency
  policy, and real-runtime test command.
- Remove obsolete dictionary-based render tests and direct feature-test use of
  `_store` once interface coverage replaces them.
- Run `git diff --check`, the fast suite, the real-runtime lane, and the manual
  package parity checks.

## Implementation order

1. Add the failing interface, parser, destination, race, attribution, and
   real-runtime regression tests.
2. Amend the ADR and internalize callback-scoped render preparation.
3. Introduce source variants and typed render outcomes; simplify the CLI render
   adapter.
4. Introduce the shared private atomic-destination implementation and protected
   path policy.
5. Route argparse failures through the unified envelope and exit mapping.
6. Add best-available template attribution.
7. Replace `_store`-driven feature setup, complete the real-runtime matrix, and
   delete superseded tests.
8. Update documentation and run all automated and manual gates.

## Acceptance criteria

1. No exported execution operation accepts a namespace callback, and no external
   execution or render outcome can contain working state.
2. `InterviewRenderer.render()` returns typed outcomes for input, state,
   execution, render, and unexpected failures; ad-hoc dictionaries begin only
   in CLI presentation.
3. Invalid render source kind/path combinations are unrepresentable, and source
   semantics are validated in render/execution rather than command handlers.
4. JSON usage failures emit the documented envelope and exit `1`; non-JSON usage
   also exits `1`; help exits `0`.
5. Render effects cannot replace any saved session, authored template, explicit
   render input, or one another within a request.
6. Saved sessions, snapshots, and artifacts share one tested atomic-replacement
   implementation with unique temp files and complete cleanup.
7. Concurrent writes to one snapshot or artifact destination yield a complete
   final file without shared-temp failures. Session transaction locking remains
   unchanged.
8. Every render failure reports at least the requested template, error type, and
   available paragraph/line; exact included identity is reported only when
   known.
9. Feature contract tests do not arrange working state through
   `InterviewExecution._store`; no public storage or runtime adapter is added
   solely for tests.
10. The seed-free real-runtime regression calls standard and declared helpers
    through every required render source and proves real `DADateTime.format()`
    behavior.
11. The fast suite, real-runtime lane, real-package smoke checks, ADR-0001 PDF
    policy, and `git diff --check` all pass.

## Non-goals

- No migration for old sessions, snapshots, commands, or JSON shapes.
- No public storage port, filesystem adapter, or docassemble runtime port solely
  for tests.
- No hostile same-process callback sandbox or adversarial filesystem race
  defense.
- No promise of exact included-template identity when upstream runtime metadata
  does not preserve it.
- No screen-description redesign, runtime-adapter extraction, Celery support,
  generated PDF conversion, or source-template repair.
- No addition of docassemble to this project's normal dependency set.
