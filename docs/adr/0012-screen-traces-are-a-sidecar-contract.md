---
status: accepted
---

# Screen traces are an append-only sidecar contract

End-to-end interview tests need to record which screens a run passed through and compare a later run against that recording without positional assertions on generated question names or volatile text. The simulator now owns a **screen trace**: an append-only JSONL sidecar whose first line is metadata and whose later lines are trace records, one per execution operation. The contract lives in one public trace module so capture, comparison, and a future server-side lane share one canonicalizer.

## Decision

- The trace module owns screen identity derivation, the record/file schema, and comparison. Identity and comparison are pure functions over screen outcomes and trace values; only the reader/writer touch the filesystem.
- `--record PATH [--phase NAME]` on `start`, `refresh`, `answer`, `seek`, and `exec` appends one record per invocation. Capture is an adapter concern: the execution operation interface, saved-session schema, and render pipeline are unchanged, and read-only inspection commands do not append.
- A failed operation records the unchanged active screen with the rejected assignments and `outcome: failed`, so validation re-asks appear as repeated occurrences of the same identity. A failure with no active screen records an `error:` identity instead.
- Screen identity falls back in this order: explicit block id (`question_name` is `ID <id>`), targeted variable (`sought == orig_sought`), generic object (placeholder `x` root anchored on the resolved root), indexed list target (placeholder `[i]` in `sought`), sorted field-variable tuple, then kind/category with a trace-local ordinal for collisions. Numeric indices are normalized to `[i]`; generated `Question_<n>` names, random instance names, and resolved occurrence paths never enter identity. The rule that produced each key is recorded.
- Field facts (variable, type, visibility, required, choice values) are compared separately from identity and are order-insensitive. Rendered question and subquestion text is kept in the record but only compared with an explicit `--full-text`.
- Comparison supports `ordered`, `unordered` (a multiset), and `phased` (ordered groups of unordered multisets). `missing` and `extra` each take `strict` or `allow`, defaulting to strict; coverage counts are always reported. A trace mismatch is a typed `trace-mismatch` failure and exits 2.
- `--update` is the only write path that rewrites a golden, and reviewed exceptions record known differences with a reason. Exceptions cannot excuse order violations or simulator faults.

## Considered options

1. **Harness-level recording only.** Each test driver wraps the `--json` envelope and writes JSONL itself. Zero simulator changes, but the canonicalizer is duplicated per driver and the identity rule cannot be shared with a server lane.
2. **A new execution operation.** Model recording as `run(Record(...))`. It would couple tracing lifecycle to execution and put a sidecar effect inside the operation contract that ADR-0002 deliberately keeps narrow.
3. **The chosen design.** A public trace module plus a CLI adapter: one canonicalizer, a sidecar that leaves persisted state untouched, and records derivable from the existing stable envelope so any transport can emit them.

## Consequences

- Traces are diffable, partial runs remain readable, and goldens are reviewed artifacts. Capture works on both supported runtime families without runtime-specific code.
- Clearing, archiving, or resetting saved sessions does not touch traces, and a session schema change cannot invalidate them.
- Identity is intentionally not uniqueness. Multiplicity, phases, and a driver that keys answers on identity are what make re-asks and per-element loops comparable.
- The `ID ` prefix and the `sought`/`orig_sought` pairing are compiler behavior, not simulator behavior; storing the identity rule with each key makes a future docassemble change visible as a rule change in a diff instead of a silent mismatch.
- Screen identity is a weak fallback for screens with no target and no fields; the category rule's ordinal disambiguation is positional, documented, and exercised by the identity study.
