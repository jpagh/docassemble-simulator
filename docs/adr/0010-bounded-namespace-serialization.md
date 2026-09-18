---
status: accepted
---

# Bound docassemble's namespace serializer

docassemble serializes the whole interview namespace while handling an
assembly failure: `parse.py` calls `serializable_dict(user_dict)` whenever
`interview.debug` is on, and `exec_with_trap` calls it unconditionally for
untrapped code-block errors. That serializer routes every value through
`functions.safe_json`, which caps **depth** at 20 but tracks no visited
objects. Shared and cyclic references are therefore re-expanded on every
path, so the walk is exponential in the density of the object graph rather
than bounded. Three mutually-referencing `DAObject`s already take about 25
seconds; four never finish. A target Interview that both builds such a graph
(for example from API/SDK response objects) and references an undefined
variable never reaches docassemble's `DAErrorMissingVariable`: the simulator
blocks inside `interview.assemble`, so the CLI emits no envelope at all.

## Decision

- At bootstrap the simulator wraps `docassemble.base.functions.safe_json`
  with path-based cycle detection and a per-serialization node budget
  (`_NODE_BUDGET`, 5000). The wrapper is installed only when docassemble and
  its `safe_json` are importable, is idempotent per function object, and
  shares one budget across nested calls through a `ContextVar`.
- Truncated branches return `None` (`'None'` for keys), the same shape
  docassemble's own depth cutoff produces. Graphs that the guard does not
  cut keep their current output.
- The guard lives in `_serialization.py` and is installed by
  `_runtime.bootstrap()` alongside the other process-level docassemble
  patches. It does not change Interview execution, seeking, or diagnostics;
  it only bounds the serializer docassemble invokes on its error and
  `all_variables`/`as_serializable` paths.
- Documented in this ADR rather than as a user-facing setting: a hang is
  never a fidelity result, and there is no correct interview that depends on
  exponential serialization.

## Consequences

- Assembly failures caused by an undefined variable surface as
  `unresolved-variable` even when the namespace contains a dense cyclic
  object graph. The reported `payment_offering` flow is the motivating case.
- Serialization of very large or deeply shared namespaces may truncate some
  branches to `None` once the node budget is reached. That output is already
  lossy at depth 20 upstream and is diagnostic metadata, not interview state.
- Both tested runtime families carry the same upstream behavior, so the
  guard is version-independent: it wraps whichever `safe_json` is installed.
- A future upstream `safe_json` that adds its own visited-set would be
  shadowed by the guard; the guard's output is a subset of a bounded walk, so
  removing it later is safe.
