# Runtime-fidelity follow-up plan

Status: proposal
Date: 2026-08-26

## Purpose

Close the simulator gaps found while running the Georgia Estate Planning
Interview in `/Users/jack/Lemma/docassemble-walkup`, while preserving the
boundary between behavior the simulator can provide locally and behavior that
requires a real docassemble deployment.

The target package's accidental removal of `docassemble.AssemblyLine` from its
local dependency configuration is a prerequisite fix in that package, not a
simulator feature. The target must restore that dependency and use a tested,
compatible version pair with `docassemble-base`.

## Decisions and boundaries

- The simulator remains a DOCX runner. It will not invoke LibreOffice or any
  other PDF converter.
- The simulator will provide a default foreground implementation of
  `background_action()` for local runs. It will not start Celery or require a
  broker.
- Foreground background work is a local convenience, not a claim of server
  parity. It may eliminate waiting/reload screens and does not reproduce worker
  isolation, queue latency, retries, or process-level failures.
- PostgreSQL, Stripe, browser behavior, uploads, and external services remain
  deployment/integration-test responsibilities.
- Generic-object templates will support explicit bindings; the simulator will
  not guess which object a template's `x` represents.
- No public storage/runtime adapter or package-specific public hook is added.

## Findings to address

### A. Target dependency and parser compatibility

The target entrypoint includes:

```yaml
include:
  - docassemble.AssemblyLine:assembly_line.yml
```

but its current local dependency file must explicitly install AssemblyLine.
A clean target environment currently reports that the included interview cannot
be found. Restore the dependency before evaluating parser compatibility.

Then test a version matrix containing the selected `docassemble-base`,
AssemblyLine, webapp, and Python versions. The `ql_baseline.yml` entries
`alMonthLabel`, `alDayLabel`, and `alYearLabel` must be tested from the installed
package without editing `site-packages`. If the parser rejects a released
AssemblyLine package, pin a compatible pair or report the upstream
incompatibility with the smallest reproduction. Do not carry local dependency
edits as a workaround.

The target should prefer compatible pins/constraints for tightly coupled
`docassemble-*` packages over independent open-ended minimums. Add a clean
installation smoke test that imports AssemblyLine and runs simulator `check`.

### B. Structured compile failures

`InterviewCatalog.check()` and `_compile_for_inspection()` currently catch a
hand-maintained list of built-in exceptions but not docassemble's `DAError`
family. A missing include can therefore escape as a traceback even when the
caller supplied `--json`.

Plan:

1. Add a private catalog compilation-error boundary that includes the runtime's
   docassemble source/compile error type without making docassemble a package
   dependency of the simulator.
2. Use it for `check`, `questions`, and `index`.
3. Preserve compile error kind, human-readable message, JSON envelope, and
   exit-code contracts.
4. Add tests for missing includes, parser/source errors, selected-interview
   errors, and `--json` output. The output must be structured and must not
   contain a traceback on stdout.

### C. Foreground background-action fallback

AssemblyLine's document bundle starts work with `background_action()` and then
checks a task's readiness before exposing downloads. The local simulator has
no broker or worker, so package seeds should not have to replace this behavior
just to make ordinary document generation testable.

Plan:

1. Identify the actual docassemble runtime seams used by supported background
   actions: `background_action`, task readiness/result/failure inspection,
   `background_response`, `background_response_action`, and action arguments.
2. Implement a private simulator task value whose common task interface is
   immediately complete after running the requested action in the current
   execution context.
3. Support the event-name/callable forms used by AssemblyLine. Preserve the
   current docassemble context, namespace, session transaction, and artifact
   paths while the action runs.
4. If an action relies on unsupported worker-only behavior, fail clearly with a
   simulator limitation error rather than returning a falsely successful task.
5. Make foreground execution the default. Retain an explicit disabled/stub mode
   for diagnosis and packages that intentionally provide their own substitute;
   document the mode and precedence in README/configuration docs.
6. Ensure a task is not executed repeatedly on refresh or repeated assembly.
   Persist enough local task/result state for a subsequent request to observe
   completion, while documenting that queue retries and worker isolation are not
   simulated.
7. Update the architecture-refactor plan's current statement that the
   simulator never executes background work; record the new boundary in an ADR
   if implementation introduces a lasting lifecycle policy.

Tests:

- a synthetic interview whose event is invoked through `background_action`;
- successful foreground completion and result readiness;
- action failure and clear error propagation;
- repeated refresh does not duplicate the action;
- AssemblyLine-style document generation reaches the completed task without a
  package `config.py` workaround;
- explicit disabled mode retains the waiting/stub behavior.

### D. PDF capability messaging

The simulator must continue to omit PDF conversion, but the user-facing
experience should explain that limitation instead of surfacing a raw missing
`.pdf` lookup from a PDF-only download helper.

Plan:

1. Add a documented capability report to `info` (and relevant `render` help):
   DOCX supported; generated PDF conversion unavailable; no external converter
   is invoked.
2. Document the boundary in README and
   `docs/workspace-layout-and-fidelity.md`, including the distinction between
   rendered DOCX validation and production PDF/download verification.
3. Detect or translate the supported PDF-download/conversion failure path into
   a stable, actionable simulator limitation message that names PDF output and
   points to a real docassemble deployment. Do not fabricate a PDF or claim a
   download exists.
4. Add CLI/JSON and human-output tests for the capability and limitation
   message. Preserve successful DOCX rendering and PDF-only omission tests.
5. Update the target's runtime test instructions so its PDF download screen is
   explicitly deferred to staging rather than treated as a simulator pass.

### E. Explicit generic-object template bindings

A direct template render cannot infer which object should be bound to `x`.
That is an underspecified request, but the simulator can make the workflow
usable without requiring ad-hoc Python fixtures.

Plan:

1. Extend the render request with repeated private bindings such as:
   `--bind x=clients[0]`.
2. Parse the left side as a simple name and evaluate the right side only after
   source preparation, rehydration, and authored seed execution.
3. Apply bindings only to the ephemeral render namespace, immediately before
   template evaluation. Do not write them into saved state or a snapshot unless
   a future option explicitly requests that behavior.
4. Support saved, fresh, and snapshot sources; fixture rendering remains an
   alternative for fully custom namespaces.
5. Report invalid expressions and missing roots as input/render errors with the
   binding name and expression.
6. Document the generic-object example and explain that `x` is an explicit
   template context variable, not an inferable object identity.

Tests:

- `render template.docx --bind x=clients[0]` evaluates a generic-object
  template;
- a second binding can select `clients[1]`;
- invalid bindings fail before artifact publication;
- bindings do not alter session bytes or saved snapshots;
- existing fixture and ordinary template renders remain unchanged.

## Verification matrix

### Fast/local

- Catalog tests for docassemble `DAError` conversion and JSON envelopes.
- Background-action unit and synthetic-runtime tests.
- DOCX capability/help and limitation-message tests.
- Explicit generic-object binding tests.
- Existing simulator suite, formatting, linting, type checking, and diff checks.

### Real target runtime

After restoring and pinning AssemblyLine in the target package:

- clean environment install;
- simulator `info`, `check --json`, and question/index commands;
- single and married interview flows with payment bypass;
- foreground AssemblyLine document generation without a package background seed;
- DOCX artifacts and template content inspection;
- explicit generic-object render bindings.

### Deployment/staging only

- PostgreSQL metadata persistence and rollback/error behavior;
- real Celery success, retry, timeout, stale, and worker-failure behavior;
- Stripe test-mode Checkout redirects, cancellation, duplicate callbacks,
  expired/unpaid sessions, and mismatched catalog data;
- external DOCX-to-PDF conversion, PDF bundles, email/download controls;
- browser navigation, JavaScript, uploads, and visual PDF inspection.

## Acceptance criteria

1. A clean target install has AssemblyLine available and a compatible parser;
   no `site-packages` edits are required.
2. `check --json` converts missing includes and parser errors into structured
   compile failures rather than tracebacks.
3. Supported AssemblyLine background document generation completes in the
   foreground by default, without Celery or a package-specific workaround.
4. README and CLI output clearly state that DOCX is supported but generated PDF
   conversion and PDF downloads are deployment-only.
5. Direct generic-object rendering works with an explicit `--bind` value and
   never mutates saved session state through that binding.
6. The automated suite and real-runtime target smoke tests pass, while the
   deployment-only matrix remains explicitly identified rather than silently
   substituted.

## Out of scope

- Implementing PostgreSQL, Redis, Celery, Stripe, browser, or PDF-converter
  emulators.
- Claiming server-level fidelity for asynchronous scheduling or external
  effects.
- Inferring generic-object bindings from template names or attachment metadata.
- Editing installed dependencies as part of simulator execution.
