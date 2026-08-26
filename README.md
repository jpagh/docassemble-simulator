# docassemble-simulator

Run a docassemble interview locally without a server. The simulator uses the
real docassemble compiler, assembly/seek machinery, validation behavior, and
DOCX/Jinja renderer while providing small commands suitable for humans and
agents.

## Install

Run the simulator in an interpreter that can import both the target package and
`docassemble.base`:

```sh
uv pip install --python /path/to/package/.venv/bin/python docassemble-simulator
```

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
- `answer` applies browser-shaped values to the active saved screen as one
  transaction, validates, marks it answered, assembles, and commits. Assignment
  or validation failure discards every submitted value.
- `status` only reads the saved outcome. `refresh` explicitly rehydrates and
  reassembles saved state.
- `seek` starts from saved state by default. `--fresh` is isolated and
  `--activate` persists the sought screen as the active screen.
- `eval` and `vars` rehydrate the namespace but never save it.
- `exec` assembles and saves by default. `--no-assemble` performs a state-only
  commit after successful Python execution.

`answer` parses JSON, then Python literals, then plain text. Checkbox and object
choice fields use the active field metadata. An exact `YYYY-MM-DD` submitted to
a date field becomes docassemble's timezone-aware `DADateTime`; malformed and
impossible dates are rejected. The same text submitted to a text field remains
a string. `answer --code` and `exec` retain Python semantics and bypass browser
coercion.

State is versioned and stored separately for each canonical interview under
`.simulator/sessions/`. Files are trusted-local pickle payloads. Unsupported or
stale state fails with an instruction to run `start` again. Mutations hold a
per-interview advisory lock and commit with atomic replacement.

## JSON and exit codes

Every command accepts `--json` and returns one envelope:

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

Exit codes are `0` for success (including interview completion), `1` for usage,
workspace, configuration, or missing-state failures, `2` for validation,
execution, seek, compile, or render failures, and `3` for unexpected simulator
faults. Argument-parsing failures follow the same envelope when `--json` is
present and exit `1`; normal `--help` output remains a successful exit.

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

DOCX output is supported. Generated PDF conversion remains intentionally
stubbed: the simulator does not invoke an external converter.

## Workspace configuration and seeds

Runtime files live under `.simulator/`. Authored simulator configuration lives
under `.config/simulator/`:

```text
.config/simulator/config.toml
.config/simulator/config.py
.simulator/sessions/
```

`config.toml` is merged with discovered parent/global configuration and supplied
to docassemble. Optional `config.py` is package runtime adapter code, executed
after standard namespace rehydration and before authored flow, evaluation, or
render work. Use it for deterministic substitutes for package/server
requirements such as no-worker `background_action()` behavior and for seeded
server-scoped data.

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
policy. It does not provide browser HTML, uploads, arbitrary server workers,
Celery, or rollback of external filesystem/network effects caused by authored
Python. Package-specific server dependency behavior belongs in authored seeds.

## Development

Run the fast suite with:

```sh
uv run pytest -q
```

Real-docassemble fidelity is covered by a separate lane that runs the CLI in a
target package's interpreter (which supplies `docassemble` and `python-docx`):

```sh
scripts/test-real-runtime /path/to/target/package/.venv/bin/python
```

The lane skips when no interpreter is configured and fails clearly when the
supplied runtime cannot run the tests. The fast suite uses stubbed runtime
modules and never requires `docassemble`.
