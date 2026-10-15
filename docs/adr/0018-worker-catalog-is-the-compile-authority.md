---
status: accepted
---

# The worker's interview catalog is its compile authority

> Recorded from the issue #69 investigation (evidence and the strongest
> counterargument are in that issue). This supersedes ADR-0016's caller-refresh
> protocol: the reload guarantee stays, the adapter-side
> refresh-and-failure-handoff choreography goes.

## Context

ADR-0016 makes a persistent worker reload its interview source lazily, and it
records a protocol around that reload: a caller that compiles *outside* an
operation — the worker's read-only `check`/`questions`/`index` requests, and a
trace replay's short-lived execution — must first run
`InterviewCatalog.ensure_current(identity)`, then hand any `CompileFailure` to
the catalog or execution that would otherwise compile the same broken source
again. Without the first step the caller reads a stale entry from docassemble's
process-global compiled-interview cache; without the second, one failing
request pays for two compiles.

The protocol exists because watches live on the worker's long-lived
`InterviewCatalog` instance while readers construct *fresh* catalog instances
that see neither the watch records nor the failed attempt. The ordering and the
failure carrier are therefore knowledge every adapter must reconstruct — in
`cli.py`'s `_worker_catalog_source` and `_trace_replay_worker_request` today,
and in any adapter added later.

## Decision

- One cached `InterviewCatalog` per worker is the worker's only compile
  authority. It owns the current-source watches, the compiled identities, and
  the current compile attempt.
- Worker inspection, `why`, render, and trace replay compile through that
  instance. `InterviewExecution` accepts an optional `catalog=` and exposes it
  read-only; a worker constructs one catalog and shares it with the readers it
  spawns. A replay still runs in its own temporary session directory — sessions
  stay isolated; compilation is shared.
- `ensure_current` (catalog and execution) and `CompileFailure` are removed.
  `_compile` performs the revalidation for every identity it is asked for,
  watched or not, so the ordering obligation has no call site.
- A failing compile raises into the consumer's own typed path (a `check` row, a
  `CatalogFailure`, a replay divergence). One request pays one compile, and no
  compile failure clears or mutates the Saved session.
- `_reload_source`'s never-watched eviction branch is retained and made
  reachable from `_compile`: when this catalog has no signature for an identity
  that docassemble's cache already holds, the cached interview's declared
  workspace helpers are evicted (bytecode included) before the entry is
  dropped.
- Every ADR-0016 guarantee is unchanged: workspace containment through
  `Path.resolve` for watching, eviction, and bytecode removal (including
  escaping symlinks), declared-helper eviction from `sys.modules`, bytecode
  invalidation for same-size rapid edits, include/routing sources with no
  compiled question, deleted or unreadable watched sources counting as changed,
  and unchanged one-shot compilation.
- Generic worker handler adapters, the catalog/execution module separation, and
  CLI/worker payload parity are unchanged. There is one cache implementation;
  no interchangeable cache adapter is introduced.

## Consequences

- Adapters no longer refresh before reading or carry a failed attempt between
  readers; the protocol is deleted rather than relocated.
- `InterviewExecution` gains an optional `catalog=` composition input and a
  read-only accessor. This is a constructor addition to an exported class, not
  a new operation or a public cache interface: the typed-operation interface
  (`run(operation)`) is unchanged.
- Constructor-order assertions are replaced by behaviour tests: fresh
  inspection and replay after an edit, one compile per failing request,
  unchanged warm requests, typed errors, and Saved-session preservation.
- The shared catalog caches every identity a replay names, not only the
  worker's own selector.
- Residual risk to prove during implementation: the never-watched eviction path
  under the shared catalog, and full both-lane verification of the new
  ownership.
