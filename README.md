# docassemble-simulator

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Run a docassemble interview locally without a server. The simulator uses the
real docassemble compiler, assembly/seek machinery, validation behavior, and
DOCX/Jinja renderer while providing small commands suitable for humans and
agents.

## Install

The simulator is not published to PyPI yet. Install it from the public GitHub
repository into the target package's interpreter.

For a uv-managed interview package:

```sh
# Run these in the interview package's project environment.
uv add --dev "git+https://github.com/jpagh/docassemble-simulator"
uv run sim check
```

The canonical command is also available as `docassemble-simulator`. For a
non-uv project, install it into the target package interpreter:

```sh
/path/to/package/.venv/bin/python -m pip install \
  "git+https://github.com/jpagh/docassemble-simulator"
/path/to/package/.venv/bin/sim check
```

Pin a release tag when reproducibility matters, for example
`git+https://github.com/jpagh/docassemble-simulator@v26.9.0`.

The simulator must run in the same environment as the target package; a global
installation is not a substitute. If `docassemble-base` or `docassemble-webapp` is absent, the CLI acquires the
missing fixed package names with `uv pip` (or the interpreter's `pip`) without
upgrading installed packages. Use `--offline` or `[simulator].offline = true`
to disable acquisition. An installed package that fails because of a native
library is reported as an import failure, not silently reinstalled.

The simulator tests two runtime families: legacy docassemble 1.9.x (thread-local
server interface) and modern 1.10.x (hook interface). Supported
AssemblyLine-backed Interviews are exercised as a tested matrix of
`docassemble-base`/`docassemble-webapp`/`docassemble.AssemblyLine`/
`docassemble.ALToolbox` versions; see
[docs/runtime-compatibility.md](docs/runtime-compatibility.md). The simulator
reproduces the docassemble webapp's startup module preload so installed custom
datatypes (for example ALToolbox `BirthDate`) register before parsing, then
compiles a minimal AssemblyLine birthdate-metadata probe before the target
Interview. An unsupported pair fails as `runtime-compatibility` with the
installed versions, failing capability, and recovery direction instead of a raw
parser error; the probe never installs, upgrades, or edits packages. The target
package interpreter is authoritative: a global `jda` or unrelated Python
installation is not a substitute. On macOS, docassemble's native dependencies
(such as zbar, commonly installed with Homebrew) must also be available to
that interpreter; native-library failures are reported rather than hidden.

## Command model

Run from a directory containing `docassemble/<package>/`, or pass `--root` and
optionally `--interview`.

```sh
docassemble-simulator info
docassemble-simulator check
docassemble-simulator questions
docassemble-simulator index --var M.family

docassemble-simulator start
docassemble-simulator answer M.name=Alice
docassemble-simulator status
docassemble-simulator refresh
docassemble-simulator eval 'M.name'
docassemble-simulator vars
docassemble-simulator exec 'M.items.append_object()'
docassemble-simulator seek M.family --activate
```

- `start` creates fresh state and assembles to a screen, completion, or flow
  error.
- `answer` submits the active saved screen like a browser form and commits
  the result as one transaction: submitted values are applied, visible fields
  with defaults take the default, visible optional fields left blank are
  defined with the value the browser would post (an empty string for text and
  dates, `0`/`0.0` for numeric fields, `None` for radio/maybe/object
  selections, the unchecked value for checkbox-style `yesno`/`noyes`, and an
  all-false dictionary for checkbox groups), so the same `sets:` question is
  not re-asked for each variable. Visible required fields left blank, or
  explicitly emptied (`field=` or `field=None`), reject the submission, and a
  required checkbox group needs at least one true selection.
  Hidden `show if` fields are neither assigned nor required, and signature
  fields become `DAEmpty()` so documents still render.
  Fields that are not on the active screen are rejected; use `exec` for
  deliberate state surgery. `--partial` restores per-field submission
  (missing required fields become warnings and lazy re-seeking is allowed),
  and `--no-validate` skips only the interview's validation code, not the
  required gate. Assignment or validation failure discards every submitted
  value.
- `status` only reads the saved outcome. `refresh` explicitly rehydrates and
  reassembles saved state.
- `seek` starts from saved state by default. `--fresh` is isolated and
  `--activate` persists the sought screen as the active screen.
- Lazy variable seeking is reported as ordered `variable-seek` diagnostics,
  not as errors. A seek fails only when docassemble exhausts its definitions;
  that produces an `unresolved-variable` error containing the sought variable.
- `eval` and `vars` rehydrate the namespace but never save it.
- `exec` assembles and saves by default. `--no-assemble` performs a state-only
  commit after successful Python execution.

`answer` parses JSON, then Python literals, then plain text. Checkbox and object
choice fields use the active field metadata. An exact `YYYY-MM-DD` submitted to
a date field becomes docassemble's timezone-aware `DADateTime`; malformed and
impossible dates are rejected. The same text submitted to a text field remains
a string. `answer --code` and `exec` retain Python semantics and bypass browser
coercion; `--code` is still scoped to the active screen's fields.

State is versioned and stored separately for each canonical interview and
effective configuration under `.simulator/sessions/`. The effective
configuration is fingerprinted over its resolved values, so two runs share a
session exactly when their discovered files, `--config` overrides, and
command-line policy flags resolve to the same content; changing any of them
starts a separate session instead of silently rehydrating the old one.
`config --json` reports this as `config_fingerprint`. Loading state saved under
a different configuration fails with a message saying so, and the previous
session stays on disk for the configuration that produced it. Files are
trusted-local pickle payloads. Unsupported or stale state fails with an
instruction to run `start` again. Mutations hold a per-interview advisory lock
and commit with atomic replacement.

## Screen traces

Execution commands can record the screens a run passes through and compare a
later run against that recording. The trace is an append-only JSONL sidecar;
saved sessions are untouched.

```sh
docassemble-simulator start --record run.trace.jsonl --phase intake
docassemble-simulator answer user_name=Alice --record run.trace.jsonl --phase intake
docassemble-simulator trace compare golden.trace.jsonl run.trace.jsonl
docassemble-simulator trace compare golden.trace.jsonl run.trace.jsonl \
  --order phased --phases intake,documents,download
docassemble-simulator trace compare golden.trace.jsonl run.trace.jsonl --update
```

`--record PATH` is accepted by `start`, `refresh`, `answer`, `seek`, and
`exec`, and takes an optional `--phase NAME` used by phased comparison. Each
invocation appends one record: sequence, operation, phase, submitted
assignments, outcome, the screen outcome, and a canonical **screen identity**
with the rule that produced it. The first line is metadata (interview
definition, config fingerprint, docassemble/AssemblyLine/simulator versions,
and the trace schema version); appending to a trace whose metadata disagrees
with the current run is an input error. A failed answer is recorded against
the unchanged active screen with the rejected assignments, so validation
re-asks appear as repeated occurrences of one identity. A failure with no
active screen is recorded as a failure instead: the entry keeps its error
payload and carries an `error:` identity (`error:unresolved:<variable>` for an
undefined reference), never a fabricated screen.

Identity ignores generated `Question_<n>` names, resolved occurrence paths,
random instance names, and rendered text. It falls back from explicit block
ids through targeted variables, generic-object anchors, and indexed list
targets to a field-variable tuple or a kind/category. The rule used is stored
with every key.

`trace compare` reports coverage counts (expected, actual, matched, missing,
extra, duplicates) in every mode, so a tolerant policy cannot hide collapsing
coverage. `--order unordered` treats the run as a multiset; `--order phased`
requires declared phases in order while tolerating order within each phase.
The declaration comes from `--phases` when given, else from the golden's
recorded `phase_order`, else from the order the golden's own entries were
recorded in — so a golden recorded with `--phase` compares in phased mode
without repeating the list.
`--missing allow` and `--extra allow` relax the default strict policies. Field
facts are compared separately from identity; `--full-text` adds normalized
question and subquestion text. Known differences belong in a reviewed
exceptions TOML passed with `--exceptions` (each entry names the interview,
phase, identity, category, and reason; order violations and simulator faults
cannot be excused). `--update` rewrites the expected golden from the actual
trace and is the only write path. A mismatch exits `2` with the full report in
the `trace-mismatch` error details.

The identity rules and the evidence behind them are documented in
[docs/screen-trace-identity-study.md](docs/screen-trace-identity-study.md).

## JSON and exit codes

Every command accepts `--json` and returns one envelope. Successful variable
seeking may add a top-level `diagnostics` list, and generated download results
may add a top-level `attachments` manifest:

```json
{"ok": true, "command": "answer", "result": {}}
```

```json
{
  "ok": false,
  "command": "answer",
  "error": {"kind": "validation", "message": "...", "details": {}}
}
```

Exit codes are `0` for success (including interview completion and resolved
lazy seeks), `1` for usage, workspace, configuration, or missing-state
failures, `2` for validation, unresolved-variable, execution, seek, compile,
runtime-compatibility, or render failures, and `3` for unexpected simulator
faults. Argument-parsing
failures follow the same envelope when `--json` is present and exit `1`; normal
`--help` output remains a successful exit.

## Rendering

```sh
docassemble-simulator render form.docx
docassemble-simulator render form.docx --fresh
docassemble-simulator render form.docx --snapshot state.pkl
docassemble-simulator render form.docx --fixture fixture.py
docassemble-simulator render form.docx --no-assemble
docassemble-simulator render form.docx --save-snapshot state.pkl
docassemble-simulator render form.docx --output artifacts/form.docx
```

A render request has orthogonal concerns:

- **source:** saved session (default), `--fresh`, `--snapshot PATH`, or
  `--fixture PATH`; choose exactly one;
- **assembly:** enabled for saved, fresh, and snapshot sources unless
  `--no-assemble`; fixtures never assemble;
- **snapshot effect:** `--save-snapshot PATH` writes prepared state atomically
  before template evaluation;
- **artifact effect:** `--output PATH` names the exact DOCX target and is written
  atomically only after successful evaluation and expectations;
- **expectation:** `--expect-missing VARIABLE` makes that strict undefined error
  the expected result.

Rendering never writes, replaces, or deletes a saved session. Fresh rendering is
ephemeral. Snapshot payloads are versioned and interview-specific. Snapshot and
artifact destinations cannot point into saved-session storage or authored
`data/templates` trees, alias a render input, or alias one another in the same
request. Writes use unique sibling temporary files and per-destination locks, so
concurrent writers install complete files with final-writer-wins behavior.

Render and include passes run while execution keeps the docassemble thread
context active. Structural DOCX failures report the requested template, error
type, and available paragraph/line; an included filename is reported only when
the runtime preserves it reliably. Source and included templates are never
rewritten or repaired.

DOCX output is supported. Generated interview attachments are stored under
`.simulator/files/`; completion links use real local `file://` URIs and JSON
includes only files actually published as download results. Intermediate
numbered files are not counted as published attachments. Published DOCX files
are checked for nested WordprocessingML paragraphs and any strict-structure
finding appears on the attachment as a non-mutating diagnostic.

Generated PDF conversion remains intentionally stubbed: the simulator does not
invoke an external converter or fabricate a PDF. When an AssemblyLine document
or bundle asks for a generated PDF and has a DOCX rendering, the simulator
skips the PDF conversion, returns the real DOCX artifact (including merged DOCX
and DOCX ZIP bundles), and records a `pdf-skip` diagnostic. PDF-only
attachments still raise `PDFConversionUnavailable`.

## Workspace configuration and defaults

Runtime files are entirely ignored under `.simulator/` (sessions, generated
YAML, and render output). Authored files live under `.config/simulator/`:

```text
.config/simulator/config.toml       # committed declarative settings
.config/simulator/config.local.toml # optional, ignored override
.config/simulator/config.py         # optional authored seed code
.config/simulator/fixture.py         # optional fixture for explicit --fixture
.simulator/config-effective.yml     # generated TOML-to-YAML handoff
.simulator/sessions/                # trusted-local pickle state
```

A fixture is used only when selected explicitly with `--fixture`. A
zero-config run uses an in-process fake Redis, simulator-local file storage,
`debug=true`, localhost, `en_US`, `US`, the local timezone (falling back to
`America/New_York`), and foreground background actions. No server database is
substituted: the simulator's own state lives in `.simulator/sessions/`, and
database-backed AssemblyLine session features are disabled by default rather
than pointed at a nonexistent PostgreSQL. These are local substitutes, not
PostgreSQL, Redis, Celery, or server storage. DOCX rendering is supported;
generated PDFs are skipped in favor of the DOCX artifact and no external
converter is invoked.

| Default | Behavior | Category |
| --- | --- | --- |
| server database | not substituted; `.simulator/sessions/` holds CLI state | capability boundary |
| AssemblyLine session metadata | `update session metadata: false`; PostgreSQL-backed upsert off | pass-through default |
| Redis | in-process `FakeRedis` | stub |
| File storage | files under `.simulator/files/` | local filesystem |
| `debug`, host, locale, country | `true`, `localhost`, `en_US`, `US` | pass-through defaults |
| timezone | local timezone, then `America/New_York` | pass-through default |
| background actions | foreground, no Celery worker | stub |
| seek diagnostics | `capture` (default), structured `variable-seek` trace | local capture |
| DOCX/PDF | DOCX supported; generated PDFs skipped in favor of DOCX, no converter invoked | capability boundary |

Configuration is merged from lowest to highest precedence: the global
`$DOCASSEMBLE_SIMULATOR_CONFIG` (or
`$XDG_CONFIG_HOME/docassemble-simulator/config.toml`), then every matching
project file while walking from the filesystem root to the package root. At a
level, local files win. Supported project candidates are:

```text
simulator.local.toml  .simulator.local.toml  simulator.toml  .simulator.toml
simulator/config.toml .simulator/config.toml .config/simulator.toml
.config/simulator/config.local.toml .config/simulator/config.toml
```

The effective file is generated at `.simulator/config-effective.yml`; TOML
`jinja-data` is normalized to docassemble's `jinja data`. `--config PATH` adds
a final YAML (or TOML) override. Sessions are isolated per effective
configuration, so runs under different overrides never share state; the
`config` command's `--json` output includes the `config_fingerprint` that
scopes them. The `config` command prints redacted values and supports
`--json`; credentials are never printed or committed.

Simulator-owned settings belong under `[simulator]`, including
`missing_runtime = "install"|"disabled"`, `background_actions =
"foreground"|"disabled"`, `seek_diagnostics = "capture"|"off"`, `offline`,
and `render_bindings` (the legacy top-level `[render-bindings]` table is also
accepted). Capture is on by default; `seek_diagnostics = "off"` (or
`--seek-diagnostics off` on any command) suppresses the structured seeking
trace and skips forcing the interview's debug trace for runs whose only goal
is to verify the interview completes end to end. Lazy-seek log noise stays off
stdout/stderr in both modes. Docassemble pass-through
settings such as `timezone`, `jinja data`, and supported database
or Redis values are visible to interview code but do not change simulator
policy. A package's `config.py` remains optional seed code and is not needed
for the standard local substitutes.

Example:

```toml
# .config/simulator/config.toml
timezone = "America/Chicago"
[jinja-data]
my_label = "Family"
[simulator]
background_actions = "foreground"
seek_diagnostics = "capture"
[simulator.render_bindings]
x = "clients[0]"
# Alternatively, the legacy top-level spelling is [render-bindings].

# Opt back in to AssemblyLine's PostgreSQL-backed session metadata when this
# project configures a real database. See docs/runtime-compatibility.md.
["assembly line"]
"update session metadata" = true
```

Use `render form.docx --bind x=clients[1]` to override a configured binding.
A template-specific table such as `[render-bindings."poa.docx"]` overrides the
default binding map for that template. Command-line bindings win over
template-specific config, which wins over the default binding map. Bindings
are simple names evaluated in an ephemeral render namespace and never saved.

Optional `config.py` executes after namespace rehydration and before authored
flow, evaluation, or render work. Use it for deterministic seed data and
server-scoped objects, not to emulate external services.

The simulator generically calls the compiled interview's non-pickleable
population mechanism on every prepared namespace. This restores
`docassemble.base.util` exports and declared `modules:`/`imports:` entries for
saved, fresh, snapshot, fixture, and no-assemble paths. Standard helpers such as
`currency`, `redact`, and date/number/text helpers do not belong in a package
seed. Functions outside standard utility exports must still be declared by the
interview.

Top-level docassemble object roots are registered for reference resolution after
rehydration and seed execution. Packages should still give server-scoped seeded
objects stable `instanceName` values and seed all data their templates consume.

## Fidelity and deliberate limits

The simulator preserves interview compilation, mandatory assembly, seeking,
screen descriptions, answer coercion, validation order, marking, seed timing,
docassemble context, template evaluation, include passes, and intentional PDF
policy. Supported `background_action()` events run immediately in the current
foreground context by default; this does not simulate worker isolation, queue
latency, retries, or process failures. `background_actions = "disabled"` retains
a waiting/stub task for diagnosis. The simulator substitutes no SQL database at
all: AssemblyLine's PostgreSQL-backed session metadata, saved answer sets, and
interview list are disabled by default and remain deployment-only unless the
project configures a real database and opts back in. Browser HTML, uploads,
external services, PostgreSQL, real Redis/Celery behavior, and PDF/download
verification remain deployment or staging responsibilities.

## Development

Run the complete suite with:

```sh
mise run test
```

For fast local feedback, run the tests without real runtimes or the external
corpus:

```sh
mise run test:fast
```

The slower tests are split into explicit lanes. `test:runtime` runs the
real-docassemble contract tests, while `test:corpus` runs the isolated external
example corpus. The default `test` task still runs every test.

Real-docassemble fidelity is covered by a separate lane that runs the CLI in a
target package's interpreter (which supplies `docassemble` and `python-docx`):

```sh
scripts/test-real-runtime /path/to/target/package/.venv/bin/python
```

Pass a second interpreter to also run the cross-family contract against
docassemble 1.9.x:

```sh
scripts/test-real-runtime /path/to/1.10/package/.venv/bin/python \
  /path/to/1.9/package/.venv/bin/python
```

The repo can provision both target interpreters itself from the `da19` /
`da110` dependency groups in `pyproject.toml`, which also pin the tested
`docassemble.AssemblyLine` / `docassemble.ALToolbox` pair for each family
(see [docs/runtime-compatibility.md](docs/runtime-compatibility.md)):

```sh
mise run test:all-da
```

This syncs isolated `.venv-da19` (docassemble 1.9.x) and `.venv-da110`
(1.10.x) environments, including the AssemblyLine compatibility fixture and
target smoke path, and runs the suite once with both lanes wired up
(`DASIMULATOR_REAL_PYTHON*` pointing at those interpreters), so every
cross-family contract test exercises the modern and legacy runtimes. Use
`mise run sync:da19` / `mise run sync:da110` to (re)provision one family
without running the suite.

There is no CI runner for the 1.9.x lane yet: `mise run test:all-da` is the
manual regression gate that must pass before changes to the runtime
compatibility layer merge (issue #1, story 23).

The real-runtime tests use the current pytest interpreter when the runtime
is installed; set `DASIMULATOR_REAL_PYTHON` to use a separate target
environment and `DASIMULATOR_REAL_PYTHON_19` for the 1.9.x lane. Without
either runtime, they skip; a configured target that
cannot run the tests fails clearly. The fast suite's stubbed runtime tests
never require `docassemble`. Generated-PDF download screens are satisfied with
DOCX artifacts locally; real PDF validation remains a deployment concern, and
the simulator gate is DOCX artifact rendering and content inspection.

The sibling docassemble-yaml example corpus has an isolated compile/start lane:

```sh
scripts/test-demo-corpus \
  --fixtures /path/to/docassemble-yaml/lsp/tests/fixtures \
  --python /path/to/package/.venv/bin/python \
  --compile --start --output .simulator/demo-corpus-results
```

The runner stages `docassemble.base` and `docassemble.demo`, overlays all 966
`examples/*.yml` fixtures, runs each phase in a subprocess with a timeout and a
network-denial sandbox, and writes `results.jsonl`, `summary.json`, and
`summary.md`. Reviewed canonical provenance is checked from
`tests/demo_corpus_provenance.toml`; regenerate it with
`--write-provenance-manifest PATH` when the fixture/runtime pair is intentionally
updated. Missing optional NLTK corpora remain a dependency boundary by default;
use `--prepare-runtime-data` explicitly when provisioning them is intended.
Prepared data is reused in a versioned cache; use `--nltk-cache-dir PATH` to
choose its root or `--refresh-nltk-cache` to publish a new generation. Use
`--jobs N` for bounded parallel case execution (serial by default),
`--match`, `--shard INDEX/COUNT`, and
`--expectations tests/demo_corpus_expectations.toml` for focused or reviewed
runs. Set `DASIMULATOR_DEMO_FIXTURES` for the default fixture root and
`DASIMULATOR_DEMO_PYTHON` for the interpreter used by the script itself.

## License

MIT. See [LICENSE](LICENSE).
