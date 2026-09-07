---
status: accepted
---

# Legacy server config restore keeps the first-published daconfig

## Context

The 1.9 compatibility layer publishes the simulator configuration onto the
legacy server object. Issue #1's context-adapter decision says the adapter
"must restore prior state in a `finally` path", and story 8 requires nested
operations not to corrupt the surrounding operation's state. At review time it
was flagged that a first activation restores thread state unconditionally but,
when the server had no prior `daconfig`, leaves the freshly published config in
place rather than removing it.

## Decision

On legacy context exit, `_LegacyContextAdapter` restores a prior
`functions.server.daconfig` verbatim inside the `finally` path. A first
activation with no prior value keeps the freshly published config, matching
install-time binding. We deliberately do not delete the config in that case:

- with no prior value there is no surrounding state to leak into, so the
  restore goal of story 8 is already met;
- the install path itself binds `server.daconfig` at registration time
  (bootstrap, refresh), so removing the published config would regress
  install-time binding for flows that never enter an operation context;
- story 11's freshness requirement (per-read republish, modern-hook parity)
  applies within an operation, not across the first activation boundary.

## Consequences

- Nested operations with different roots or configurations never leak their
  server config outward (story 8), covered by
  `test_nested_contexts_restore_outer_server_config` and
  `test_exception_still_restores_server_config`.
- A first activation's config remains visible after the operation ends;
  `test_context_entry_refreshes_server_config` pins the within-operation
  freshness behavior that motivated moving story-11 assertions inside the
  operation.
- This partially refines the issue's "restore prior state" letter; the
  refinement is intentional and implemented exactly as described here.
