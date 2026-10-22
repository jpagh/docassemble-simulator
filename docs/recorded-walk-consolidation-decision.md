# Recorded automatic walks: consolidation decision (#78)

## Decision: no-go

Do not consolidate capture and replay interpretation in a production refactor
on the present evidence. The most consequential policy is already shared:
trace-owned identity and verdicts, execution-owned bounded walking and saving,
and one CLI/worker replay report seam. The remaining capture/replay knowledge
is mostly complementary encoding and decoding, not independently repeated
policy. The strongest reason **not** to consolidate is that a cross-layer
walk abstraction would have to expose those distinctions anyway, or hide the
execution transaction boundary behind a trace/capture API. Neither outcome
deletes enough caller knowledge to justify the new interface.

This is the investigation authorized by [#78](https://github.com/jpagh/docassemble-simulator/issues/78),
not a behavior change. Schema 4, production code, and checked-in goldens are
unchanged. The boundary follows [ADR-0012](adr/0012-screen-traces-are-a-sidecar-contract.md),
[ADR-0002](adr/0002-execution-and-render-interfaces.md),
[ADR-0017](adr/0017-persistent-worker-and-standard-runner.md), and
[ADR-0018](adr/0018-worker-catalog-is-the-compile-authority.md).

## Current owners and consumers

Source symbols below identify the inspected seams rather than unstable line
numbers. Paths are relative to `src/docassemble_simulator/`.

| Decision | Current owner | Consumer / observable consequence |
| --- | --- | --- |
| Expand an automatic result into attempted-screen records and a trailing observation | `cli.py`: `_record_execution`, using execution's `auto_trace` | Both one-shot capture and `_worker_journey_recorder` use the same encoder. Each attempted submission gets `intent="auto"`; the final seek gets `post_auto=True`. Destination, phase, failure reporting, and filesystem effects remain adapter concerns. |
| Fresh-marker ownership | `_record_execution` | Only the first expanded automatic entry gets the operation's `fresh`; the trailing entry has `fresh=False`. With no automatic attempts there is no expanded group, and the final seek retains `fresh`. Group replay uses the first entry's `fresh` to initialize one namespace; it does not restart at the trailing observation. |
| Record shape, canonical identity, accumulated identity context | `trace.py`: `TraceEntry`, `append_trace`, `assign_identity`, `replay_identity_context` | Capture, comparison, replay reporting, and execution's immutable expectations share these rules. The module does not choose submissions or save state. |
| Recognize an automatic group versus legacy entries | `cli.py`: `_trace_replay_report`, `_trace_replay_automatic_group` | A maximal adjacent run of `intent or operation == "auto"` followed immediately by `post_auto` is replayed together. The marker separates two adjacent walks. Without it, replay uses `_trace_replay_step` per entry. |
| Choose target and activation | `_trace_replay_automatic_group` | The target and fresh flag actually come from the **first automatic entry**. Activation comes from the **trailing entry**, even if false and contradicted by true per-screen flags. The helper's first-entry activation fallback is not the markerless compatibility path: the report dispatcher only calls it for a marked group. Capture currently copies activation to both entry kinds. |
| Treat the trailing record as expectation, not another submission | Adapter builds `replay_expectation(post)`; execution's `_replay_walk_divergence` consumes it | `ReplayAutomatic` compares the post-walk observation inside the persistence decision. The adapter reports it as another matched/divergent trace step, not another seek. `_trace_replay_step` also treats a standalone `seek/post_auto` as a Status observation; ordinary recorder groups consume their marker together. |
| Bound submissions, compare before submitting, detect early end, save only after all verdicts match | `execution.py`: `ReplayAutomatic`, `_replay_automatic_operation`, `_seek_auto`, `_replay_walk_divergence` | The recorded attempts are a hard bound. Identity/field facts are checked before submission; flow/error immediately after; the trailing result before saving. Activation saves once only if there is no divergence. Nonactivation discards the operation-local target and mutations. Ordinary automatic seeking remains separate and unbounded by a recording. |
| Pure verdict policy | `trace.py`: `replay_expectation`, `compare_replay_expectation`, `compare_replay_entries` | Execution's transaction checks and adapter reports use the same identity, field-fact, flow, and error policy. Divergence reporting copies execution's verdict, rather than deciding persistence from report cardinality. |
| Markerless schema-4 compatibility | `cli.py`: `_trace_replay_step`, `_trace_replay_operation` | Hand-written automatic entries observe Status, then submit an Answer (defaults for empty assignments). Comparison follows submission, not a pre-submission guard. Mutations persist for later entries even with `activate=False`. If Status fails and the entry is fresh, an activated fresh Seek initializes the private session. These are not recorder-produced walk semantics; ADR-0012's #43 amendment explicitly preserves this limit. |
| Replay lifecycle and transport reporting | `cli.py`: `_trace_replay_report`, `cmd_trace_replay`, `_trace_replay_worker_request` | CLI and worker already share reports and typed input errors. Replay creates a temporary session directory, never writes its golden, and worker replay passes `worker.execution.catalog` as compile authority. `worker.py` keeps generic handlers/observer callbacks and no trace-specific imports. |

The actual first-entry target/fresh consumption is important when reading
ADR-0012's #38 summary of seek policy: an expanded recording deliberately moves
freshness off the trailing seek. This investigation does not add precedence
rules for contradictory target/fresh values in hand-authored traces.

## Repeated decisions versus repeated work

| Candidate repetition | Would consolidation delete caller knowledge? |
| --- | --- |
| Capture distributes fresh/activation flags; replay reads them | Mostly no: encoding an operation's attempts and decoding an immutable recording are inverse responsibilities, not two independent policy implementations. A pure projection helper could relocate index/flag expressions but capture must still supply execution observations and replay must still choose an operation. |
| Group dispatcher and group runner both scan to the first non-auto entry and inspect its marker | It could delete one loop and repeated recognition predicate. This is real repeated scanning within one adapter, not multiple semantic owners. Passing an end index would save scanning but not remove capture or execution knowledge. It does not motivate a cross-layer abstraction. |
| Trailing activation overrides per-screen activation | The precedence decision exists once in group construction. Capture's copying of the same policy is not another precedence decision; execution consumes an already chosen boolean. Moving selection into a trace helper removes it from this one caller only, not from competing callers. |
| Capture emits a trailing record; replay reports it; execution validates it | These are three necessary roles: describe, report, and gate persistence. They cannot all disappear. A pure trace grouping value could hide marker consumption from replay, but would not replace capture's expansion or execution's hard bound/save checks. |
| Adapter builds actual entries while execution builds immutable expectation verdicts | Repeated observation shaping/comparison work, but no duplicated comparison policy: both end at `compare_replay_entries`. Execution needs a verdict before persistence; report construction happens later. Removing the execution check in favor of adapter reporting would break divergence-before-save. |
| Legacy and grouped replay both submit defaults/overrides | They intentionally differ in state lifetime and comparison timing. Treating legacy records as a marked walk would silently change compatibility. Both eventually use execution's browser-shaped submission acceptance, already shared. |
| CLI and worker replay | Already consolidated by `_trace_replay_report` and the generic worker handler. **No new benefit** credited here. Likewise shared catalog compilation is already implemented, not a prospective gain. |

## Executable characterization index

All entries below are in `tests/test_trace_real_runtime.py` unless stated
otherwise. Tests use per-test `tmp_path` projects and existing `_record`,
`worker_journey`, and `_command` harnesses. New assertions are public CLI,
worker, and trace-value behavior, not private helper order.

- **Extended** `test_trace_replay_groups_adjacent_recorded_nonactivated_walks`:
  real capture writes `start, auto, auto, seek/post_auto, auto, auto,
  seek/post_auto, answer`. Both walks replay, then the answer still addresses
  the original Saved screen. Exact CLI/worker report equality, eight matched
  entries, unchanged capture bytes/Saved session, and temporary replay cleanup
  distinguish this from a hand-written automatic fixture. Existing successful
  override and invalid-field divergence checks remain.
- **Added** `test_trace_replay_trailing_activation_overrides_contradictory_automatic_entries`:
  parameterized in both directions. Capture a real walk and subsequent Refresh,
  then write a **separate compatibility copy** with only per-auto activation
  flipped. The real trailing marker remains authoritative. Refresh observes
  either the committed target or original Saved screen, so a clean replay
  proves activation precedence, not just matching walk observations. Both
  original/copy bytes and caller sessions stay unchanged during replay.
- **Added** `test_trace_replay_hand_written_markerless_schema4_keeps_legacy_mutations`:
  explicitly hand-authored schema-4 entries, using screens obtained from normal
  answers and metadata from a start-only capture. There is no captured automatic
  walk and no `post_auto`. Two automatic default submissions with activation
  false still leave mutations visible to the following Status. Five matches
  characterize the documented legacy path without calling it a recorded walk.
- **Strengthened** `test_trace_replay_groups_fresh_activated_multi_screen_walk`:
  real capture has exactly one fresh entry, on the first attempt, never on its
  post-walk marker; replay succeeds without an initial Start.

Relevant existing coverage retained and exercised:

- `test_trace_replay_round_trips_default_auto_failure_and_answer` and
  `test_worker_journey_recording_matches_the_one_shot_cli`: attempted/rejected
  submissions, ordinary defaults, and one-shot/worker capture equivalence.
- `test_trace_replay_preserves_nonactivated_seek_context`: later answers use
  the Saved context, not the temporary sought screen.
- `test_trace_replay_stops_before_later_automatic_effect_on_first_identity_divergence`,
  `test_trace_replay_never_submits_an_unrecorded_automatic_screen`,
  `test_trace_replay_reports_a_trailing_outcome_divergence_without_submitting`,
  `test_trace_replay_reports_the_earliest_missing_expected_step`, and
  `test_trace_replay_execution_keeps_divergent_activated_walks_out_of_the_session`:
  bounded submissions, earliest divergence, no later effects, rollback, and no
  partial saved session (including fresh replay).
- `test_worker_trace_compare_and_replay_match_the_cli` and
  `test_worker_trace_replay_sees_an_edit_between_requests`: immutable goldens,
  isolated sessions, shared reports, and the warm worker's current-source
  catalog (shared-catalog replay coverage lives here).
- `tests/test_trace.py`: pure replay verdicts, malformed/compare-only input,
  comparison/identity tests, and the older stubbed markerless automatic test.
- `tests/test_submission_acceptance.py`: shared submission-acceptance runtime
  coverage in the required gate selection.
- `tests/test_detect_session.py`: unmarked, stub-based execution-lifecycle
  coverage. It runs in the fast lane; the gate's `real_runtime and not corpus`
  selection excludes it.

## Depth, locality, leverage, and deletion test

- **Depth:** A pure recorded-group value could hide adjacency and activation
  selection. It cannot hide the actual difficult policy (bounded browser
  submission and save timing), which already belongs to execution. A configurable
  replay adapter would expose more policy than it hides and is not proposed.
- **Locality:** Recognition and activation selection already live together in
  the CLI adapter; both transports reach that same location. Capture is also
  shared by both transports. Trace owns pure comparisons and execution owns
  mutable state. There is no demonstrated second independent interpreter to
  reconcile.
- **Leverage:** Adjacent walks, contradictory activation flags, and markerless
  entries all work through the existing boundary on both runtime families.
  The tests add leverage by pinning distinctions a refactor could accidentally
  erase; code movement alone supplies little additional leverage.
- **Deletion test:** A proposed helper can delete a duplicate scan and replace
  some flag/slice expressions with a constructor. It cannot delete capture's
  need to understand expanded attempts, replay's grouped-versus-legacy dispatch,
  or execution's walk/save decisions without either moving those responsibilities
  wholesale or changing behavior. There is no ADR-0018-style caller refresh
  protocol whose obligations disappear across several readers.

**No consolidation follow-up is recommended now.** Reconsider only with evidence
of an independent consumer repeating group/legacy/precedence decisions and a
concrete deletion list that removes that consumer's knowledge. Any future pure
trace interpretation must leave capture effects and report orchestration in
adapters, walk/save decisions in execution, generic worker handlers intact, and
shared compilation separate from replay session isolation.

## Validation and limitations

Exact final commands and results:

| Command | Result |
| --- | --- |
| `mise run lint` | Passed: ruff, tombi (6 files), pyrefly (0 errors). |
| `mise run test:fast` | 697 passed, 253 deselected (main's supplied baseline: 697 / 247; six new runtime parameter instances explain the selection difference). |
| `scripts/test-runtime tests/test_trace_real_runtime.py -k 'adjacent_recorded or trailing_activation or hand_written_markerless or fresh_activated_multi'` | 10 selected/passed: modern 5, legacy 5; no failures or skips. |
| `scripts/test-runtime tests/test_trace_real_runtime.py tests/test_submission_acceptance.py tests/test_detect_session.py` | 92 selected/passed: modern 46, legacy 46; no failures or skips. |

The checked two-family gate used docassemble-base/webapp **1.10.13** (modern,
`.venv-da110`) and **1.9.13** (legacy, `.venv-da19`), with four test workers;
its coverage check verified passing tests from each family. This is the
requested three-file gate, not a claim of a full runtime-suite run.

Development runs of the same targeted command initially had 6 passed / 4 failed
and then 8 passed / 2 failed. The first corrected an assertion that compared
trace-local identity occurrence/ordinal values instead of raw screen identity.
The second exposed an out-of-scope capture edge: recording Status immediately
following an activated walk re-expands the saved outcome's `auto_trace` with no
seek target; replay then reports `trace step 5 has no automatic seek target`.
The final precedence test uses Refresh (a new execution observation) instead.
This edge is not repaired or covered as supported behavior by this ticket, and
is evidence that capture provenance is an adapter concern, not a second pure
walk interpreter.

The new legacy case characterizes successful, non-fresh default submissions and
retained mutations, not every malformed schema-4 mixture. Fresh markerless
initialization, orphan post-walk markers, contradictory target/fresh flags, and
legacy submission-before-divergence side effects are not newly characterized.
Markerless legacy entries still lack the recorder path's pre-submission guard
and transaction isolation by design; no equivalence between the two paths is
claimed. No golden update, production refactor, or schema/session change was
made.
