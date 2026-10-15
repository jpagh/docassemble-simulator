---
status: accepted
---

# Screen traces are an append-only sidecar contract

End-to-end interview tests need to record which screens a run passed through and compare a later run against that recording without positional assertions on generated question names or volatile text. The simulator now owns a **screen trace**: an append-only JSONL sidecar whose first line is metadata and whose later lines are trace records, one per execution operation. The contract lives in one public trace module so capture, comparison, and a future server-side lane share one canonicalizer.

## Decision

- The trace module owns screen identity derivation, the record/file schema, and comparison. Identity and comparison are pure functions over screen outcomes and trace values; only the reader/writer touch the filesystem.
- `--record PATH [--phase NAME]` on `start`, `refresh`, `answer`, `seek`, and `exec` appends a record for each execution operation. Automatic seeking intentionally expands into one record per attempted screen plus the post-walk seek outcome. Capture is an adapter concern: the execution operation interface, saved-session schema, and render pipeline are unchanged, and read-only inspection commands do not append.
- A failed operation records the unchanged active screen with the rejected assignments and `outcome: failed`, so validation re-asks appear as repeated occurrences of the same identity. A failure with no active screen records an `error:` identity instead.
- Screen identity falls back in this order: explicit block id (`question_name` is `ID <id>`), targeted variable (`sought == orig_sought`), generic object (placeholder `x` root anchored on the resolved root), indexed list target (placeholder `[i]` in `sought`), sorted field-variable tuple, then kind/category with a trace-local ordinal for collisions. Non-generic explicit ids remain `id:<id>`; generic-object and indexed-list explicit ids include an `@<anchor>` suffix so instances stay distinct. Numeric indices are normalized to `[i]`; generated `Question_<n>` names, random instance names, and resolved occurrence paths never enter identity. The rule that produced each key is recorded.
- Field facts (variable, type, visibility, required, choice values) are compared separately from identity and are order-insensitive. Rendered question and subquestion text is kept in the record but only compared with an explicit `--full-text`.
- Comparison supports `ordered`, `unordered` (a multiset), and `phased` (ordered groups of unordered multisets). `missing` and `extra` each take `strict` or `allow`, defaulting to strict; coverage counts are always reported. A trace mismatch is a typed `trace-mismatch` failure and exits 2.
- `--update` is the only write path that rewrites a golden, and reviewed exceptions record known differences with a reason. Exceptions cannot excuse order violations or simulator faults. Schema version 3 records the explicit-id anchor rule for generic-object and indexed-list instances plus replay intent (`answer`, `defaults`, `auto`, or `seek`) and seek context. Earlier schemas are rejected rather than silently interpreted with different operation semantics.

## Amendment

Issue #27 clarifies automatic-step cardinality and replay semantics. The
implementation now records the screen before every automatic browser-shaped
submission, including a rejected submission, with an explicit `auto` intent.
Replay compares that pre-submission screen and then applies `answer --defaults`
(or a per-step override) in its isolated session. The post-walk seek result is
recorded separately so the trace retains both the transition and the resulting
screen. A default `answer --defaults` is recorded as `defaults`, never as an
ordinary empty answer. A per-step replay override carries the same
explicit-blank intent as an ordinary `answer` (ADR-0013, issue #71), so a raw
empty required field stops the walk instead of being accepted. A trace schema
change is required for these meanings; earlier traces are input errors, not
silently reinterpreted.

Issue #9 clarified two points the original text left implicit, and the
implementation now enforces them:

- An outcome envelope whose `kind` is `error` is a failure outcome, never a
  screen. A failed operation with no active screen is recorded with a null
  screen and an `error:` identity derived from its error payload, whether the
  failure kind arrives nested in `details` or at the top level of a bare error
  outcome. `screen:error` is not a valid identity key. Capture and identity
  derivation share one predicate for this rule so the adapter and the
  canonicalizer cannot drift.
- A phased comparison takes its phase declaration from an explicit override
  first, then the golden's recorded `phase_order`, then the order the golden's
  own entries were recorded in. `phase_order` remains reserved for
  hand-written and driver-written traces: the CLI only ever serializes it as
  an empty list, never a declaration, so no per-invocation flag can make the
  metadata-mismatch rule fire.

Issue #38 clarifies the replay lifecycle without changing schema 4. The
`fresh`, `target`, and `activate` values on the automatic walk's trailing
post-walk record are the seek policy; older schema-4 automatic records that
left `activate` false on their per-screen entries remain readable because the
trailing record is authoritative. Replay initializes that target in one
operation-local namespace, compares each pre-submission screen, and applies the
recorded default or override in the same namespace. A non-activated walk never
persists its temporary target or mutations, so later replayed answers observe
the original Saved screen; an activated walk commits the resulting target only
once the walk completes. Identity and field facts are checked before each
submission, while flow and error verdicts are checked immediately after that
submission and before the walk can continue. A mismatch terminates the
operation; an activated replay does not save a partly replayed walk. Identity,
field-fact, flow, and error verdicts are computed by pure functions in this
module, while the CLI retains only execution and report orchestration.

Issue #45 gives the persistent worker the same capture contract, without changing the sidecar or its schema. `worker` (and its `run`/`journey` aliases) accepts `--record PATH [--phase NAME]` as a journey-wide default, and each request may override either field with its own `record`/`phase`; an absent or `null` field means "use the worker default", and a non-string value is an input error. The recorded `operation` is the canonical one-shot command spelling, so an alias request (`evaluate`, `execute`, `variables`, `state-seed`) records what `eval`, `exec`, `vars`, and `seed` record. Capture stays an adapter concern: the observer is a generic callable passed to the worker's `serve`, `worker.py` imports nothing from the trace module, and protocol-only requests (`ping`, `reset`, `shutdown`, malformed frames, and non-object frames) are never recorded. A recording failure is a typed `input` response and a stderr diagnostic, exactly as the one-shot path turns the same failure into a typed `input` failure and exit 1, so a truncated journey trace cannot report success; an operation that already failed keeps its own error, details, timing, and diagnostics and reports the recording failure as a `recording-failure` diagnostic, so the recording failure cannot mask the operation's result. The capture commands are therefore the one-shot `start`, `refresh`, `answer`, `seek`, and `exec` invocations plus the persistent `worker`/`run`/`journey` journeys; the persistent transport adds a capture command, not new record semantics.

Issue #43 completes the boundary issue #38 implied. The recorded automatic
group is a hard submission bound for replay: exhausting the recorded attempts
observes the resulting outcome instead of falling back to ordinary unbounded
automatic submission, so a changed interview cannot authorize a screen the
recording never submitted. The trailing post-walk record is immutable
expectation data on the same operation and is validated with the same
trace-owned verdict logic and accumulated identity context as a per-screen
expectation, inside the persistence decision: an activated walk is saved only
when every recorded attempt matched and the trailing outcome matched. A walk
that ends before its recorded attempts run out reports the earliest missing
expected step and saves nothing. Reaching the boundary is not itself a
mismatch, matching rejection/completion/resolution keep their existing meaning,
and ordinary non-replay automatic seeking keeps its existing unlimited
behavior. The command adapter reports execution's divergence decision rather
than inferring one by counting submissions, and a screen that failed a
pre-submission check is reported as observed, not submitted. One limit
remains: a hand-written schema-4 trace whose automatic entries have no
following `post_auto` marker still replays through the legacy per-entry answer
path, which submits the current screen without the pre-submission check and
keeps a non-activated walk's mutations for later replayed entries. The recorder
always writes the marker, so every recorded golden is covered by the hard
boundary.

Issue #51 fixes the replay vocabulary boundary. A recorded `status` response
replays as a read-only `Status()` observation, because the sidecar carries all
the input that operation needs, while `eval`, `vars`, `seed`, and `exec`
records carry no expression or code and replay cannot reconstruct them. A
golden containing one of those compare-only records is the typed `input`
error `trace step N records compare-only operation '<operation>'`, never a
fault and never a silent skip, and the one-shot CLI and the worker's
`trace-replay` request report the same message because both consume one shared
replay-report seam. Resolving a step-level input problem into that typed error
is part of the shared seam, so the two transports cannot drift.

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

## Rendered constraint facts (#62)

Screen fields also record the runtime-computed `constraints` (min/max,
minlength/maxlength, step, scale, currency symbol, and field metadata). These
are debug-only facts: the compared field-fact projection remains variable,
type, visibility, requiredness, and choice values. Constraint changes do not
change identity or comparison, even with `--full-text`. Existing schema-4
traces without constraints remain readable and comparable; no golden needs
re-recording for this additive description.

Numeric min/max/step and minlength/maxlength facts use numbers when the
rendered value parses numerically; nonnumeric bounds and scale, currency
symbol, and field metadata retain their rendered values. Captured submission
visibility expressions and requiredness do not enter trace comparison.
