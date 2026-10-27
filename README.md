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

`why VARIABLE` lists variable-resolution candidates in the same order the real
runtime will try them, including explicit ordering directives, applicable
language fallback, catalog provenance, and authored guards. With a saved session,
guards are evaluated after read-only execution preparation, so rehydrated helpers
and docassemble objects behave normally; the operation never saves the prepared
state. Without a saved session it reports guard text without fabricating guard
values. It marks the runtime winner when it can and reports resolution
conflicts; `--strict` makes a conflict non-zero. Saved-state reports label each
candidate `live`, `inactive`, or `unknown`. Unknown guard errors are handled
conservatively: overlapping unknown/live candidates conflict without claiming a
winner, while definitely inactive or provably disjoint candidates do not. The
original guard error remains in the candidate report for diagnosis. A terminal
unresolved-variable error includes the responsible block provenance, the
expression that ultimately referenced the sought variable, and that variable;
attribution comes from the ordered runtime seek history rather than a stale
previously displayed screen.
Expression matching includes the complete attribute, key, or index path; when
that precision is not retained, the expression is reported as unknown rather
than inferred from a shared root.
Tolerated lazy references remain diagnostics without a culprit. With seek
capture disabled, the simulator does not fabricate attribution when the target
runtime does not retain the ordered history.

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
optionally `--interview`. `--version` prints the installed simulator version
without touching the runtime, and `info --json` reports the same value as
`simulator_version` beside the detected docassemble runtime.

```sh
docassemble-simulator info
docassemble-simulator check
docassemble-simulator questions
docassemble-simulator index --var M.family
docassemble-simulator why M.family --strict

docassemble-simulator start
docassemble-simulator answer M.name=Alice
docassemble-simulator answer --defaults
docassemble-simulator seek M.family --auto
docassemble-simulator status
docassemble-simulator refresh
docassemble-simulator reset
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
  not re-asked for each variable. A submitted `checkboxes`/`multiselect`
  dictionary is completed the same way: a browser posts every rendered choice,
  so an omitted choice is defined `False` rather than left undefined. A
  review's continue button is described as an answerable `True` submission,
  and `--defaults` presses it. Visible required fields left blank, or
  explicitly emptied (`field=` or `field=None`), reject the submission, except
  that a nonblank `None` or `null` token explicitly selecting an offered
  null-valued radio choice is answered and stores `None`. A required checkbox
  group needs at least one true selection. When native resolved choices suppress
  an input, authored requiredness remains in the screen description but does not
  demand an impossible answer (`rendered_input: false`). An undefined absent
  `object`/`object_radio` selection becomes `None`; an existing value is preserved.
  Browser constraints do not apply to an absent input, but authored validation
  still runs and may reject it (for example, “You must select at least one Trust”).
  No `--partial` or relaxed strictness is needed. Missing normalized choices alone
  do not establish absence; comboboxes and datalists still offer inputs.
  Hidden `show if` fields are neither assigned nor required, and signature
  fields become `DAEmpty()` so documents still render.
  Fields that are not on the active screen are rejected; use `exec` for
  deliberate state surgery. `--defaults` submits the active screen without
  explicit assignments, using visible defaults and browser-blank values;
  required fields without defaults are reported instead of being guessed.
  `--partial` restores per-field submission
  (missing required fields become warnings and lazy re-seeking is allowed),
  and `--no-validate` skips only the interview's validation code, not the
  required gate. Assignment or validation failure discards every submitted
  value.
- `status` only reads the saved outcome. `refresh` explicitly rehydrates and
  reassembles saved state. `reset` discards every saved session for the
  selected interview, including any session stored under a different effective
  configuration. It needs no importable interview, never assembles, and is the
  one-shot escape when a saved session cannot be loaded under the current
  configuration.
- `seek` starts from saved state by default. `--fresh` is isolated and
  `--activate` persists the sought screen as the active screen. `--auto`
  submits each screen that can be completed from defaults/blanks as its own
  browser-shaped transaction, reports the ordered accepted screens, and stops
  immediately when the sought variable resolves, the interview completes, or
  real input/validation is needed. A rejected auto-submission rolls back its
  defaults, synthesized blanks, and validation mutations; earlier accepted
  screens remain committed in the operation-local walk. Non-activated and
  fresh seeks do not save the walk.
- Lazy variable seeking is reported as ordered `variable-seek` diagnostics,
  not as errors. A seek fails only when docassemble exhausts its definitions;
  that produces an `unresolved-variable` error containing the sought variable.
- `eval` and `vars` rehydrate the namespace but never save it.
- `exec` assembles and saves by default. `--no-assemble` performs a state-only
  commit after successful Python execution.

Table/review screens expose `row_actions`, an array of rendered controls such as
`{"kind": "add", "target": "people"}` and
`{"kind": "delete", "target": "people", "row": 0}` (zero-based rows).
`kind` is an open string vocabulary; add, delete, and edit are supported.
Press them with `row-action add people`, `row-action delete people --row 0`,
or `row-action edit people --row 0`. Edit presents the row's authored screen.
Nested targets such as `children[0].grandchildren` use the same controls.
The operation accepts only controls on the saved screen, replays docassemble's
rendered action dispatch, and saves the resulting screen only on success. The worker accepts
`{"operation": "row-action", "kind": "delete", "target": "people", "row": 0}`;
Python callers use `runner.row_action("delete", "people", row=0)`.
These additive screen facts are debug-only in screen traces, not field-fact
comparison inputs; older traces remain readable.

### Native screen actions

Screens also expose `actions`: links captured from rendered question/subquestion
text and visible note/HTML fields, including inline and reference-style Markdown
links interpreted by docassemble's configured Markdown engine. Each control has an opaque `id`, `label`,
`source`, and either its native `payload` or an `unsupported` reason.

```sh
docassemble-simulator status --json  # inspect result.actions
docassemble-simulator action ACTION_ID --record run.trace.jsonl
```

The worker accepts `{"operation":"action","action_id":"ACTION_ID"}`;
Python callers use `runner.action(action_id)`. Choose an identifier from the
current saved screen, not a URL or an action name. Identifiers are deterministic
for the same rendered screen and distinguish creation parameters and source
locations; they are not permanent interview-wide identifiers. Old saved screens
without captured note content need `refresh` before those links can be pressed.
Likewise, refresh screens saved by earlier builds to regenerate their action
metadata and identifiers; `status` only reads the previously saved outcome.

`action` dispatches the complete native payload through docassemble, including
`url_ask()`'s `_da_force_ask` wrapper and ordered parameter-setting/add-item
sequences. It does not submit unanswered fields or run form validation. A
successful resulting screen is saved atomically; errors leave the saved session
unchanged (external side effects of authored code are not rolled back). Missing
identifiers, JavaScript, external/cross-interview links, extra URL navigation
parameters, and malformed URLs are rejected explicitly. Only local native action
links are supported, not arbitrary DOM/JavaScript interaction or CSS visibility.
`row-action` remains available for its existing list controls.

Do not evaluate a context-dependent button helper with `eval` to reconstruct a
link: evaluation has no active question-render context. Press the link already
rendered on the saved screen instead; no artificial `current_variable` is needed.

`answer` parses JSON, then Python literals, then plain text. Its JSON result
reports each assignment as `applied`, `unknown field`, or `parse error`, and
rejections include the active fields, closest-name suggestions, and missing
required fields. Unknown-field and parse-error rejections provide that same
missing-input guidance from valid assignments and rendered defaults without
running interview validation; all rejected values are discarded. The active
screen describes every field with a copy-pasteable
`example` (including checkbox mappings, object choices/groups, blanks, and
ISO dates); `answer --help` documents the same grammar. Checkbox and object
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
`config --json` reports this as `config_fingerprint` and, for every effective
top-level key, the contributing file/override layers and shadowed values.
Loading state saved under a different configuration reports the stored
fingerprint, config files/override, an exact resume command, and an exact
`reset` command for discarding it; the previous session stays on disk for the
configuration that produced it. This diagnostic
is shared by `status`, `vars`, `answer`, and `seek`. Files are trusted-local
pickle payloads. Unsupported or stale state fails with an instruction to run
`start` again. Mutations hold a per-interview advisory lock and commit with
atomic replacement.

`check` also reports duplicate explicit question ids with every block's
provenance. They are warnings by default; `check --strict` makes accidental
duplicates fail. A valid override/supersedes declaration is reported
separately with its participating blocks, but never blanket-exempts a duplicate
id group; unrelated or missing targets do not suppress the collision, and mixed
groups retain an accidental-collision report.

## Persistent journeys and the standard runner

A multi-step journey should use one persistent worker instead of launching one
process per operation:

```sh
printf '%s\\n' \\
  '{"id":"1","operation":"start"}' \\
  '{"id":"2","operation":"answer","assignments":{"user_name":"Alice"}}' \\
  '{"id":"3","operation":"status"}' \\
  '{"id":"4","operation":"shutdown"}' \\
  | sim --root ./my-project --interview docassemble.my_project:data/questions/main.yml run \\
      --timeout 30 --timings
```

`run` and `worker` accept JSON-lines on stdin or `--journey FILE` (a JSON array
is also accepted). Each response contains the request `id`, `ok`, the normal
simulator `result` or `error`, and the active project/interview/session scope.
`reset` discards every saved session for the worker's interview, including
sessions stored under another effective configuration; `shutdown` exits
cleanly.
`--session-root PATH` makes the session resumable by another worker. Without it,
the command uses a temporary isolated session. Interview/bootstrap output is
redirected to stderr so stdout remains protocol-only. Timings are optional and
remain in the protocol response.

While a worker is running, editing the interview's source takes effect on the
next operation. The worker checks the timestamp and size of every workspace
YAML source it watches — every `*.yml`/`*.yaml` under the workspace's
`docassemble/*/data/questions` trees, plus each file a compiled question came
from, so an abbreviated `pkg:file` include and a relative include nested under
another package are covered at the exact file docassemble read — together with
the declared `modules:`/`imports:` helper files, once per operation and, when
they changed, drops the compiled program, evicts the recompiled interview's
declared helpers from `sys.modules`, invalidates their cached bytecode, and
recompiles — so renaming a screen, editing an included YAML (including an
include-only routing file or a features-only document that contributes no
question), or editing a helper (with or without an interview-source edit) is
served without restarting the worker. Watching the whole questions tree
over-includes: an unrelated YAML edit in the same workspace merely causes a
reload, which is preferred to missing a source the interview could read. A
declared helper is watched when it resolves to a file or package under the
workspace source tree; helper edits to a declared package directory are watched
recursively, so an edited or newly added submodule counts. Only workspace
declared helpers are evicted, so an installed module named in `imports:` keeps
its identity. A declared module outside the workspace (stdlib or an installed
docassemble package) is not watched, and neither is a workspace module the
interview does not declare. Watching, eviction, and bytecode invalidation all
resolve real paths first, so a workspace symlink that escapes the resolved root
is not watched, evicted, or unlinked, a `__pycache__` symlink pointing outside
the root does not have its external cache deleted, and a YAML or include that
resolves outside the workspace root is not watched. An unchanged interview
stays
warm and is not recompiled. A reload never touches the Saved session; `reset`
remains the explicit way to clear it, and a reload that makes the interview
invalid reports the usual typed `compile` error with the session intact.
Configuration is resolved once at worker startup and is **not** reloaded, so a
configuration change still requires a restart. Templates are read at render
time, so a template edit is visible to the next render without a reload. See
ADR-0016. One-shot commands and any `cache_interview=False` caller compile
fresh on every operation and are unchanged.

`worker` (and its `run`/`journey` aliases) also accepts `--record PATH
--phase NAME` to append every execution response's screen outcome to one trace
sidecar, using the same adapter and format as the one-shot `--record` commands.
A request may override either field with its own `"record"` and `"phase"`
keys, so one journey can tag entries per step; an absent or `null` field uses
the worker's `--record`/`--phase` default, and a non-string value is an input
error. Recorded operations use the canonical one-shot spelling (`start`,
`status`, `refresh`, `answer`, `action`, `seek`, `eval`, `exec`, `vars`, `seed`), so an
alias request such as `"operation":"evaluate"` records what `eval` records.
Unlike the one-shot commands, which accept `--record` only on `start`,
`refresh`, `answer`, `action`, `seek`, and `exec`, the worker accepts a record for any
execution request, so a journey sidecar can also carry `status` responses and
`eval`/`vars`/`seed` failures recorded against the unchanged active screen.
A `status` record replays as a read-only screen observation; the rest, like a
one-shot `exec` record, are compare-only: `trace replay` reports the first
compare-only step as the typed `input` error `trace step N records compare-only
operation '<operation>'`.
Protocol-only requests (`ping`,
`reset`, `shutdown`, and malformed frames) are never recorded. A recording
failure is a typed `input` response (and is logged to stderr), never a silent
`ok`, so a truncated trace cannot pass unnoticed; when the operation itself
already failed, its response keeps that error and gains a typed
`recording-failure` diagnostic instead, so the recording failure never erases
what the operation did. A scenario whose
journey reaches only the one-shot-recordable commands is equivalent to
recording it one process per operation, including an automatic seek's expanded
`auto` steps, its trailing post-walk marker, and a failed answer recorded
against the unchanged active screen.

```sh
printf '%s\n' \
  '{"operation":"start","record":"run.trace.jsonl","phase":"intake"}' \
  '{"operation":"answer","assignments":{"user_name":"Alice"},"record":"run.trace.jsonl","phase":"intake"}' \
  '{"operation":"shutdown"}' \
  | sim --root ./my-project run --record run.trace.jsonl
docassemble-simulator trace compare golden.trace.jsonl run.trace.jsonl
```

A worker also serves `render`, so a warm journey can produce documents without
re-launching the interpreter:

```sh
printf '%s\n' \
  '{"operation":"render","template":"form.docx","source":"saved","no_assemble":true,"output":"artifact.docx"}' \
  '{"operation":"render","template":"form.docx","source":"fresh","no_assemble":true}' \
  '{"operation":"shutdown"}' \
  | sim --root ./my-project --interview docassemble.my_project:data/questions/main.yml run
```

The `render` request mirrors `sim render`: `template` (required), `source`
(`saved` by default, or `fresh`, `fixture` + `path`, `snapshot` + `path`),
`no_assemble`, `output`, `save_snapshot`, `expect_missing`, and `bindings` (an
object of `NAME: EXPRESSION`, or an array of `NAME=EXPRESSION` strings). The
per-request `timeout` applies as it does to every operation, and a `fixture`
source never assembles, exactly as the `--fixture` flag does. For the same
project and inputs, the response `result` is the `sim render --json` result
(same artifact path and shape), with `diagnostics` and `attachments`
forwarded. A malformed request or a render failure arrives as a typed
`input`/`render` error, never a raw exception or protocol noise. Rendering
reads the worker's saved session and never mutates it beyond what the
`sim render` source policy already does.

A worker also serves the read-only catalog and project inspection commands, so
a warm journey can list, check, and explain its interview without a fresh
process:

```sh
printf '%s\n' \
  '{"operation":"check"}' \
  '{"operation":"questions","var":"name"}' \
  '{"operation":"index"}' \
  '{"operation":"why","variable":"user_name"}' \
  '{"operation":"info"}' \
  '{"operation":"config"}' \
  '{"operation":"shutdown"}' \
  | sim --root ./my-project --interview docassemble.my_project:data/questions/main.yml run
```

`questions`, `index`, `check`, `why`, `info`, and `config` mirror the one-shot
commands: the response `result` is the `sim <command> --json` result for the
same project, and a failure is the same typed `error` kind. The listing and
`check` requests are scoped to the worker's interview (like `--interview`),
take the listing filter as `var`, and accept `check`'s `--strict` flag as
`strict`; `why` requires `variable` and also accepts `strict`. None of these
requests creates or mutates a Saved session: `check`/`questions`/`index` only
compile, `info`/`config` never touch session state, and `why` reads the
worker's saved namespace read-only through the existing explanation operation
— without a session it returns the same static `not saved` report as
`sim why`. A malformed request, a compile failure through `check`, or a
missing `why` variable arrives as the typed `input`/`compile` error the
one-shot command reports.

A worker also serves `trace compare` and `trace replay`, so an agent can hold
one warm worker for a record → change → replay loop:

```sh
printf '%s\n' \
  '{"operation":"trace-compare","expected":"golden.trace.jsonl","actual":"run.trace.jsonl"}' \
  '{"operation":"trace-compare","expected":"golden.trace.jsonl","actual":"run.trace.jsonl","order":"phased","phases":["intake","close"],"exceptions":"exceptions.toml"}' \
  '{"operation":"trace-replay","golden":"golden.trace.jsonl"}' \
  '{"operation":"trace-replay","golden":"golden.trace.jsonl","overrides":["2.user_name=Bob"]}' \
  '{"operation":"shutdown"}' \
  | sim --root ./my-project run
```

The `trace-compare` request mirrors `sim trace compare`: `expected` and
`actual` (required sidecar paths), `order` (`ordered` by default, or
`unordered`/`phased`), `missing`/`extra` (`strict` by default, or `allow`),
`full_text`, `phases` (an array or comma-separated string, only with
`order: phased` — as with `--phases`, declaring phases for another order is the
typed `input` error `sim` reports), and an optional `exceptions` TOML path. It
is read-only: `--update` is the CLI's one golden write path, so the worker has
no `--update` path, a request carrying `update` is the typed `input` error
`trace compare update is not a worker capability`, and the worker never writes
a golden. For the same sidecars and policy the response `result`
is the `sim trace compare --json` result, and a mismatch is the typed
`trace-mismatch` error whose `details` are that same report.

The `trace-replay` request mirrors `sim trace replay`: `golden` (required),
`overrides` (a `STEP.FIELD=VALUE` array, or a single `override` string),
`artifact_dir` (an optional retained attachment output directory), and an
optional `interview` (it defaults to the worker's own interview, so a golden
for another interview is rejected); `update` is rejected here too, because the
worker never writes a golden. The replay runs against its own temporary
session directory, so it never reads or writes the worker's Saved session and
never writes the golden; a divergent activated walk leaves no durable session
behind. The response `result` is the `sim trace replay --json` result
(`clean`, `steps`, `first_divergence`, `counts`), and a divergence is the typed
`trace-mismatch` error whose `details` are that same report. With `artifact_dir`,
that report also includes `attachments`; file lifetime and export failures are
[described below](#retaining-replay-attachments).

Replaying through a warm worker reuses the interview the journey already
compiled, because docassemble's compiled-interview cache is process-global and
the replay's short-lived execution reads it rather than recompiling. Before
each replay, and before every `questions`/`index`/`check` request, the worker
runs the same lazy source check its execution operations use (#47), so an edit
between two requests is recompiled rather than served stale; an unchanged
source stays warm. A request whose source no longer compiles pays that compile
once: the refresh's failure is reused by the request's own path, so the worker
reports exactly the envelope the one-shot command reports (a compile error for
`questions`/`index`, `check`'s per-interview rows, or a replay's divergence
report). Development measurements on the `da110` lane (a scratch
measurement of one recorded default/auto/validation scenario, not a repository
benchmark and not part of any gate) put a warm replay around 15 ms against
about 3 s for a fresh `sim trace replay` process whose cost is dominated by
interpreter startup and bootstrap, with the one-time compile for a worker that
has not compiled the interview yet around 0.1 s inside that first request.
Replay is still one execution per request: it does not reuse a
previous replay's working state.

The Python client uses the same protocol and lifecycle:

```python
from docassemble_simulator import SimulatorRunner

with SimulatorRunner("./my-project", interview="data/questions/main.yml") as journey:
    started = journey.start()
    answered = journey.answer({"user_name": "Alice"})
    current = journey.status()
```

Each Python-runner request gets a generated decimal-string identifier (`"1"`,
`"2"`, …) unless its journey/request mapping supplies an identifier. Supplied
identifiers may be strings or integers and are sent unchanged; the response
must echo the same value and JSON type. Other values, including `null`,
booleans, fractional numbers, arrays, and objects, are invalid and raise
`ValueError` before the request is written. Every valid request advances the
generated sequence, including supplied-ID requests. A mismatched or malformed
response invalidates the worker; the runner never retries because the
operation's commit status may be uncertain.

The runner discovers the project root when `root` is omitted, accepts a config
path or mapping, and defaults to a copied source workspace plus a temporary
session root. Configuration is resolved against the original project root before
copying or symlinking, so ancestor layers and relative explicit paths are not
reinterpreted from the temporary directory. Its deadline covers writing the
request and reading one complete newline-delimited response, including partial
or stalled frames. A timeout,
crash, malformed frame, or unexpected response id closes the runner. Timeout responses
are typed as `timeout` and include only operation context. Use
`source_policy="symlink"` or `isolate=False` only when shared source behavior is
intentional. `journey.seed("...", assemble=False)` is the explicit state-seeding
escape hatch for unsupported browser-only dynamic fields; such screens report
`dynamic_fields: true` and `field_source: code` instead of fabricated fields.
The one-shot commands remain available.

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

`--record PATH` is accepted by `start`, `refresh`, `answer`, `action`, `seek`, and
`exec`, and takes an optional `--phase NAME` used by phased comparison. The
persistent `worker`/`run`/`journey` commands accept the same flags (or
per-request `record`/`phase` fields) and record an equivalent sidecar. In a
journey, an absent or `null` `record`/`phase` field means "use the worker's
`--record`/`--phase` default", and any other non-string value is an input
error; the one-shot commands have no default phase, so there `--phase` only
ever applies to its own invocation. A recording failure (an uncreatable
destination, a trace from another run) is a typed `input` response, exactly as
it is an exit-1 `input` failure for the one-shot commands, so a truncated
journey trace never reports success; a failed operation keeps its own error and
reports the recording failure as a `recording-failure` diagnostic. Each
record includes sequence, operation, explicit **intent**, phase, submitted
assignments, outcome, screen outcome, and canonical **screen identity** with the
rule that produced it. Intent distinguishes ordinary answers (`answer`),
browser-shaped default submissions (`defaults`), expanded automatic submissions
(`auto`), native actions (`action`, with the control identifier in `target`),
and seeks (including their target, fresh, and activation context).
The first line is metadata (interview definition, config fingerprint,
docassemble/AssemblyLine/simulator versions, and the trace schema version);
appending to a trace whose metadata disagrees with the current run is an input
error. An automatic seek records one `auto` entry for every attempted screen,
including a rejected default submission, followed by the post-walk seek result.
A failed answer is recorded against the unchanged active screen with the
rejected assignments, so validation re-asks appear as repeated occurrences of
one identity. A failure with no active screen is recorded as a failure instead:
the entry keeps its error payload and carries an `error:` identity
(`error:unresolved:<variable>` for an undefined reference), never a fabricated
screen.

Identity ignores generated `Question_<n>` names, resolved occurrence paths,
random instance names, and rendered text. Non-generic explicit ids remain
`id:<id>`; generic-object and indexed-list instances are anchored as
`id:<id>@<anchor>` so distinct instances do not collapse. It falls back from
explicit block ids through targeted variables, generic-object anchors, and indexed list
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

`trace replay GOLDEN` starts a fresh isolated session, applies each recorded
walk intent (start, status, refresh, answer and its browser-defaults variant,
action, seek, and expanded automatic submissions), and stops at the
first identity, field-fact, flow, or rejected-assignment divergence. Automatic
steps initialize their recorded seek target in one operation-local namespace,
compare each pre-submission screen, and then apply the recorded transaction;
a failed automatic submission is therefore not silently replaced by a seek.
The recorded automatic group is also a hard submission bound: once the recorded
attempts are exhausted, an unexpected screen is observed and compared with the
recorded post-walk outcome instead of being submitted, so a changed interview
cannot authorize extra work or run an unrecorded screen's validation code. The
recorded activation policy is preserved: a non-activated walk does not replace
the replay session's Saved screen, while an activated walk commits its result
once, and only after every recorded attempt and the trailing post-walk outcome
matched. Replay checks identity and field facts before each submission and
checks flow/error results before continuing; a walk that ends before every
recorded attempt ran reports the earliest missing expected step. Any mismatch
stops the operation without saving, leaving the previous namespace, Saved
outcome, and Active seek unchanged. Use
`--override STEP.FIELD=VALUE` to vary one answer without changing the golden.
Replay validates the interview and configuration
fingerprint metadata, never writes the golden or the user's Saved session, and
exits non-zero on divergence. A replayable golden is a walk recording (start,
status, refresh, answer and its browser-defaults variant, action, seek, and expanded
automatic submissions); a `status` record is a read-only observation of the
saved screen. A sidecar can also carry records replay cannot reconstruct — the
worker accepts a record for any execution request, and `exec` is
one-shot-recordable — and those `eval`/`vars`/`seed`/`exec` records are
compare-only: replay reports the first such step as the typed `input` error
`trace step N records compare-only operation '<operation>'` instead of
silently skipping it.

The identity rules and the evidence behind them are documented in
[docs/screen-trace-identity-study.md](docs/screen-trace-identity-study.md).

### Retaining replay attachments

```sh
sim trace replay golden.trace.jsonl --artifact-dir ./artifacts --json
# Equivalent worker request:
# {"operation":"trace-replay","golden":"golden.trace.jsonl","artifact_dir":"./artifacts"}
```

Export is opt-in. Replay copies only files exposed as published downloads during
its executed operations, not all generated files, intermediate renderings, or
Saved session data. `result.attachments` is a manifest using the ordinary
attachment metadata (`filename`, `extension`, `mimetype`, `diagnostics`,
`structure`), with `path` and `uri` pointing at the retained copy. Repeated
publication of the same source path is exported once; distinct published files
with the same download filename remain distinct entries. Each export creates
a unique `replay-*` subdirectory beneath the requested directory, with numbered
basenames to prevent collisions. Relative directories resolve from the process
working directory; parent directories are created as needed.

The isolated replay workspace is still removed before the response returns.
Manifest files remain readable until **you** remove them; repeated replays do
not overwrite earlier exports. Without an output directory, replay retains no
files and adds no manifest. Neither mode changes screen identities, field-fact
comparison, match counts, divergence policy, the golden, or your Saved session.
A matching trace is not an assertion about document inventory or content: use
ordinary client assertions over the manifest and open its DOCX/ZIP files.

On divergence, exported files from the executed prefix are retained and the
manifest is in `error.details.attachments`, alongside the unchanged replay
report (`trace-mismatch`, CLI exit `2`). Directory, copy, or manifest failures
return an explicit `export_error` in that report. With no divergence, an export
failure is an `input` error (CLI exit `1`), even if the report's `clean` is true:
`clean` describes trace matching only. With both failures, `trace-mismatch`
keeps precedence and `export_error` records the additional export problem.
A failed export's manifest lists only completed copies; these remain retained,
but the inventory may be incomplete. Early input errors (for example, invalid
golden metadata) do not run replay or export files. Human output also reports
retained attachments and export errors.

The existing PDF-skip policy remains: a DOCX-backed PDF request can publish
real DOCX output, including DOCX members of ZIPs, **not** a fabricated PDF.
This does not establish PDF, browser-JavaScript, payment, Celery, or deployment
fidelity.

## JSON and exit codes

Every command accepts `--json` and returns one envelope. By default,
variable-seek diagnostics are bounded to a `count`, per-kind `counts`, and
`head`/`tail` events; pass `--seek-diagnostics capture` or
`--full-seek-diagnostics` for the complete ordered event list. `--seek-diagnostics
off` remains quiet. The human `check` command ends with checked/ok/failures,
and JSON check results include the same `totals`. Generated download results
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
runtime-compatibility, render, or worker-timeout failures, and `3` for
unexpected simulator faults. Argument-parsing
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
docassemble-simulator render form.docx --bind client_name=clients[0].name
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
  the expected result;
- **bindings:** `--bind NAME=EXPRESSION` (repeatable) evaluates an expression
  in the ephemeral render namespace. Configured defaults come from
  `[simulator.render_bindings]`, with `[simulator.render_bindings."form.docx"]`
  overriding them for one template; command-line bindings win over both. The
  legacy top-level `[render-bindings]` spelling supplies the whole table
  instead of `[simulator.render_bindings]`, so the two do not merge. Bindings
  are never saved.

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

### Authoring a fixture

A fixture is a Python file `exec`'d into a fresh render namespace; it is not a
module and is not imported. A fixture source never assembles, so a
template-only check needs no `start`, session, or saved state.

```python
# .config/simulator/fixture.py
from pathlib import Path

from docassemble.base.util import DALocalFile

import docassemble.my_project

PACKAGE = Path(docassemble.my_project.__file__).parent


def build_namespace():
    return {
        "client_name": "Ada Lovelace",
        "fee": currency(1250),
        "month": format_date("2026-10-01", "MMMM yyyy"),
        "letterhead": DALocalFile(PACKAGE / "data/static/letterhead.docx"),
    }


for key, value in build_namespace().items():
    globals()[key] = value
```

```sh
docassemble-simulator render fee_agreement.docx \
  --fixture .config/simulator/fixture.py --output out.docx
```

- The namespace starts from `parse.INITIAL_DICT` (essentially empty) and is the
  same namespace the prepared interview supplies at render time. Helpers such
  as `currency`, `format_date`, and `ordinal_number` resolve in it; do not stub
  them, because stubbing hides the real formatting behavior a template must be
  tested against.
- Names must be module-level globals. The `build_namespace()`-returns-a-dict
  shape assigned item by item into `globals()` is the convention that keeps
  every value visible to the template.
- `__file__` is not defined. Anchor repository-relative paths with
  `Path(docassemble.<package>.__file__)` (as above) or an absolute path.
- File inputs (an uploaded letterhead, a static PDF) need `DALocalFile(path)`
  so `include_docx_template` takes its `.path()` branch instead of a
  package-template lookup.
- A failing fixture reports its path, error type, and full Python traceback in
  the error's `details` as `fixture`, `error_type`, and `traceback`; the
  traceback's failing line is attributed to the fixture file.

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
A template-specific table such as `[simulator.render_bindings."poa.docx"]`
overrides the default binding map for that template. Command-line bindings win
over template-specific config, which wins over the default binding map. The
legacy top-level `[render-bindings]` spelling is an alternative to
`[simulator.render_bindings]` that supplies the whole table; the two spellings
do not merge. Bindings are simple names evaluated in an ephemeral render
namespace and never saved.

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

Use the default command for everyday development:

```sh
mise run test
```

It selects only fast tests, excluding real-runtime and external corpus tests
regardless of which runtimes or fixtures are installed locally. `mise run
test:fast` remains supported with the same selection, and the commit hook
continues to use that fast lane. Neither command is a complete release gate.

Run slow validation explicitly:

```sh
mise run test:runtime # Two-family runtime compatibility gate (provisions both lanes)
mise run test:corpus  # External corpus canary gate (provisions the modern lane, validates fixtures)
mise run test:all-da  # Aggregate lane: whole suite with both runtime families wired
```

`mise run test:runtime` is the runtime compatibility gate. It provisions the
isolated `.venv-da110` (docassemble 1.10.x) and `.venv-da19` (1.9.x)
environments, points `DASIMULATOR_REAL_PYTHON` / `DASIMULATOR_REAL_PYTHON_19`
at their interpreters, and runs the real-runtime contract tests, including the
screen-trace acceptance scenario, with the external corpus excluded. By
default it uses `min(4, CPU count)` pytest-xdist workers, falling back to two
when no CPU probe answers; set `DASIMULATOR_RUNTIME_WORKERS=1` for a serial
comparison/debug run, or set it to any count in the accepted range. The count
is bounded to 1–8 rather than using every detected CPU, and every worker
inherits both explicit family interpreter paths. Each runtime test uses its
own pytest temporary workspace for sessions, files, traces, and packages, so
parallel workers do not share writable interview state. The gate prints
per-family pass/fail/skip counts in either mode. A lane that is missing, cannot
import the runtime, or belongs to the wrong family fails before pytest runs,
and a family that contributes no passing coverage fails the session, so the
gate can never pass by skipping its intended coverage. `mise run sync:da19` /
`mise run sync:da110` (re)provision one family without running the gate.

Both gate scripts use the shared `scripts/runtime-lane-preflight` for lane
executability, importability, family validation, and version reporting; their
selection, fixture, worker, and false-green policies remain separate.

`mise run test:corpus` is the external corpus canary gate. It provisions the
modern `.venv-da110` lane (`sync:da110`), validates that interpreter and the
external [docassemble-yaml](https://github.com/jpagh/docassemble-yaml) fixture
checkout before pytest, then runs the `corpus`-marked contract tests against
that lane: the demo corpus runner canary and the demo package compile check.
The checkout defaults to the sibling `../docassemble-yaml/lsp/tests/fixtures`;
set `DASIMULATOR_DEMO_FIXTURES` to use another location. A missing lane, a
fixture checkout that is absent or incomplete, an empty selection, or a suite
in which every corpus test skipped fails the command with recovery
instructions, so it cannot pass by skipping the canary it exists to run.
Prepared NLTK data is reused from a stable, version-aware cache; set
`DASIMULATOR_NLTK_CACHE_DIR` to choose its root (default
`.simulator/nltk-cache`), and a new generation is published only when the
lane's Python or runtime identity changes. The direct runner's
`--refresh-nltk-cache` flag remains the explicit refresh path. Corpus case
concurrency is bounded and configurable through `DASIMULATOR_CORPUS_JOBS`
(default 4); the runner's own default is serial. This task is deliberately the
canary over the selected contract, not a run of the whole external corpus: run
`scripts/test-demo-corpus` directly (with `--match`, `--shard`, or no filter)
to exercise every example.

To run every test in the current environment, use `uv run pytest`; runtime and
corpus checks skip when their prerequisites are absent. The gates themselves
fail instead of skipping, so use the provisioned cross-family gate and the
corpus gate for complete slow validation.

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
target smoke path, and runs the whole suite once with both lanes wired up
(`DASIMULATOR_REAL_PYTHON*` pointing at those interpreters), so every
cross-family contract test exercises the modern and legacy runtimes. It is the
aggregate lane: fast tests plus both families, plus the corpus tests when their
fixtures are available. The compatibility gate proper is `mise run
test:runtime`, which runs the same two lanes against the runtime contract only.
Use `mise run sync:da19` / `mise run sync:da110` to (re)provision one family
without running the suite.

There is no CI runner for the 1.9.x lane yet: `mise run test:runtime` is the
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

`mise run test:corpus` runs the canary subset of this runner behind the lane
and fixture checks described above; invoke `scripts/test-demo-corpus` directly
for a focused or full run.

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

Releases are cut with `mise run release`. A release first visibly runs the
slow two-family runtime compatibility gate (`mise run test:runtime`, excluding
the external corpus), then bumps the patch digit, pushes the commit and
annotated tag, and publishes the GitHub release with notes generated from the
commits since the previous tag. A failed or interrupted gate leaves version
files and release Git state unchanged. Run `mise run test:corpus` separately
when the external corpus gate is needed; it is not part of release validation.
`mise run bump` still bumps and pushes without creating a release, and
`scripts/release --dry-run` previews the next bump without provisioning
interpreters or running either slow gate.

## License

MIT. See [LICENSE](LICENSE).
