---
status: accepted
---

# Consolidate simulator runtime installation in one private module

`bootstrap.py` mixes one-time process hook installation with root-scoped
resource ownership and several runtime policies, tracked by process-global
flags (`_PREPARED`, `_STUBBED`, `_BACKGROUND_INSTALLED`, `_BACKGROUND_ACTION_MODE`,
`_DIAGNOSTIC_LOGGING_INSTALLED`, `_ATTACHMENT_FALLBACK_INSTALLED`). The
architecture review (finding 03) identified this as one installation knot:
first-call state can outlive a workspace, and tests must toggle private
globals. We accept the direction of deepening a single package-private
simulator runtime module that owns installation state, root activation, the
local numbered-file registry, and runtime policy (background-action mode, seek
diagnostics, PDF omission, logging dispatch), while the CLI composition root
remains the only place that applies process patches.

Implementation is a follow-up stream recorded in
`docs/family-flow-feedback-and-architecture-plan.md`; the durable file
registry in `_artifacts.py` and the diagnostic log dispatcher are its first
extracted parts. Until the stream lands, the process-global flags remain the
working implementation.

## Consequences

- No new public adapter or port: the module stays package-private, consistent
  with ADR-0002's rule that private runtime seams are not public adapters.
- Root-scoped state (file registry, effective-config path) is activated per
  operation so resources do not leak across workspaces inside one process.
- Tests stop toggling module globals and instead activate a runtime for a
  workspace.
- Keeping the current split (globals plus extracted registry/dispatcher) was
  rejected as the endpoint because root activation and policy ownership would
  still be split between the two, and the flags would keep leaking between
  roots in-process.