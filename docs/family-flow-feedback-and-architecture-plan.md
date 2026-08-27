# Family-flow feedback and architecture deepening plan

Status: implemented
Date: 2026-08-26
Depends on: ADR-0001, ADR-0002, ADR-0003

## Implementation verification

Verified against the retained Family workflow session in
`docassemble-automatedpleading` after implementation:

- `check`: 33 interviews, 0 failures;
- refresh reached the saved final question with no raw seek output on stderr;
- 20 current-operation seek stages were reported as structured diagnostics;
- 14 published DOCX attachments were reported with existing `file://` links;
- no final link contained `href="None"`; and
- strict structural checks attributed nested paragraphs to 7 of the 14 logical
  attachments (the earlier count of 11 included repeated/intermediate files).

The repository suite passes with 122 tests, including a real-runtime generated
attachment and cross-process refresh regression.

Post-review amendments (2026-08-27):

- **Seek-trace switch:** `[simulator] seek_diagnostics = "capture"|"off"` (and
  `--seek-diagnostics` on every command) disables structured trace capture for
  completion-check-only runs; lazy-seek log noise stays off in both modes.
- **Render-seam kind:** render preparation now lifts docassemble's exhausted-
  seek `failure_kind` to the outer failure kind, so `render` reports
  `unresolved-variable` exactly like `start`/`seek`.
- **Bug report:** exact-observations-only report written at
  `docs/bug-report-nested-paragraphs.md`; not filed upstream or in the target
  package tracker.

## Follow-up streams

1. **Root-scoped runtime adapter** (architecture finding 03; recorded in
   ADR-0005): extract the
   remaining process-global installation state in `bootstrap.py` (`_PREPARED`,
   `_BACKGROUND_ACTION_MODE`, `_BACKGROUND_INSTALLED`,
   `_DIAGNOSTIC_LOGGING_INSTALLED`, `_ATTACHMENT_FALLBACK_INSTALLED`,
   `_STUBBED`) into one package-private runtime module owning installation,
   root activation, and runtime policy, per “Selected module shape §4”.
2. **Triage the nested-paragraph bug report** in the target package's tracker
   once the report's reproduction is confirmed there.

## Goal

Turn the successful Family interview run into a reliable simulator contract:
variable seeking is visible without being misreported as failure, generated DOCX
attachments have usable local references and honest structural diagnostics, and
the implementation gains the locality identified by the architecture review.

The current baseline is healthy: `111 passed, 5 warnings`; the reported target
run compiled 33 interviews with no failures, selected six non-PDF Family
documents, reached Workflow Complete, and generated 14 logical DOCX
attachments. This plan preserves that behavior.

## Decisions

1. **A variable-seek diagnostic is not an error.** `NameError` and Jinja
   `UndefinedError` are control signals used by docassemble while it recursively
   seeks a definition. The simulator will expose the ordered variable/question
   trace as diagnostics and will not print those expected signals as errors.
2. **An exhausted seek is an error.** Within variable resolution, the terminal
   failure is that docassemble sought a variable and found no question or code
   capable of defining it. That failure will identify the unresolved variable
   and retain the seek trace that led to it.
3. **Local attachments get local references, not fake server references.** A
   generated DOCX has a durable simulator path and a `file://` URI. The
   simulator will never invent an HTTP server URL. Final screen links and the
   structured artifact manifest will refer to the same file.
4. **Published attachments and rendering files are different facts.** The 14
   documents exposed by the completed workflow are published attachments; the
   22 files written while rendering are storage activity. Reports will count
   published attachments rather than presenting every numbered file as a
   logical result.
5. **DOCX structure is diagnosed before it is changed.** Nested `<w:p>` output
   must first be attributed to authored source, an included template, the stock
   docassemble attachment path, or simulator-specific rendering. The simulator
   will not silently rewrite authored templates or apply a broad XML repair.
6. **PDF conversion remains unsupported.** ADR-0001 stands: generated DOCX is
   supported, generated PDF conversion is omitted, and no external converter is
   invoked. This feedback requires regression coverage and clear completion
   output, not a PDF implementation.
7. **The accepted execution/render interface remains.**
   `InterviewExecution.run(operation) -> Outcome` and
   `InterviewRenderer.render(request) -> Outcome` remain the external seams.
   The work below deepens their implementations and does not expose working
   state or reopen ADR-0002.

## Finding disposition

| Finding | Disposition | Planned result |
|---|---|---|
| 178 non-fatal `NameError`/`UndefinedError` messages | Confirmed classification and presentation defect | Structured `variable-seek` diagnostics; no `error:` prefix, failure outcome, non-zero exit, or raw stderr for successful seeking |
| Only “sought but could not be defined” should fail | Confirmed semantic rule | One typed unresolved-variable failure with the sought variable and accumulated trace |
| Final links contain `href="None"` | Confirmed runtime-resource defect | Durable local file registry, `file://` URI hook, and artifact manifest |
| 11 DOCX files contain nested `<w:p>` | Confirmed evidence; source not yet established | Characterization first; fix the responsible implementation or report an attributed structural warning if stock docassemble produces the same output |
| No PDFs generated | Expected capability constraint | Keep ADR-0001 and prove completion output advertises only formats that exist |
| Screen facts are re-derived in three modules | Strong architecture finding | One screen-description module supplies live field facts to description, answering, validation, catalog presentation, and tests |
| Configuration has several owners | Strong architecture finding | One resolved-configuration interface owns layering, normalization, validation, redaction, reporting, and effective handoff |
| Bootstrap has process-global resource state | Architecture finding directly related to links/artifacts | One root-scoped simulator runtime module owns installation, local files, URL policy, logging dispatch, PDF policy, and foreground tasks |
| Seven execution paths repeat lifecycle setup | Architecture finding directly related to diagnostics | One private lifecycle spine owns compile/load/context/prepare/capture/commit policy |
| Runtime preflight and catalog discovery share `detect.py` | Architecture finding | Separate Interview catalog identity discovery from runtime import/acquisition |

## Selected module shape

### 1. Screen-description module

Deepen `describe.py` rather than add a second screen model beside it. Its
interface should produce one immutable description of the active screen and its
live field facts from the compiled question, namespace, and docassemble result.
The facts include:

- screen kind and question identity;
- decoded saved-variable name and safe ID;
- visibility and requiredness;
- datatype and browser-shaped coercion information;
- resolved choices and object-selection keys; and
- dynamic text context, including the currently sought variable.

Execution will consume those same field facts when applying and validating an
answer. `_field_types()` and the requiredness/visibility re-reading in
`execution.py` will be deleted. The Interview catalog will use the same field
identity decoder but will remain read-only compiled metadata; it will not
pretend static inspection has a live namespace.

This is the first architecture slice because it gives one source of screen
truth before diagnostics or lifecycle code start attaching more information to
outcomes.

### 2. Diagnostic vocabulary and presentation

Add a package-private typed diagnostic value, separate from `Failure`, with a
shape such as:

```python
Diagnostic(
    kind="variable-seek",
    message="seeking M.children[0].name",
    details={
        "variable": "M.children[0].name",
        "question": "children_name",
        "reason": "considering",
    },
)
```

Use `InterviewStatus.seeking` as the source of truth. It already records sought
variables, candidate questions, and why blocks were considered, run, or asked.
Do not infer success or failure from the words `NameError` or `UndefinedError`,
and do not maintain a second exception-derived seek algorithm.

`Outcome` will carry diagnostics independently from `result` and `error`.
The CLI envelope may add an optional top-level `diagnostics` list; this is an
additive JSON change. Human output will use a `diagnostics:`/`variable seeking:`
heading on stdout, never `error:`. The full ordered trace remains available;
if human output is compacted, it must report the total and how to request the
full trace rather than silently applying the current 40-entry cutoff.

Install one docassemble log dispatcher at runtime installation. While an
execution operation is active, known duplicate messages of the forms
`NameError exception during document assembly: ...` and
`UndefinedError exception during document assembly: ...` are routed away from
raw stderr because their structured seek stages are already captured. Other
runtime messages continue to normal logging. Use operation-local context (for
example, a `ContextVar`) so concurrent operations do not share diagnostics.

When docassemble raises its terminal missing-variable type, normalize it to a
new `unresolved-variable` error kind (exit 2), with:

- the exact sought variable;
- docassemble's user-facing message;
- the ordered seek diagnostics; and
- no duplicate raw exception lines.

A plain authored source error, validation failure, render failure, or simulator
fault remains its existing error kind. Only expected lazy-seeking signals are
reclassified.

### 3. Private execution lifecycle spine

Keep the typed operation variants, but consolidate their repeated implementation
inside `execution.py`. The private spine owns:

1. lock and source policy (fresh, saved, snapshot, or read-only);
2. interview compilation;
3. namespace creation/load and rehydration;
4. docassemble thread context;
5. operation-local diagnostic capture;
6. the operation action;
7. terminal error normalization; and
8. commit/no-commit policy.

`Start`, `Answer`, `Refresh`, `Seek`, `Evaluate`, `Variables`, `Execute`, and
render preparation should supply only operation-specific behavior and policy.
Read-only operations still have no commit path, failed answers still roll back,
and active seek still commits only when requested. The private callback-scoped
render seam remains private.

This is a refactor under existing interfaces, not a new public lifecycle seam.
Tests should cross `run(operation)` and assert the operation matrix rather than
calling the private spine.

### 4. Root-scoped simulator runtime and local artifact registry

Replace bootstrap's `_PREPARED`, `_STUBBED`, `_SIMULATOR_FILES`, numeric counter,
and related first-call ownership with one concrete, package-private runtime
module. Do not introduce a public protocol merely for substitution. The module
owns:

- one-time process hook installation;
- root/config activation for each operation;
- fake Redis and foreground background-action adapters;
- the docassemble log dispatcher;
- PDF omission policy;
- local numbered-file storage; and
- a durable file index under `.simulator/files/`.

The durable index is needed because a `DAFile` number can survive in a saved
session while individual CLI commands run in new processes. Index updates and
number allocation use the existing atomic write/lock primitives.

The runtime's `url_finder` hook will:

- recognize simulator-owned `DAFile`, `DAFileList`, and `DAFileCollection`
  values;
- resolve their number through the durable index;
- return `Path.resolve().as_uri()` for an existing local artifact;
- honor the selected filename without claiming temporary/server access
  semantics; and
- return no result for references the simulator does not own, allowing the
  normal hook chain to continue.

Requesting a local URL marks that numbered file as published. The operation
outcome includes an artifact manifest for newly published files with logical
filename, format, MIME type, local path, URI, and structural diagnostics. This
makes the final HTML and machine-readable result agree and prevents internal
render writes from inflating the logical attachment count.

Do not delete unreferenced numbered files in the first slice. Safe retention
requires a reachability rule across saved sessions and foreground task values;
it can follow after the durable registry is proven.

### 5. DOCX structural validation

Add a pure ZIP/XML structural check for published DOCX files. At minimum it
must inspect all relevant WordprocessingML parts and report a paragraph nested
inside another paragraph with part name and element location. It must not
rewrite the package.

Before choosing a fix, reproduce one failing Family artifact and compare:

1. authored top-level and included templates before rendering;
2. output from docassemble's normal `Question.finalize_attachment()` path with
   only simulator storage/PDF hooks active;
3. output from `InterviewRenderer.render()`; and
4. output from the same pinned docassemble version in a real deployment or a
   minimal server-equivalent harness.

Disposition after that comparison:

- **Simulator render divergence:** fix `render_template()` at the include/render
  pass that introduces the nesting and add a byte-level regression. Do not add
  a generic post-render XML repair.
- **Simulator runtime-hook divergence:** fix the local runtime adapter at the
  responsible seam.
- **Authored template defect:** report the source/include identity and leave the
  authored file unchanged.
- **Stock docassemble output:** report an artifact warning that strict OOXML
  validation fails even though tolerant readers open it; file an upstream or
  package issue with the smallest reproduction rather than claiming simulator
  corruption.

Default behavior should publish a readable DOCX with a warning. A strict
artifact-validation mode may promote structural warnings to a failed operation,
but it must be explicit and use a typed render/artifact failure. Add that policy
through the resolved configuration module rather than another bootstrap global.

### 6. One configuration owner

Move deep merge out of `bootstrap.py` and replace CLI-side merging/report
reconstruction with one interface in `config.py`, for example:

```python
resolve_configuration(root, override_path=None, command_overrides=None)
    -> ResolvedConfiguration
```

The resolved value owns ordered sources, normalized simulator settings,
pass-through docassemble settings, validation, effective-config path, redacted
reporting, and writing the effective YAML handoff. CLI adapters consume its
report; bootstrap/runtime consumes its handoff. Neither reconstructs
precedence. This slice also provides the proper owner for diagnostic verbosity
and DOCX structural-validation policy if either becomes configurable.

### 7. Catalog discovery and runtime preflight

Let the Interview catalog own package/interview identity listing and
resolution. Move import probing, lock/pin inspection, and acquisition into a
runtime-preflight module selected only by the CLI composition root. Keep `uv`
and `pip` as the two acquisition adapters at that internal seam. Catalog
inspection must not know installer policy, and runtime preflight must not know
question metadata or session state.

## Work plan

### 0. Capture the reported behavior

- Save a redacted Family-flow command transcript and artifact inventory in the
  target package or as an opt-in real-runtime fixture; do not copy client data
  into this repository.
- Record the 178 messages by operation and identify which are docassemble
  logger output versus simulator outcomes.
- Retain one failing DOCX whose content is non-sensitive, or build the smallest
  synthetic include/template reproduction.
- Add an opt-in Family acceptance runner so future changes can verify the 33/0
  compile result, six non-PDF selections, Workflow Complete, and published
  attachment inventory without making this package depend on the Family
  interview.

### 1. Concentrate screen semantics

- Add characterization tests for field identity, choices, visibility,
  requiredness, date coercion, object selections, and dynamic sought-variable
  context.
- Introduce the single screen-description/facts interface.
- Convert description, assignment, validation, and catalog field identity to
  consume it.
- Delete `_field_types()` and duplicate field-rule paths.
- Run the fast and synthetic real-runtime suites before changing diagnostics.

### 2. Add the lifecycle spine and diagnostic channel

- Pin every operation's load/lock/prepare/commit policy through
  `InterviewExecution.run()` tests.
- Extract the private lifecycle spine without changing outcomes.
- Add typed diagnostics to shared outcomes and CLI presentation.
- Collect the full `InterviewStatus.seeking` trace for assembly and explicit
  seek operations.
- Route duplicate docassemble lazy-seek log lines away from stderr while an
  operation collector is active.
- Normalize terminal missing-variable exceptions to
  `unresolved-variable`; preserve all other failure classifications.

Required focused scenarios:

- `NameError` seeks a variable, finds a question, and returns a successful
  screen with diagnostics and exit 0;
- Jinja `UndefinedError` during attachment evaluation seeks and resolves a
  variable without producing an error;
- a nested seek chain shows variables and candidate/running/asking stages in
  order;
- a variable with no definition returns one unresolved-variable failure, exit
  2, and the trace;
- JSON remains parseable and successful commands write no raw seek messages to
  stderr; and
- diagnostics from concurrent operations and separate roots do not mix.

### 3. Introduce runtime-owned local artifacts and URLs

- Extract the concrete runtime module and separate process installation from
  root activation.
- Persist numbered-file metadata and allocation atomically under the active
  root.
- Implement local file lookup and `url_finder` with `file://` URIs.
- Mark URL-referenced files as published and return the operation artifact
  manifest.
- Add cross-process tests that generate in one command, reload the saved
  session in another, resolve the same file number, and preserve the same path
  and URI.
- Assert final generated HTML contains no `href="None"`, every published URI
  exists, PDF-only entries are absent, and internal file count does not alter
  published attachment count.

### 4. Characterize and address nested DOCX paragraphs

- Add the non-mutating OOXML structural checker.
- Run the four-way provenance comparison above.
- Apply only the disposition supported by evidence.
- Add strict and warning presentation tests, include-template attribution, and
  an assertion that source DOCX bytes never change.
- Re-run the Family artifacts through both tolerant read and strict structural
  validation, recording any remaining upstream/authored warnings explicitly.

### 5. Give configuration one owner

- Add `ResolvedConfiguration` contract tests for every layer and command-line
  override.
- Move merge, normalization, validation, redaction, reporting, and effective
  YAML writing behind that interface.
- Remove config reconstruction from CLI and config policy from bootstrap.
- Preserve current discovered paths, defaults, redaction, help text, and
  zero-config behavior.

### 6. Separate discovery from preflight

- Move identity/list/resolve behavior into the Interview catalog module.
- Move runtime imports, acquisition, and installer command construction into
  preflight.
- Inject acquisition adapters in focused tests; keep execution unaware of
  installation.
- Verify `info` and identity discovery require no docassemble import, while
  compile/execution commands request preflight explicitly.

### 7. Documentation, domain language, and gates

- Add **variable-seek diagnostic**, **unresolved-variable failure**, and
  **published attachment** to `CONTEXT.md`; distinguish them from a saved
  session and an explicitly rendered artifact.
- Record the diagnostic-vs-failure decision in an ADR because it changes the
  stable machine-readable semantics and is easy to misinterpret later.
- Amend ADR-0001 only as needed to state that local DOCX `file://` references
  are real local artifacts, not fabricated PDF/server URLs.
- Update README examples for diagnostics, unresolved-variable output, artifact
  manifests, local URIs, structural warnings, and the no-PDF constraint.
- Run `uv run pytest -q`, the synthetic real-runtime lane, the opt-in Family
  flow, Ruff, type checking, build, and `git diff --check` after each slice.
- Re-run the two-axis code review and architecture review at the end; compare
  specifically against the five opportunities that originated this plan.

## Implementation order

1. Reported-behavior evidence and red tests.
2. Screen-description deepening.
3. Execution lifecycle spine.
4. Structured variable-seek diagnostics and terminal failure normalization.
5. Root-scoped runtime, durable local files, local URIs, and artifact manifest.
6. DOCX provenance check and evidence-driven correction/warning.
7. Resolved configuration owner.
8. Catalog/preflight separation.
9. Domain/ADR/README updates and full gates.

The first releasable milestone is steps 1–4: it directly fixes the misleading
178 “errors.” The second is steps 5–6: it fixes completion artifacts and makes
DOCX fidelity honest. Steps 7–8 complete the remaining high-value architecture
work without delaying the user-visible fixes.

## Acceptance criteria

1. A successful lazy-seek chain may contain any number of variable-seek
   diagnostics but has `ok: true`, exits 0, and emits no raw
   `NameError`/`UndefinedError` lines to stderr.
2. Each diagnostic identifies at least the sought variable and, where supplied
   by docassemble, the candidate question and reason in original order.
3. A sought variable that cannot be defined produces exactly one
   `unresolved-variable` failure with exit 2 and the complete trace. Resolved
   intermediate seeks never become failures.
4. Screen description, answer coercion, and requiredness/visibility validation
   consume one source of live field facts; execution no longer re-derives field
   types.
5. Every published DOCX attachment has an existing local path and non-`None`
   `file://` URI. Final HTML, JSON manifest, and durable numbered-file lookup
   agree across CLI processes.
6. Published attachment counts reflect logical download results, not all local
   rendering writes. The Family run reports its 14 logical DOCX attachments
   without advertising skipped PDFs.
7. Every published DOCX receives a structural result. Simulator-caused nested
   paragraphs are fixed at their origin; authored or stock-docassemble nesting
   is attributed and warned without mutating source files.
8. No generated PDF exists, no PDF link is advertised, and no converter process
   runs.
9. `run(operation)` and `render(request)` remain the external execution/render
   interfaces; working state and internal callbacks never escape.
10. Configuration has one owner, runtime resources are root-scoped, and catalog
    discovery is independent of installer policy.
11. The 111-test baseline, added focused tests, synthetic real-runtime lane,
    opt-in Family flow, lint/type/build checks, and final reviews pass.

## Non-goals

- Running an HTTP file server or fabricating deployment URLs.
- PDF conversion, fake PDFs, LibreOffice installation, or PDF bundle testing.
- Silencing arbitrary docassemble logs or treating all `NameError` values as
  lazy-seek diagnostics.
- Repairing authored DOCX XML automatically after rendering.
- Deleting intermediate numbered files before a session/task reachability rule
  exists.
- Replacing docassemble's variable-seeking algorithm.
- Adding public storage, runtime, logging, or installer interfaces solely for
  tests.
