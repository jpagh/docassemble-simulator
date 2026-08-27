# Architecture Refactor Plan

Status: implemented; interface decision accepted in ADR-0002
Date: 2026-08-26

## Goal

Build the simulator around deep interview-execution and render modules: small
interfaces that hide docassemble lifecycle, thread-context, state, validation,
and rendering complexity. Optimize for a coherent greenfield program rather
than compatibility with the current Python or CLI interfaces.

Preserve docassemble fidelity where it is intentional. Accidental behavior,
fragmented Python methods, inconsistent command side effects, existing JSON
shapes, the current session pickle layout, and current command names are not
compatibility constraints.

## Product stance

- The documented CLI and its JSON output are the supported external interface.
  Python modules remain internal until an interface is deliberately documented.
- The existing `Session` class is not retained as a compatibility adapter. Its
  fragmented lifecycle methods are replaced by cohesive execution operations,
  then the obsolete class and paths are deleted.
- Saved sessions and snapshots may use a new, versioned format. No migration is
  required; stale state should fail with a concise instruction to run `start`
  again.
- CLI commands, flags, output shapes, and exit codes may change where doing so
  produces clearer and safer semantics. README and help text change in the same
  stream.
- Preserve interview assembly, seeking, screen semantics, answer coercion,
  validation order, authored seed execution, docassemble context behavior,
  template evaluation, include passes, and intentional PDF policy.
- Preserve ADR-0001: generated PDF conversion remains stubbed and no external
  converter is invoked.

## Live-package feedback disposition

The latest synthetic family-category run against
`docassemble-automatedpleading` left production compilation, math probes, and
pytest green. It produced four inputs for this plan:

- **CLI date values:** `YYYY-MM-DD` answers to `date` fields remained strings,
  so downstream template calls such as `.format(...)` failed. This is an
  outstanding simulator fidelity defect and is in scope for this refactor.
- **Runtime helpers:** the package seed now imports the standard docassemble
  `currency()` and `redact()` helpers, but that is a workaround rather than the
  correct ownership. `custom_jinja_env()` exposes these names as filters, while
  function-call syntax resolves them from the render namespace. Session
  persistence drops some callables, and fixture/no-assemble rendering did not
  repopulate the namespace. The current stream must perform docassemble's
  server-equivalent non-pickleable population for every prepared namespace; do
  not maintain a helper-by-helper seed list.
- **Background work:** local simulation now supplies an immediate-completion
  foreground `background_action()` task by default because it has no Celery
  worker. This is a simulator-owned convenience, not worker parity: queue
  latency, isolation, retries, and worker failures remain deployment concerns.
  `background_actions = "disabled"` retains the waiting/stub behavior.
- **Embedded DOCX structure:** shared templates that lacked a leading paragraph
  were fixed at their source after docassemble raised `IndexError`. Rendering
  should continue to report structural template failures, not silently rewrite
  source or included DOCX files.

The remaining simulator changes from these findings are field-aware date
coercion and general namespace rehydration. The background and template fixes
establish seed-adapter and template ownership constraints that the refactor must
preserve.

## Architecture decisions

### One operation owns an execution lifecycle

A caller requests a complete operation such as start, answer, seek, evaluate,
execute code, refresh, or prepare render state. The execution module owns all
required stages and their ordering:

1. resolve and load the interview definition
2. acquire any required session lock
3. create or load an operation-local namespace
4. enter docassemble thread context
5. restore standard docassemble utilities and interview-declared imports/modules,
   then run authored simulator seed code
6. apply the requested mutation or inspection, using active-field metadata for
   answer coercion
7. validate and mark an answered question when applicable
8. assemble or seek when requested
9. construct a typed outcome
10. atomically commit session state when the operation's policy requires it

Callers never coordinate `load -> apply -> validate -> assemble -> save`
themselves and never receive mutable session state, interview objects,
`InterviewStatus`, or thread-context helpers.

### Operation-local state, not a second in-memory authority

The execution object may own immutable configuration such as workspace root,
interview identity, and collaborators, but it does not cache a mutable session
namespace across operations. Each operation loads a new working namespace from
the state store or creates a fresh one. This makes rejected operations naturally
discardable and keeps the versioned state file as the single durable authority.

Mutating operations use a per-interview advisory lock across the complete
read-modify-write transition so two CLI processes cannot silently overwrite one
another. Commits use a sibling temporary file and `os.replace()`.

### Namespace rehydration is generic, not a helper allowlist

Persisted state deliberately omits callables that cannot round-trip through
pickle. Fresh fixture namespaces also begin without the normal imported utility
surface. Before seed, evaluation, assembly, or rendering, execution restores the
same non-pickleable names that docassemble restores on a server request:

- call the compiled interview's `populate_non_pickleable()` mechanism, or a
  version-adapted equivalent hidden inside execution, while docassemble context
  is active
- restore exports from `docassemble.base.util` plus every declarative
  `modules:` and `imports:` entry in the compiled interview
- apply this to saved sessions, snapshots, fresh state, fixture state, and
  no-assemble render state without advancing mandatory flow
- run the authored seed only after this standard namespace exists

This covers `currency`, `redact`, date/number/text helpers, and package module
functions as a class; the simulator must not enumerate individual helpers or
copy them into the execution interface. It does not make every symbol installed
under `docassemble` implicit. A function outside the standard utility exports
must still be declared by the interview, while a function that requires an
external server dependency may still need a runtime or package adapter for its
behavior.

### Answer coercion follows active field semantics

`answer` accepts browser-shaped values, not untyped Python assignments. The
execution module performs coercion while the active question's live field
metadata and docassemble context are available; the CLI adapter only supplies
raw assignments.

- Preserve field-aware checkbox and object-choice coercion alongside scalar
  JSON/literal parsing.
- For a non-code answer to a field whose effective datatype is `date`, convert
  an exact `YYYY-MM-DD` string through docassemble's own date conversion to the
  server-equivalent `DADateTime` value before validation and marking.
- Reject impossible calendar dates and unsupported non-empty date shapes as a
  typed answer-input failure. A failed field in a multi-field answer discards
  every assignment and leaves the saved session unchanged.
- Do not coerce by string shape alone: the same value assigned to a text field
  remains a string. Explicit code assignments and `exec` retain Python
  semantics and bypass browser-value coercion.

Before implementation, characterize docassemble's real form-submission behavior
to pin timezone, midnight, and empty optional-date semantics. Reuse the
server-side converter rather than hand-rolling date parsing.

### Transaction scope

The transaction covers the interview namespace, simulator session metadata, and
session-state file. Assignment failure, validation rejection, code-execution
failure, and a failed pre-replacement write leave the previous session intact.
A docassemble exception deliberately represented as a flow error outcome may be
committed where the command matrix below says so; that preserves useful
reproducible debugging state.

The transaction cannot roll back arbitrary external effects caused by authored
Python, `exec`, docassemble globals, attachment hooks, or filesystem/network
calls. This limitation is part of the execution interface. Artifact and snapshot
writes are separate explicit effects with the timing defined below.

### Static interview inspection is not session execution

`check`, `questions`, and `index` compile and inspect interview definitions but
do not need session transaction machinery. A focused interview-catalog module
owns definition discovery, compilation, block metadata, and variable indexing.
The execution module uses the same internal definition loader without exposing
compiled docassemble objects to the CLI.

This keeps the session-oriented execution interface small instead of adding
unrelated metadata methods merely because both implementations load YAML.

### Rendering and execution remain distinct modules

The render module owns render-mode semantics, template resolution, DOCX/Jinja
preparation, include passes, missing-variable expectations, artifact writing,
and render outcomes.

The execution module owns preparing a namespace and keeping docassemble context
active while rendering consumes it. Their internal seam must not expose
`Session._in_interview` or require render to reproduce load/config/assemble
steps. Interface design must explicitly solve this context handoff—for example,
execution can invoke a narrow render action while the prepared context is active.
The raw context must not cross into CLI code or persistence code.

### Bootstrap remains at the composition root for now

The application entrypoint performs environment preparation, then conditionally
runs `ensure_importable()` and `bootstrap()` once for commands that require the
docassemble runtime. `info` must remain usable without runtime bootstrap and
must continue to report that docassemble is unavailable rather than failing.

Individual command handlers do not invoke bootstrap. Bootstrap policy remains
in `bootstrap.py`; extracting the simulator runtime adapter is a later stream.

### Authored seed scripts remain package runtime adapters

Execution runs `.config/simulator/config.py` after generic namespace
rehydration and before authored flow, evaluation, or render work on every
applicable operation. The seed provides data and deterministic substitutes for
package/server dependencies, as the live package does for `background_action`.
Its current `currency` and `redact` imports become unnecessary once generic
rehydration lands; standard docassemble helpers must not be a per-package seed
responsibility. Seeded names remain namespace implementation details and do not
become methods on the execution interface.

The simulator does not start Celery or pretend to execute arbitrary background
work. A package that needs no-worker behavior owns that policy in its seed until
two real packages justify a narrower shared seam. Render preparation must see
the same rehydrated and seeded namespace as flow execution.

## Recommended module topology

### Interview catalog module

Owns:

- package/interview discovery and selector resolution
- interview compilation and user-facing compile errors
- question/block listings
- variable-to-question indexing
- read-only metadata outcomes for `check`, `questions`, and `index`

It does not create session namespaces or enter the execution lifecycle.

### Interview execution module

Owns:

- fresh namespace construction
- versioned session and snapshot loading
- per-interview session identity and locking
- operation-local working state
- server-equivalent utility and declarative module/import rehydration
- authored seed execution for package/server dependencies
- docassemble thread context and global-root registration
- assemble, seek, and active-seek behavior
- active-field-aware answer parsing, date/checkbox/object coercion, application,
  validation, and marking
- expression evaluation and code execution
- transaction policy and atomic session persistence
- preparation of live render state
- typed execution outcomes and errors

Session storage is local-substitutable filesystem implementation detail. Do not
add a public storage port solely for tests; tests use temporary workspaces.

### Render module

Owns:

- render request validation
- template lookup under `data/templates`
- render-state source and assembly-policy semantics
- DOCX preparation and docassemble Jinja evaluation
- included-document render passes and context reset
- missing-variable expectations
- paragraph/error attribution
- atomic artifact writing
- render outcomes and errors

It must not load or save simulator session files, execute seed/fixture code
outside execution, or enter private docassemble thread context itself.

### CLI adapters

Own only:

- argument parsing and help
- workspace/interview selector syntax
- construction of catalog, execution, and render requests
- human presentation
- JSON presentation
- exit-code mapping

Domain resource layout such as `.simulator` state paths and
`data/templates` lookup stays hidden in the module that owns it.

## Interface design checkpoint

Do not implement the refactor around the first interface proposed. Before code
movement, design at least three materially different interfaces:

1. one `run(operation) -> outcome` entry point using typed operation variants
2. a session-oriented runner with a small set of cohesive methods
3. command-focused use-case functions optimized for the common agent loop

For each design, demonstrate `start`, `answer`, read-only evaluation, active
seek, code execution, and render preparation. Include invariants, ordering,
error modes, persistence effects, and the execution/render context handoff.
Compare the designs by depth, locality, seam placement, and test surface, then
record the selected design in an ADR before implementation.

The selected external execution interface must satisfy these constraints:

- no raw `user_dict`, compiled interview, status, or context manager escapes
- no caller-controlled lifecycle ordering
- persistence behavior follows the command matrix
- read-only operations cannot accidentally commit
- render can evaluate while the required docassemble context is active
- typed outcomes contain enough information for both human and JSON adapters
- context/runtime test seams remain private unless two real adapters justify a
  public seam

## Proposed CLI contract

Names remain subject to the interface-design checkpoint, but this is the
baseline contract to improve on:

- Rename `set` to `answer`; it answers the current saved screen transactionally.
- Rename `get` to `eval`; it evaluates an expression without saving.
- Remove `start --set`, whose lifecycle differs from normal answer validation.
- Make `status` a pure read of the saved outcome. Add explicit `refresh` for a
  mutating reassembly of saved state.
- Make `seek` use the saved session by default, with `--fresh` for isolated
  diagnosis and `--activate` to persist the sought screen as the active screen.
- Make `exec` assemble and report the resulting screen by default; use
  `--no-assemble` for a state-only escape hatch.
- Remove `status --vars`; `vars` remains the focused inspection command.
- Remove the implicit `.config/simulator/fixture.py` render mode. Fixture use is
  explicit with `render --fixture PATH`.
- Replace render's `--no-flow` with the precise `--no-assemble` name.
- Replace render's directory-shaped `--output DIR` with `--output PATH` for the
  exact artifact target.
- Keep `--json`, but replace command-specific ad hoc dictionaries with one
  documented envelope:

  ```json
  {"ok": true, "command": "answer", "result": {}}
  ```

  ```json
  {
    "ok": false,
    "command": "answer",
    "error": {"kind": "validation", "message": "...", "details": {}}
  }
  ```

Recommended exit codes:

- `0`: successful command, including a finished interview
- `1`: usage, workspace, configuration, or missing-state precondition failure
- `2`: validation, interview execution, seek, or render failure
- `3`: unexpected simulator fault

## Normative command and persistence matrix

A "flow error outcome" is a structured result produced after docassemble raises
while assembling; it is distinct from failure to load/configure the runtime or
from a simulator fault.

| Operation | Working-state source | Runtime work | Durable session effect |
|---|---|---|---|
| `info` | none | none; no bootstrap | none |
| `check` | compiled definition(s) | compile only | none |
| `questions` / `index` | compiled definition | inspect only | none |
| `start` | fresh namespace | rehydrate, seed, assemble | atomically replace selected interview's session after a screen, finished outcome, or flow error outcome exists; prior session survives earlier failure |
| `status` | saved outcome | none | none |
| `refresh` | saved namespace | rehydrate, seed, reassemble or re-seek an active target | atomically replace on screen, finished, or flow error outcome |
| `answer` | saved namespace and active screen | apply all answers, validate, mark, assemble | replace on accepted answer and resulting outcome; unchanged on assignment or validation rejection |
| `seek` | saved namespace by default; fresh with `--fresh` | rehydrate, seed, mandatory setup, seek | none unless `--activate`; activation atomically saves the seek state and pin |
| `eval` | saved namespace | rehydrate, seed, evaluate | none |
| `vars` | saved namespace | rehydrate, seed, inspect | none |
| `exec` | saved namespace | rehydrate, seed, execute, assemble unless `--no-assemble` | replace only when code execution succeeds; an ensuing flow error outcome is committed |
| `render` | explicit render source | prepare state and render | never writes or deletes a session; snapshot/artifact effects follow render policy below |

Sessions are stored per interview rather than sharing one root-level
`session.pkl`. The state store derives a safe stable filename from the canonical
interview identity and hides that mapping from callers. Session and snapshot
payloads include a schema version and interview identity.

## Render preparation model

Do not model all render flags as one mode string. Render constructs a request
with orthogonal concerns:

```text
RenderRequest
  template
  source: SavedSession | Fresh | Snapshot(path) | Fixture(path)
  assemble: bool
  save_snapshot: path | none
  output: path | none
  expect_missing: variable | none
```

Rules:

- Exactly one source is selected; saved session is the default.
- Saved-session, fresh, and snapshot sources assemble by default.
  `--no-assemble` disables flow assembly but still performs the minimum safe
  namespace rehydration needed by real template helpers.
- Fixture source starts from a fresh namespace, restores standard and declared
  non-pickleable names, runs authored seed setup and the explicit fixture in
  execution context, and does not assemble.
- `render --fresh` is ephemeral and never deletes or replaces a saved session.
- A snapshot source validates schema and interview identity before use.
- `--save-snapshot` writes atomically after successful state preparation and
  before template evaluation. The snapshot therefore remains available when
  template rendering fails; preparation failure produces no snapshot.
- Artifact output is written atomically only after successful rendering and
  successful missing-variable expectations.
- Render and include passes remain inside the live docassemble context prepared
  by execution.
- Render never inserts paragraphs or otherwise repairs the source or included
  DOCX files. Structural failures become typed render errors with the best
  available template and paragraph attribution.

A characterization spike must pin `Interview.populate_non_pickleable()` (or the
supported-version equivalent) as the minimum safe rehydration mechanism: it must
restore standard utilities and declared modules/imports for no-assemble
rendering without advancing mandatory flow. Any docassemble-version adaptation
stays private to execution.

## State and concurrency details

- Pickle remains acceptable for local docassemble object graphs, but payloads
  are explicitly trusted-local files and are schema-versioned.
- The state store validates payload type, version, and interview identity and
  converts failures to typed state errors with recovery guidance.
- Mutating operations hold a per-interview lock from load through atomic replace.
- Temporary files are cleaned after failure. Tests inject serialization, flush,
  and replacement failures.
- Snapshot and artifact locks are not session locks; each uses atomic replacement
  at its own destination.
- The implementation must not rely on shallow-copy isolation of DA objects.
  Reloading operation-local state is the default strategy. Any alternative must
  prove object identity, pickling, and thread-context fidelity in a spike.

## Testing strategy

### Characterization before deletion

Characterize fidelity behavior that must survive, not every accidental CLI
shape:

- assemble, seek, active-seek, finished, continue, and flow-error outcomes
- answer parsing; date, checkbox, and object coercion; validation order; strict
  required checks; marking; and rejection rollback
- date-field `YYYY-MM-DD` conversion to `DADateTime`, including downstream
  `.format(...)`, invalid-date rejection, text-field non-coercion, and explicit
  code-assignment bypass
- generic namespace rehydration after callable dropping, including callable
  `currency`, `redact`, date/number/text helpers, and a function from an
  interview-declared package module
- authored seed timing, deterministic dependency substitutions, and global-root
  registration after standard rehydration
- screen descriptions consumed by answer validation
- snapshot round trips and callable dropping/rehydration across saved, fresh,
  fixture, snapshot, and no-assemble sources
- DOCX preparation, smart-quote fixing, paragraph attribution, strict undefined
  behavior, include passes, artifacts, and structural-error reporting without
  source-template repair
- ADR-0001 PDF omission and proof that no converter runs

Existing tests may serve as an oracle during extraction, but accidental method
boundaries and old CLI dictionaries do not become compatibility requirements.

### Interface and CLI contract tests

Test through the selected catalog, execution, and render interfaces. Add
parameterized coverage for every row in the command matrix and for the complete
render request matrix. Assert:

- typed outcomes and unified JSON envelopes
- ISO date answers become docassemble date values before validation, malformed
  dates roll back the whole answer, and date-shaped text answers remain strings
- human presentation for representative outcomes
- exit-code mapping
- session creation, replacement, preservation, and per-interview isolation
- read-only operations leave session bytes unchanged
- assignment, validation, code, serialization, and replacement rollback
- active-seek creation, status, answer, and refresh behavior
- bootstrap runs once when required and not at all for `info`
- every runtime-backed operation and render source restores standard utilities
  and interview-declared modules before seed or authored work
- snapshots persist after render failure but not preparation failure
- render never mutates or deletes session state
- lock contention cannot produce a lost update

Use temporary workspaces for local filesystem state. Keep real docassemble
behavior covered by integration tests. Focused fakes may satisfy private internal
seams when both real and test adapters are useful, but do not expose a runtime
port solely for tests.

The interface is the test surface. Delete tests for obsolete `Session` helper
methods after equivalent interface coverage exists; do not retain both old and
new lifecycle test suites.

## Implementation sequence

1. Add the fidelity characterization tests and command-side-effect probes listed
   above, including a failing date-field answer-to-template-formatting probe.
2. Complete the interface-design checkpoint and record the selected catalog,
   execution, and execution/render interfaces in an ADR.
3. Finalize the CLI names, JSON envelope, exit codes, command matrix, and render
   request rules in README/help contract tests.
4. Introduce the versioned per-interview state store, operation-local loading,
   locking, atomic replacement, and failure-injection tests.
5. Implement the interview catalog interface and route `check`, `questions`, and
   `index` through it.
6. Implement the shared namespace-preparation path using docassemble's
   non-pickleable population mechanism, then execution operations for `start`,
   `status`, `refresh`, `answer`, `seek`, `eval`, `vars`, and `exec`; implement
   active-field-aware date coercion inside `answer` and route CLI adapters
   through the operations one use case at a time.
7. Implement render-state preparation at the execution seam and verify generic
   rehydration for saved, snapshot, fresh, fixture, and no-assemble sources.
8. Move render request validation and all render orchestration into the render
   module. Remove CLI state selection, fixture execution, private-context access,
   and session-file access.
9. Centralize conditional runtime bootstrap in the application composition root.
10. Delete the obsolete `Session` class, duplicated lifecycle branches, old CLI
    commands/flags, and superseded implementation tests. Do not leave a
    compatibility layer.
11. Update README and design documentation, run the full suite, and perform the
    manual docassemble flow/render/include parity checks.
12. Verify the acceptance criteria and mark the interface ADR accepted.

## Later streams

### Simulator runtime adapter

Deepen `bootstrap.py` around environment setup, FakeRedis, pluggy hooks, session
no-ops, local file storage, attachment handling, and PDF policy. Standard
namespace population remains owned by execution; this later stream handles the
runtime behavior needed by helpers that depend on server infrastructure. Keep
package-specific background-task substitutes in authored seeds and do not
launch Celery. Keep the execution interface independent of bootstrap
implementation details.

### Screen-description module

Concentrate conversion of docassemble status/result objects into screen
descriptions, including field visibility, requiredness, choices, dynamic
context, and screen kinds. Keep CLI human rendering separate from screen
semantics.

## Non-goals

- No migration support for old CLI commands, JSON shapes, `Session` Python
  callers, root-level `session.pkl`, or unversioned snapshots.
- No recipe/recording workflow or other unrelated CLI capability in this stream.
- No runtime-adapter extraction in this stream.
- No screen-semantics redesign in this stream.
- No generated PDF conversion or external converter invocation.
- No Celery worker or claim that arbitrary background jobs execute locally;
  package seeds may install deterministic substitutes.
- No mutation or automatic repair of authored DOCX template structure.
- No hand-maintained allowlist of docassemble helper names and no implicit import
  of every installed docassemble symbol; mirror standard utility exports and
  interview declarations.
- No promise to roll back arbitrary side effects from authored or executed
  Python.
- No public adapter introduced solely to make tests easier.

## Acceptance criteria

1. CLI handlers contain no docassemble lifecycle, validation, persistence, or
   render-mode orchestration.
2. No code outside execution reaches private thread context; render consumes
   prepared state only through the selected internal seam.
3. Every operation obeys the normative persistence matrix, including rollback,
   per-interview isolation, and lock behavior.
4. `status`, `eval`, `vars`, and render leave session bytes unchanged; `render
   --fresh` never deletes a session.
5. Render source, assembly, snapshot, and artifact concerns are orthogonal and
   covered as a request matrix.
6. Runtime bootstrap occurs once for commands that require it and is skipped by
   `info`.
7. The old `Session` lifecycle and duplicated command branches are deleted rather
   than wrapped.
8. The new module interfaces and CLI/JSON contracts have complete tests; obsolete
   implementation tests are removed.
9. Full automated tests and manual real-package flow, render, snapshot, include,
   and artifact parity checks pass.
10. ADR-0001 remains true: DOCX output works, generated PDFs are omitted, and no
    external converter runs.
11. Answering an active `date` field with a valid `YYYY-MM-DD` value stores a
    server-equivalent docassemble date object that supports template formatting;
    invalid dates roll back, date-shaped text remains text, and code assignments
    are unchanged.
12. A seed-free regression interview can call `currency`, `redact`, additional
    standard date/number/text helpers, and an interview-declared module function
    after persisted-state loading and from every render source; no mandatory
    flow pass is required solely to restore those names.
