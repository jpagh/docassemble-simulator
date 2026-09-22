# Runtime-fidelity follow-up plan

Status: implemented.
Date: 2026-08-26

## Purpose

Close the simulator gaps found while running the Georgia Estate Planning
Interview in a private AssemblyLine target package, while preserving the
boundary between behavior the simulator can provide locally and behavior that
requires a real docassemble deployment.

The target package has restored its AssemblyLine dependency; this plan
assumes the target pins a compatible `docassemble-base`/AssemblyLine/webapp
set. The simulator should still make missing core runtime dependencies easy to
recover locally, without silently changing an installed or explicitly pinned
set.

## Decisions and boundaries

- The simulator remains a DOCX runner. It will not invoke LibreOffice or any
  other PDF converter.
- The simulator will provide a default foreground implementation of
  `background_action()` for local runs. It will not start Celery or require a
  broker.
- The simulator will automatically acquire missing `docassemble-base` and
  `docassemble-webapp` packages when the current interpreter cannot import
  them. It uses an active project lock/pin when one exists and otherwise asks
  for the latest available version. It will not upgrade packages that are
  already installed, and explicit target dependency pins remain authoritative.
- SQLite, fake Redis, local file storage, debug settings, and other server
  substitutes have simulator-owned defaults. Configuration is for overrides and
  package-specific data, not mandatory boilerplate.
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

Status: resolved by issue #6. The root cause was simulator runtime setup, not a
docassemble parser change or an unsupported package pair: the webapp's startup
module preload (which registers `CustomDataType` classes such as ALToolbox's
`BirthDate`) was missing, so `alMonthLabel` was parsed as a second label. The
simulator now reproduces that preload pass and compiles an in-memory birthdate
metadata probe through the real compiler before the target Interview; an
unsupported pair fails as a structured `runtime-compatibility` result naming
the runtime family, versions, failing capability, and recovery direction. The
tested matrix, probe, and failure taxonomy are documented in
[docs/runtime-compatibility.md](runtime-compatibility.md) and exercised by
`tests/test_real_runtime.py` via `mise run test:all-da`. No `site-packages`
edits, parser forks, or metadata stripping are used.

The target entrypoint includes:

```yaml
include:
  - docassemble.AssemblyLine:assembly_line.yml
```

and the target dependency configuration must explicitly install AssemblyLine.
The accidental omission has been corrected. Assume the target now pins a tested
compatible version set before evaluating simulator behavior.

Then test a version matrix containing the selected `docassemble-base`,
AssemblyLine, webapp, and Python versions. The `ql_baseline.yml` entries
`alMonthLabel`, `alDayLabel`, and `alYearLabel` must be tested from the installed
package without editing `site-packages`. The known parser failure is consistent with the historical docassemble
breaking release that AssemblyLine had not yet adapted to. Verify the selected
versions against the current AssemblyLine release; pin a compatible pair or
report the upstream incompatibility with the smallest reproduction. Do not
carry local dependency edits as a workaround.

The target should prefer compatible pins/constraints for tightly coupled
`docassemble-*` packages over independent open-ended minimums. Add a clean
installation smoke test that imports AssemblyLine and runs simulator `check`.

### B. Missing runtime dependency acquisition and simulator defaults

Status: the acquisition half is implemented and tested. The default-substitution
half is resolved differently from the original plan: docassemble's server
database layer supports only PostgreSQL/MySQL/Oracle and AssemblyLine's session
metadata writes are PostgreSQL-only, so the simulator substitutes no database
at all instead of a broken SQLite default. `assembly line.update session
metadata` defaults to `false` and is overridable with a real `db` configuration
(see [runtime-compatibility.md](runtime-compatibility.md)). The plan text below
is kept for the acquisition contract and the remaining configuration rules.

The simulator package intentionally does not declare docassemble as a normal
Python dependency because it must run inside the target interpreter. When that
interpreter lacks a core package, however, the failure should be recoverable.

Plan:

1. Extend the preflight import check to distinguish missing
   `docassemble-base`/`docassemble-webapp` from an import failure caused by an
   installed package or native library.
2. If either core package is absent, invoke the available package installer for
   the current interpreter (`uv pip` first, then a clearly reported `pip`
   fallback) with fixed package names. Use the active project lock/pin when
   available; otherwise request the latest versions. Never install based on an
   import name supplied by the interview, never upgrade an installed package by
   default, and never overwrite an explicit project lock.
3. Re-run the imports after installation and report the exact command and
   actionable failure if acquisition is unavailable or unsuccessful. Provide an
   opt-out for offline/CI use.
4. Keep this behavior at the CLI/bootstrap composition boundary; the execution
   and render modules must not know how packages are installed.
5. Test missing-base, missing-webapp, already-installed, installer-failure,
   offline opt-out, and native-library/import-error cases with a fake installer.

Move the current bootstrap defaults into an explicit simulator-defaults model
(or equivalent private constants) and test that a package works without a
configuration file using:

- SQLite for server database settings;
- an in-process fake Redis connection;
- simulator-local file storage and generated effective configuration;
- default debug/host/locale/country/timezone behavior; and
- foreground background actions.

User configuration remains a deep-merge override. Define and document two
categories of settings:

- **Simulator settings**, under a reserved simulator configuration table:
  missing-runtime installation policy, background-action mode, render bindings,
  and any future local-service modes.
- **Docassemble/server-compatible settings**, passed through to the effective
  configuration: `timezone`, `jinja data`, and other explicitly supported
  values such as database/Redis configuration.

The documentation must state which settings alter simulator behavior and which
only affect the configuration visible to interview code. In particular, the
default fake Redis, SQLite/session stubs, local file storage, and foreground
worker behavior must not be mistaken for real PostgreSQL, Redis, Celery, or
server storage. Configuration is not required for those defaults; it is only
required for package-specific seeds, Jinja data, credentials, or a changed
service policy.

### C. Configuration reference and CLI discoverability

The README and `docs/workspace-layout-and-fidelity.md` must contain one
canonical configuration reference with:

- all discovery locations and precedence, including global, parent, project,
  and local override files;
- the generated effective-config path and TOML-to-YAML normalization;
- the complete default table and whether each default is an in-process stub,
  local filesystem behavior, or a pass-through value;
- simulator-owned settings and accepted values;
- docassemble-compatible pass-through settings;
- command-line/config precedence for render bindings and runtime modes;
- secret handling and the rule that credentials are never printed or committed;
- examples for a zero-config run and a package with `config.toml` plus
  `config.py`.

The CLI must make this discoverable without reading source code:

1. Expand global `--help` and relevant command help to describe config
   discovery, defaults, `--config`, and the no-worker/no-PDF boundaries.
2. Extend `info` with non-secret config diagnostics: discovered config files in
   precedence order, effective-config path, active simulator modes, and a
   concise defaults/capabilities summary. Never emit passwords, API keys,
   tokens, or raw credential-bearing values.
3. Add a config inspection option or command that prints the effective,
   redacted simulator settings and pass-through keys, with an explicit
   `--json` form for automation. It must distinguish defaults from overrides.
4. Add tests for help text, zero-config `info`, config precedence, redaction,
   and JSON/human config inspection.

Do not make a package's `config.py` necessary for standard local service
substitutes. Keep it for package-specific seed data and behavior.

### D. Structured compile failures

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

### E. Foreground background-action fallback

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

### F. PDF capability messaging

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

### G. Explicit generic-object template bindings

A direct template render cannot infer which object should be bound to `x`.
That is an underspecified request, but the simulator can make the workflow
usable without requiring ad-hoc Python fixtures.

Plan:

1. Extend the render request with repeated private bindings such as:
   `--bind x=clients[0]`.
2. Accept the same bindings from simulator config, for example a
   `render-bindings` table keyed by template or a default binding map. For
   example, `[render-bindings] x = "clients[0]"` supplies a default and
   `[render-bindings."poa.docx"] x = "clients[1]"` supplies a template-specific
   value. Define precedence explicitly: command-line bindings override
   template-specific config, which overrides defaults; neither changes the
   package's interview namespace.
3. Parse the left side as a simple name and evaluate the right side only after
   source preparation, rehydration, and authored seed execution.
4. Apply bindings only to the ephemeral render namespace, immediately before
   template evaluation. Do not write them into saved state or a snapshot unless
   a future option explicitly requests that behavior.
5. Support saved, fresh, and snapshot sources; fixture rendering remains an
   alternative for fully custom namespaces.
6. Report invalid expressions and missing roots as input/render errors with the
   binding name and expression.
7. Document the generic-object example and explain that `x` is an explicit
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

- Runtime acquisition/defaults tests with a fake installer and no config file.
- Configuration reference, precedence, redaction, help, and inspection tests.
- Catalog tests for docassemble `DAError` conversion and JSON envelopes.
- Background-action unit and synthetic-runtime tests.
- DOCX capability/help and limitation-message tests.
- Explicit generic-object binding tests.
- Existing simulator suite, formatting, linting, type checking, and diff checks.

### Real target runtime

After the target's compatible AssemblyLine dependency pins are restored:

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
2. A target interpreter missing core runtime packages can acquire them by
   default, while installed/pinned packages are not silently upgraded.
3. `check --json` converts missing includes and parser errors into structured
   compile failures rather than tracebacks.
4. Supported AssemblyLine background document generation completes in the
   foreground by default, without Celery or a package-specific workaround.
5. README and CLI output clearly state the configuration defaults and that
   DOCX is supported but generated PDF
   conversion and PDF downloads are deployment-only.
6. Direct generic-object rendering works with an explicit `--bind` value or
   config binding and never mutates saved session state through that binding.
7. The automated suite and real-runtime target smoke tests pass, while the
   deployment-only matrix remains explicitly identified rather than silently
   substituted.

## Out of scope

- Implementing PostgreSQL, Redis, Celery, Stripe, browser, or PDF-converter
  emulators.
- Claiming server-level fidelity for asynchronous scheduling or external
  effects.
- Inferring generic-object bindings from template names or attachment metadata.
- Editing installed dependencies as part of simulator execution.
