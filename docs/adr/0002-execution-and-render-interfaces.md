---
status: accepted
---

# Use typed operations and callback-scoped render preparation

The simulator exposes interview execution as `run(operation) -> outcome`, with typed operation variants, and keeps catalog inspection in a separate read-only module. Execution invokes a narrow render callback while its prepared docassemble context is active, so mutable namespaces, compiled interviews, statuses, and context managers never escape to CLI or persistence code.

## Considered options

1. **Typed operation variants.** `Start`, `Answer`, `Status`, `Refresh`, `Seek`, `Evaluate`, `Variables`, `Execute`, and render preparation all enter one dispatcher. Each variant fixes ordering, error modes, and commit policy; read-only variants cannot request a commit. Start creates fresh state; answer validates and commits atomically; evaluation reads without committing; active seek commits only when requested; execute commits after successful code and normally assembles; render preparation invokes a callback in context and never changes the session.
2. **Session-oriented runner methods.** Cohesive `start()`, `answer()`, and related methods are easy to discover, but persistence invariants and context preparation become repeated method contracts and the public surface grows with every operation.
3. **Command-focused use-case functions.** Functions make individual CLI paths direct, but duplicate configuration and lifecycle knowledge, couple execution vocabulary to command names, and provide a weak execution/render handoff.

The typed-operation design has the deepest interface and best locality: operation variants encode requested behavior while the dispatcher owns lifecycle and persistence. Catalog compilation remains separate because static inspection has no session transaction. Render owns request validation, template evaluation, include passes, expectations, and artifact effects; execution owns source preparation, rehydration, seed timing, assembly policy, and the live context in which the callback runs.

## Consequences

Callers cannot control lifecycle stage ordering or receive runtime state. Saved sessions and snapshots are versioned and interview-specific. Read-only operations have no commit path. Tests use the catalog, execution, render, and CLI interfaces; private runtime seams are not public adapters.
