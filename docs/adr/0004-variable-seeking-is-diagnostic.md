---
status: accepted
---

# Treat resolved variable seeking as diagnostics

Docassemble uses `NameError` and Jinja `UndefinedError` as control signals while recursively seeking variables, so the simulator reports the resulting ordered variable/question trace as non-fatal diagnostics rather than raw errors. Only docassemble's terminal missing-variable result becomes an `unresolved-variable` failure; it retains the sought variable and trace so users can understand how resolution was attempted.

When the runtime retains ordered seek history, terminal attribution uses the last block considered for the exhausted variable, bounded by the preceding nested seek and the next variable stage. Its provenance and the expression that references the sought variable are included in the failure and attached to the matching diagnostic. Expression matching is path-specific: attributes, string keys, and indexes must identify the complete missing path, rather than merely sharing its root. Generic-object placeholders are matched only when the retained block is marked generic and the resolved path suffix agrees; otherwise the expression is left unknown. The simulator does not use `InterviewStatus.question` when seek history identifies a different block, because that field can still describe the last successfully displayed screen. If diagnostics are disabled or a target runtime does not preserve the history, the simulator reports no fabricated culprit and keeps the failure kind and sought variable unchanged.
