# Demo interview validation plan

## Goal

Exercise the simulator against every demo interview in
`/Users/jack/Projects/docassemble-yaml/lsp/tests/fixtures/examples` and turn the
results into a repeatable compatibility gate. The gate should distinguish a
simulator defect from an interview that intentionally errors, requires a real
server, or depends on an external service.

## Scope and current inventory

- Primary corpus: **966** YAML files under `fixtures/examples`.
- Also run `fixtures/demo_package` as a small package/include smoke test, but do
  not count its three YAML files as part of the 966-file corpus.
- Exclude `reference_package` (reference-resolution fixtures) and `regressions`
  (including intentionally invalid files) from the demo gate. They can retain
  their own targeted tests.
- The examples directory is a flattened corpus assembled from the installed
  `docassemble.base` and `docassemble.demo` example packages. In the current
  environment, 965 files are byte-identical to an installed example; the
  remaining file, `stage-one.yml`, uses an older `user_info().package`
  expression. Two names exist in both upstream packages, so filename alone is
  not sufficient provenance.
- Many examples reference package-relative includes, templates, static files,
  Python modules, or external/server facilities. Copying only the YAML files
  into a synthetic package would therefore produce misleading failures.

## Success criteria

For every corpus file:

1. The runner records exactly one result and never hangs.
2. Compilation either succeeds or matches an explicitly reviewed expectation.
3. A fresh `start` returns a valid simulator JSON envelope and a recognized
   typed outcome, or matches an explicitly reviewed capability/dependency
   expectation.
4. Exit code 3, malformed JSON, process crashes, state leakage, and timeouts are
   always failures; they may not be added to the expectation list.
5. Unexpected failures include enough information to reproduce them with one
   command.
6. A curated representative subset additionally verifies answering,
   persistence, refresh, attachments, background actions, and completion. A
   successful first screen alone is not treated as proof of end-to-end flow
   fidelity.

## Exploratory findings

The first fresh-start sweep is exploratory, not a green baseline. The following
failures were confirmed as simulator gaps and fixed with focused canaries:

- declared interview objects named `user` were shadowed by the authentication
  dictionary;
- `sections.yml`, `sections-horizontal.yml`, and `sections-auto-open.yml` need
  the simulated request method;
- `device.yml` and `device-ip.yml` need deterministic request metadata;
- package-local `path_and_mimetype()` lookups need the base-package fallback and
  URL metadata; and
- documented `Individual` spouse convenience methods are absent from the
  installed base runtime even though the relationship demo uses them.

The following remain deliberate boundaries or environment-dependent cases and
must not be hidden by broad expectations: generated PDF conversion and PDF
field/signature operations, browser/session/authentication behavior, network or
OAuth services, database-backed services, machine-learning integrations,
optional Python dependencies, LibreOffice, and interviews that intentionally
raise an error or contain an infinite loop. `DAFile` image creation still needs
an explicit local numbered-file storage adapter before its demos can be called
supported. Incomplete interviews that reference undefined variables should stay
visible as authored-flow failures rather than being treated as simulator
successes.

The current corpus reports should therefore be read case-by-case: a nonzero
exploratory count is not evidence by itself that all cases are simulator bugs,
and missing shards/timeouts are not a completed baseline.

## Proposed implementation

### 1. Add a corpus runner

Create `scripts/test-demo-corpus` plus a small Python module under `tests/` or
`scripts/` that:

- accepts `DASIMULATOR_DEMO_FIXTURES` (defaulting to the sibling checkout path
  above), an interpreter, per-interview timeout, shard index/count, and output
  directory;
- discovers the 966 files rather than maintaining a hand-written filename
  list;
- records the fixture repository commit, fixture SHA-256, Python version, and
  installed `docassemble.base`, `docassemble.webapp`, and `docassemble.demo`
  versions in the report;
- supports `--compile`, `--start`, `--match`, and `--shard N/M` modes;
- writes machine-readable JSON Lines for individual cases and a compact JSON
  and Markdown summary grouped by outcome/error kind;
- prints a copy-paste reproduction command for each unexpected result.

Run each interview in its own subprocess. This is required because the corpus
contains infinite-loop and deliberate-error examples, and because docassemble
keeps process-global caches and hooks. Apply a hard timeout and terminate the
whole process group on expiry.

### 2. Reconstruct faithful package context

Before execution, build a temporary, read-only test workspace from the runtime
installed in the selected interpreter:

1. Copy or hard-link the installed `docassemble/base` and `docassemble/demo`
   packages, including their `data/questions`, `data/templates`, `data/static`,
   `data/sources`, and Python modules.
2. Map each fixture back to its source package by an explicit generated
   provenance manifest. Use installed filename membership and hashes to propose
   the mapping, then review ambiguous names once and commit the decision.
3. Overlay the fixture YAML at its canonical
   `data/questions/examples/<name>.yml` location without modifying the source
   checkout or interpreter.
4. Fail early with a clear "corpus/runtime drift" result when required package
   assets or versions cannot be reconciled. Do not report drift as a simulator
   failure.

This preserves canonical identities such as
`docassemble.base:data/questions/examples/foo.yml`, package-relative includes,
and resource lookup. Add unit tests for provenance conflicts, the
`stage-one.yml` mismatch, missing resources, and ambiguous filenames.

### 3. Run two all-corpus passes

**Compile pass**

Invoke the simulator's `check --interview ... --json` behavior once per
interview. Require successful compilation unless the reviewed expectation
manifest says otherwise. Do not use one bulk `check`: per-case subprocesses
provide isolation, timeout enforcement, and precise reporting.

**Fresh-start pass**

Invoke:

```sh
docassemble-simulator --root <staged-root> --interview <canonical-id> \
  --json --seek-diagnostics off start
```

Give every case a clean `.simulator` directory and temporary HOME. Default
background actions to disabled for the broad safety pass; run foreground
background behavior only in the curated suite. Run in a network-denied sandbox
or container so examples cannot send email, call APIs, or reach databases.
Classify results as:

- supported success (screen, continuation, or completion);
- expected deliberate interview error;
- expected simulator capability boundary (browser-only behavior, upload,
  external service/database, PDF-only output, authentication/session server
  behavior, and similar documented limits);
- unexpected compile/execution/seek failure;
- simulator fault, crash, malformed protocol, or timeout.

The first run should be exploratory and must not immediately become a green
baseline. Review each non-success cluster, fix simulator defects, and add only
well-understood deliberate/capability outcomes to the expectation manifest.
Every expectation needs a narrow match on interview, phase, exit code, error
kind, and stable message fragment, plus a reason and tracking issue where
appropriate. Do not use unrestricted xfails.

### 4. Add representative interaction contracts

Select at least one interview per simulator-supported behavior discovered in
`questions --json`, including:

- text, number, date, yes/no, choice, checkbox, object, and validation fields;
- multi-screen answer/persistence/refresh behavior;
- variable seeking and unresolved-variable diagnostics;
- includes, modules, and package resources;
- foreground background actions;
- DOCX attachment generation and published local URI;
- completion and deliberate flow errors.

For these cases, commit deterministic answer scripts and semantic assertions
against normalized result fields. Avoid snapshots of entire screen payloads.
Keep PDF, browser rendering, uploads, and real external services classified as
staging/deployment boundaries rather than pretending the simulator validates
those behaviors.

### 5. Integrate without slowing the fast suite

- Unit-test discovery, staging, matching, timeout handling, and report
  generation in the normal `uv run pytest -q` suite using a tiny fake corpus.
- Add a manual/nightly real-runtime lane for all 966 compile and start cases.
  Shard it by a stable hash of canonical interview identity, not list position.
- Add a smaller pull-request canary set chosen by feature coverage and prior
  failures.
- Upload JSONL/Markdown reports and unexpected-case logs as CI artifacts.
- Pin or record the fixture checkout and docassemble runtime versions so a
  dependency/corpus update is reviewed separately from simulator code changes.

## Delivery sequence

1. Implement discovery, provenance mapping, faithful staging, and unit tests.
2. Implement isolated compile execution, timeout/process cleanup, and reports.
3. Run and review the 966-file compile baseline; fix true simulator defects.
4. Implement fresh-start execution and sandboxing; review failure clusters.
5. Commit narrow expectations for deliberate and unsupported cases.
6. Add representative deterministic interaction contracts.
7. Add PR canaries and the sharded nightly/manual corpus lane.
8. Document how to update the corpus/runtime pins and triage a new failure.

## Initial commands to validate the runner

```sh
# Tiny smoke run while developing the runner
scripts/test-demo-corpus \
  --fixtures /Users/jack/Projects/docassemble-yaml/lsp/tests/fixtures \
  --match 'yesno.yml|fields.yml|attachment-simple.yml' \
  --compile --start

# Full local run
scripts/test-demo-corpus \
  --fixtures /Users/jack/Projects/docassemble-yaml/lsp/tests/fixtures \
  --compile --start --output .simulator/demo-corpus-results
```

The exact CLI can change during implementation, but the per-case isolation,
faithful package resources, explicit expectations, and structured reports are
required parts of the gate.
