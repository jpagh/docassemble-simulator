---
status: accepted
---

# Use typed operations and callback-scoped render preparation

The simulator exposes interview execution as `run(operation) -> outcome`, with typed operation variants, and keeps catalog inspection in a separate read-only module. Execution invokes a narrow render callback while its prepared docassemble context is active, so mutable namespaces, compiled interviews, statuses, and context managers never escape to CLI or persistence code.

## Considered options

1. **Typed operation variants.** `run(Start())`, `run(Answer(...))`, `run(Evaluate(...))`, `run(Seek(..., activate=True))`, `run(Execute(...))`, and `run(PrepareRender(..., action))` all enter one dispatcher. Each variant fixes ordering, error modes, and commit policy; read-only variants cannot request a commit. Start creates fresh state; answer validates and commits atomically; evaluation reads without committing; active seek commits only when requested; execute commits after successful code and normally assembles; render preparation invokes its callback in context and never changes the session. State, input, validation, seek, execution, and unexpected-fault errors are typed outcomes.
2. **Session-oriented runner methods.** The corresponding calls would be `runner.start()`, `runner.answer(...)`, `runner.evaluate(...)`, `runner.seek(..., activate=True)`, `runner.execute(...)`, and `runner.with_render_state(request, action)`. Each method could own complete ordering and declare its persistence effect, while the callback would preserve context locality. This is discoverable, but persistence and error invariants become repeated method contracts and the interface grows with every operation.
3. **Command-focused use-case functions.** The corresponding calls would be `start_interview(config)`, `answer_screen(config, ...)`, `evaluate_saved(config, ...)`, `activate_seek(config, ...)`, `execute_saved(config, ...)`, and `render_prepared(config, request)`. Each function could implement complete ordering and fixed persistence, and render could accept a private context callback. This optimizes individual command paths but repeats configuration, loading, transaction, and error knowledge; couples execution vocabulary to command names; and weakens the execution/render seam.

The typed-operation design has the deepest interface and best locality: operation variants encode requested behavior while the dispatcher owns lifecycle and persistence. Catalog compilation remains separate because static inspection has no session transaction. Render owns request validation, template evaluation, include passes, expectations, and artifact effects; execution owns source preparation, rehydration, seed timing, assembly policy, and the live context in which the callback runs.

## Consequences

Callers cannot control lifecycle stage ordering or receive runtime state. Saved sessions and snapshots are versioned and interview-specific. Read-only operations have no commit path. Tests use the catalog, execution, render, and CLI interfaces; private runtime seams are not public adapters.
