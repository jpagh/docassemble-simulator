# Architecture Smell Cleanup Plan

Status: proposed
Date: 2026-08-26
Depends on: `docs/architecture-refactor-follow-up-plan.md` (implemented), ADR-0002

## Goal

Eliminate the six standards-axis findings from the post-implementation review
(commits `2fc2d51`, `8ee406b`) without changing the documented CLI/JSON contract,
the exit-code map, or any accepted ADR decision. The spec-axis findings from
that review were already fixed in `8ee406b` and appear here only as context.

This is a locality and vocabulary cleanup, not a behavior stream. Every change
is either a pure internal refactor pinned by existing tests or a new pin test
for an invariant the review flagged as untested.

## Review findings in scope

| # | Finding | Disposition | Chosen design |
|---|---|---|---|
| 1 | `StateStore.load` and `load_snapshot` duplicate schema/identity/blob validation | Confirmed | One private `_read_payload(path, *, namespace)` used by both, with unchanged error strings |
| 2 | Two flock implementations (`StateStore.lock` sibling file vs `_files._destination_lock` hashed `/tmp` file) | Confirmed | One `flock()` primitive in `_files.py`; keep the two lock-location policies with a documented reason |
| 3 | Destination-safety policy split across render, execution, and `_files` | Confirmed | One private `validate_destinations()` guard invoked once at the execution/render seam; render supplies its protected paths through `_RenderPreparation` |
| 4 | Error `kind` strings decoded independently (`cli._exit_for_error`, envelope) | Confirmed | `ErrorKind` StrEnum in a shared private `_outcomes.py`; exit-code mapping stays in the CLI adapter |
| 5 | `ExecutionOutcome`/`ExecutionError` mirror `RenderOutcome`/`RenderFailure`, mirrored again by `cli._envelope` | Confirmed | Shared generic `Outcome[T]` and `Failure` in `_outcomes.py`; existing public names remain as typed aliases |
| 6 | `from_safeid_safe(getattr(field, "saveas", "") or "")` recurs at four sites | Confirmed | `describe.field_variable(field)` helper; adopt at all four sites |

## Assumptions to make explicit

1. **The JSON contract is byte-stable.** `ErrorKind` is a `str`-subclass enum, so
   envelope values serialize to exactly the current strings (`"input"`,
   `"validation"`, …). Exit codes are unchanged and remain a CLI-adapter
   concern. The subprocess contract tests are the guard.
2. **Public names are preserved.** `ExecutionOutcome`, `ExecutionError`,
   `RenderOutcome`, `RenderFailure`, `RenderResult`, and all operation/source
   classes stay importable from the same modules with the same constructor
   shapes. Tests and callers that construct or `isinstance`-check them keep
   working.
3. **Lock scope is not changing.** Session mutations keep the per-interview
   sibling lock and their existing read-modify-write transaction. Snapshot and
   artifact writes keep per-destination locks. Only the flock body is shared.
   The session lock stays workspace-local (sibling file) so separate workspaces
   and test temp dirs never contend; destination locks stay global per-user
   because destinations can live anywhere.
4. **Validation ordering is preserved.** Destination checks run before any
   session load, fixture execution, or seed work. Moving render's early-return
   dest check into the seam does not change when checks happen, only where the
   failure is produced — still kind `input`, same message strings.
5. **Error message strings are preserved** so stderr/human output and existing
   tests do not churn. Where a message must live in the new guard, the guard
   receives the reason label that reproduces the exact current text.
6. **No new public surface.** `_outcomes.py`, `_files.py` additions, and the
   seam-carried `ProtectedPaths` are package-private, consistent with ADR-0002's
   "private runtime seams are not public adapters" rule.
7. **Typed vocabulary is still adapter-free.** The CLI keeps its own
   `EXIT_CODES` mapping; the enum does not know about exit codes or JSON.

## Design

### 1. Shared payload reader (`execution.py`)

```python
def _read_payload(
    path: Path, *, identity: str, load_namespace: bool
) -> dict[str, Any]:
    # open + pickle.load -> schema check -> interview check -> blob check
    # -> optional namespace unpickle; raises ExecutionFailure("state", ...)
    # with the exact current strings for "saved state" and "snapshot"
```

`StateStore.load` and `StateStore.load_snapshot` become thin callers. Error
strings are parameterized only where they already differ (`saved state …` vs
`snapshot …`). This is a pure extraction; existing round-trip, schema, and
identity tests are the pins.

### 2. One flock primitive (`_files.py`)

```python
@contextmanager
def flock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
```

- `StateStore.lock` becomes `with flock(self.lock_path)` (keeps the sibling
  `*.lock` next to the session file).
- `_destination_lock(target)` becomes `with flock(hashed_lock_file(target))`
  (keeps the hashed `/tmp` location).

Alternatives considered: moving session locks to the hashed scheme (rejected:
loses workspace locality, makes test temp workspaces contend globally) and
leaving the duplication (rejected: two implementations of one primitive).

New pin test: concurrent session mutations from multiple processes must be
serialized — N processes each running `exec 'counter += 1'` (plus an
interleaved `answer`) leave `counter == N` with a valid final session. This
covers the "lock contention cannot produce a lost update" dimension the review
noted as untested.

### 3. One destination guard (`_files.py` + seam)

```python
@dataclass(frozen=True)
class ProtectedPath:
    path: Path
    reason: str          # exact message fragment, e.g. "saved-session storage"

def validate_destinations(
    destinations: tuple[Path, ...],
    protected: tuple[ProtectedPath, ...],
) -> None:
    # duplicates among destinations      -> "snapshot and artifact destinations must be different"
    # destination equals a protected file -> "render effects cannot replace {reason}"
    # destination under a protected dir   -> "render effects cannot write inside {reason}"
```

- `_RenderPreparation` gains a `protected: tuple[ProtectedPath, ...]` field.
- `InterviewRenderer.render` stops calling `_validate_effect_destinations`
  directly. It builds the protected list (template file + fixture/snapshot
  inputs as `"a template or render input"` files; every package
  `data/templates` tree as `"template directories"` dirs) and passes it through
  the private preparation request.
- `InterviewExecution._with_render_state` appends its session directory as a
  `"saved-session storage"` dir and runs `validate_destinations` once, before
  loading any source namespace. `_reject_session_destinations` and
  `_validate_effect_destinations` are deleted.

Why one pass at the seam: execution is the only place that sees both the
session store and the destinations; render is the only place that knows the
template and inputs. The private request object is exactly the mechanism the
two already share (ADR-0002's callback-scoped seam). A separate guard module
without the seam would still need two call sites and two error pathways.

New pin tests:

- template-dir rejection happens before fixture execution: a fixture that
  writes a marker file, with `output` inside `data/templates` → failure, marker
  absent;
- snapshot destination inside a package `data/templates` tree → rejected;
- the four existing destination tests (session storage, template tree, input
  alias, snapshot/artifact collision) keep passing unchanged as the regression
  net.

### 4. Error vocabulary (`_outcomes.py`)

```python
class ErrorKind(StrEnum):
    INPUT = "input"
    STATE = "state"
    ANSWER_INPUT = "answer-input"
    VALIDATION = "validation"
    EXECUTION = "execution"
    SEEK = "seek"
    RENDER = "render"
    COMPILE = "compile"
    FAULT = "fault"
    WORKSPACE = "workspace"
    CONFIGURATION = "configuration"
```

- `ExecutionFailure.__init__`, `Failure.kind`, and render's failure
  constructions take `ErrorKind`; `details` remains `dict[str, Any] | None`.
- `cli._exit_for_error` becomes `EXIT_CODES = {ErrorKind.INPUT: 1, …}` with the
  current default `2` for every other kind; `cmd_catalog` uses
  `ErrorKind.COMPILE` for its ad hoc error dict.
- Because `StrEnum` is a `str`, `json.dumps` output is identical; the
  subprocess envelope tests (`--json answer`, unknown command, render conflict)
  are the pins.

Alternative considered: plain string constants instead of an enum (rejected:
constants do not constrain the ~10 construction sites, which is the actual
smell).

### 5. Shared outcome type (`_outcomes.py`)

```python
@dataclass(frozen=True)
class Failure:
    kind: ErrorKind
    message: str
    details: dict[str, Any] | None = None

@dataclass(frozen=True)
class Outcome(Generic[T]):
    ok: bool
    result: T | None = None
    error: Failure | None = None
```

- `execution.py`: `ExecutionError = Failure`, `ExecutionOutcome = Outcome[Any]`
  (kept in `__all__`).
- `render.py`: `RenderFailure = Failure`, `RenderOutcome = Outcome[RenderResult]`
  (kept in `__all__`).
- `cli._envelope` simplifies: the generic `_clean` dataclass handling already
  converts typed outcomes and failures to the documented envelope, so the
  `hasattr(error, "kind")` branch and the mirrored dictionary construction
  collapse. Verified by the JSON presentation tests.

Caveat to check in the slice: `isinstance(outcome, RenderOutcome)` must keep
working through the generic alias (it checks the origin class); the existing
contract tests assert exactly this and are part of the red step.

Alternative considered: leaving the two outcome types separate (rejected: the
mirrored anatomy is the review finding; a shared private vocabulary does not
expose anything new).

### 6. Field-variable helper (`describe.py`)

```python
def field_variable(field: Any) -> str:
    return from_safeid_safe(getattr(field, "saveas", "") or "")
```

Adopt at `execution.py` field-type and validation sites, `catalog.py` question
listing, and `describe.py`'s own internal use; export from `describe.__all__`.
Existing describe/catalog tests are the pins.

## Work plan

### 1. Pin the contracts (red)

- `cli._exit_for_error(ErrorKind.INPUT) == 1`, `…FAULT == 3`, unknown-kind
  default `2` (new unit test).
- `ErrorKind` values serialize as the exact current strings through
  `_envelope`/`_emit` (new unit test plus existing subprocess tests).
- Outcome aliases: existing `isinstance(outcome, RenderOutcome)` tests keep
  passing (they are the red step if aliasing breaks).
- Destination ordering: marker-file fixture test (new, fails today because
  render's early check happens before execution but the ordering assertion
  pins it for the refactor); template-dir snapshot test (new).
- Session lock contention: multi-process `exec 'counter += 1'` serialization
  test (new; deterministic final value).
- `_read_payload` and schema-error messages: corrupt-file and wrong-interview
  tests (new pins for the extraction).
- `field_variable`: direct unit test on a `SimpleNamespace` field with an
  encoded `saveas` (new).

### 2. Introduce `_outcomes.py` and `ErrorKind` (slice)

Implement `Failure`/`Outcome`/`ErrorKind`; switch execution, render, and CLI to
the enum; update `_exit_for_error` and `cmd_catalog`; run the full suite. The
JSON and exit-code contract tests are the gate.

### 3. Unify the outcome aliases (slice)

Alias `ExecutionError`/`ExecutionOutcome`/`RenderFailure`/`RenderOutcome`;
simplify `cli._envelope`; run the suite, paying attention to `isinstance` and
constructor-shape tests.

### 4. Flock unification (slice)

Add `flock()` and `hashed_lock_file()` to `_files.py`; convert `StateStore.lock`
and `_destination_lock`; add the concurrent-session-mutation test; run the
suite.

### 5. Single destination guard (slice)

Add `ProtectedPath`/`validate_destinations()` to `_files.py`; extend
`_RenderPreparation` with `protected`; move render's destination check into the
seam; delete `_validate_effect_destinations` and `_reject_session_destinations`;
add the ordering and template-dir snapshot tests; run the suite.

### 6. `_read_payload` extraction (slice)

Extract the shared reader with the schema/identity tests; run the suite.

### 7. `field_variable` adoption (slice)

Add the helper, adopt at four sites, run the suite.

### 8. Documentation and gates

- Add one sentence to ADR-0002: typed error/outcome vocabulary is shared
  package-private (`_outcomes.py`); external shapes unchanged.
- Mark this plan implemented.
- Run `ruff check --select F,E9 src tests`, `git diff --check`, the fast suite,
  and `scripts/test-real-runtime <target-python>`.
- Re-run the two-axis review against `origin/main` and address anything new.

## Implementation order

1. Pin tests (vertical, one per finding).
2. `_outcomes.py` + `ErrorKind` adoption.
3. Outcome aliasing + envelope simplification.
4. Flock unification + contention test.
5. Single destination guard + ordering tests.
6. `_read_payload` extraction.
7. `field_variable` helper.
8. Docs, gates, re-review.

## Acceptance criteria

1. JSON envelope bytes and exit codes are unchanged for every command path;
   `ErrorKind` serializes as the current strings.
2. All previously exported names remain importable with the same constructor
   shapes; `isinstance`-based contract tests pass.
3. Destination validation is exactly one private guard invoked once per render
   request at the execution seam, before session load or fixture/seed work,
   with the current message strings and `input` kind.
4. There is exactly one flock implementation; session locks remain sibling,
   per-interview files and destination locks remain hashed `/tmp` files;
   N-process session mutation leaves a valid final session with no lost update.
5. `StateStore.load`/`load_snapshot` share `_read_payload` with unchanged error
   strings.
6. Error kinds are a single typed vocabulary; the CLI owns the only exit-code
   mapping.
7. `field_variable` is the single decode path at all four sites.
8. `_outcomes.py` and `_files.py` additions are package-private; no new public
   adapter or port.
9. Fast suite, real-runtime lane, ruff, `git diff --check`, and the follow-up
   re-review all pass.

## Non-goals

- No change to CLI/JSON contract, command names, flags, or exit codes.
- No change to lock scope, transaction policy, or where locks live.
- No new public seams, storage ports, or runtime adapters.
- No message-text churn in user-facing errors.
- No redesign of render semantics, screen descriptions, or bootstrap.
- No migration of error kind spellings; the enum values are the existing
  strings.