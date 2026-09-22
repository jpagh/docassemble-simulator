# Render Follow-up — Agent-drive Fidelity, State Capture, Real-mode Includes

Status: implemented — all three gaps closed; the §6 acceptance criteria are
covered by the render unit and contract tests, and the §5 real-package
integration pass was manual.
Date: 2026-08-24
Scope: three follow-on gaps found while live-testing the `render` command
(built from `docs/template-render-design.md`) against the
`docassemble-automatedpleading` package. Tackles, in order: (1) `describe`
choice fidelity so an agent can answer any screen, (2) reproducible
real-state renders (snapshots + answer recipes), (3) real-mode
`include_docx_template` (CRIFS) verification.

## 0. Context

The `render` command v1 works: fixture mode reproduces the old
`template-render-harness.py` result byte-for-byte on the same namespace
(parity confirmed 2026-08-24, 279 paragraphs), `--expect-missing` holds in
both directions, failure reporting includes the template paragraph number,
`--output` writes a `docx validate`-clean artifact, and mode A (real flow
state) runs the real assemble pass and reports strict `UndefinedError`s.

Live-driving the family flow surfaced three gaps that block two things:

- **An agent answering screens from `render`'s sibling commands**
  (`start`/`set`/`status`): the screen descriptions do not faithfully expose
  the answer keys for buttons, checkboxes, or reference picks.
- **`render` mode A without a human in the loop**: producing an assembled
  state requires hand-driving ~30 screens each time; there is no replay, and
  the one template feature fixtures must stub (`include_docx_template` /
  CRIFS) is unverified in real mode.

## 1. Gap 1 — `describe` choice fidelity

### 1.1 The gather-button misrender (confirmed)

The party/child gather screen ("Gathering x `there_are_any/there_is_another/revisit`",
`x_generic_questions.yml` in the family package) is a `multiple_choice` with a
single button field whose compiled `choices` is the docassemble form
`[{'label': TextObject, 'key': TextObject}]`. `describe_choices()`
(`src/docassemble_simulator/describe.py`) iterates the dict's *items*, so it
emits:

```
choices: 'label' (Continue), 'key' (complete)
```

i.e. it treats the literal strings `'label'` and `'key'` as the values. The
real submitted value is the **`key` text** — the screen's `validation code`
matches `x.button == "there_are_any"` / `"there_is_another"`. Live-proved:

- `set x.button=complete` / `set --code "x.button=-1"` → screen repeats.
- `set x.button=there_is_another` → advances (sets `x.there_is_another=False`,
  deletes the button).

**Fix:** in `describe_choices`, when a choice item is a dict containing both
`label` and `key` keys (the multiple_choice button form), emit
`{"value": key-text, "label": label-text}` instead of iterating the items.
Unit-test with a mocked `TextObject` (the repo's `SimpleNamespace` style).

### 1.2 Checkbox assignment coercion (confirmed)

`set 'documents.selected_documents={"family_parenting_plan": true}'` is
rejected: validation crashes on `'dict' object has no attribute
'true_values'` because the server stores checkbox answers as a `DADict`, not a
plain dict. Working path today: `set --code "…=DADict(elements={...})"`.

**Fix:** teach `set` to coerce dict-literal values into `DADict` when the
target field's described `type` is `checkboxes`. The screen description
already carries the field type, so this stays server-faithful (the server
builds a `DADict` from the POST on the same screen). Keep
`--no-validate`/`--code` bypasses working as-is.

### 1.3 Reference-based choices (object_radio / combobox-of-list)

`object_radio` and list-referencing `combobox` fields (e.g.
`M.family.parenting_plan.holidays_schedule[i].schedule` with choices
`visitation_time_options`) compile to live object entries; `describe_choices`
currently falls back to `<unrenderable choice: …>` reprs, so an agent cannot
answer them from the description, and `set VAR=plain-string` may not resolve
to the object.

**Plan (spike first):** determine the exact value format `set` must assign for
each reference shape — likely the choice's `instanceName` string or its
safeid, as the browser POSTs. Then:

- If resolvable: emit `{"value": <instanceName>, "label": <str(choice)>}`.
- If not determinable statically: emit `{"reference_to": "<list var>"}` and
  document the `--code` workaround (`x.field = M.family.parenting_plan.visitation_time_options[0]`)
  until a native `set` reference syntax lands.

Do **not** block Gap 2 on this; combobox-of-strings is already described fine.

## 2. Gap 2 — reproducible real-state renders

Goal: `render` against a *real* assembled state without hand-driving every
time, and re-render after interview edits (regression). Two mechanisms, both
small; snapshot is the 80% case.

### 2.1 Snapshots

- `render --snapshot PATH` — after assembling (mode A), save
  `_picklable_view(user_dict)` to `PATH` (pickle). Reuses the session's
  existing drop-callables logic (`session._picklable_view`: interview imports
  don't survive pickling — identical to a server save/load round trip).
- `render --from-snapshot PATH` — load into `user_dict`, then run **one
  assemble pass** (as `cmd_render` already does for `--no-flow`/default
  paths) so imports and any flow-defined names repopulate, then render.
- Immutability note: `--fresh --from-snapshot` conflict-checks like
  `--fresh --no-flow`.

The result is "record a real state once, replay forever" and a direct,
feature-parity replacement for `probe-render-session.py` (which renders
against server-dumped `UserDict` records).

### 2.2 Answer recipes

- Format: `<root>/.simulator/recipe.jsonl` (or `--recipe PATH`), one answer
  per line:
  ```json
  {"screen": "Question_16", "set": [["dwpms.select_a_matter", "None"]]}
  {"screen": null, "code": "documents.selected_documents=DADict(elements={'family_parenting_plan': True})"}
  {"screen": "ID Gathering x `there_are_any/there_is_another/revisit`", "button": "there_is_another"}
  ```
- New `drive --recipe FILE` command: start a fresh session and replay the
  entries in order, running the same per-screen apply → validate → mark → 
  assemble loop as `set` between entries; stop when the recipe is exhausted
  or the flow finishes; report the final screen. `--json`, exit codes as
  usual. If a screen's exact question name isn't needed, allow
  `"screen": null` (blind apply) for simple flows.
- Recording: `set --record` appends the successful assignments to
  `<root>/.simulator/<interview>.recipe.jsonl` so a session walked by hand
  once becomes a reusable script.
- Recipes are interview-scoped like sessions (same `interview_path`
  preamble/guard).

### 2.3 Relationship

Snapshot = finished state, cheapest. Recipe = replayable path, survives flow
changes better (re-assemble each step). Ship snapshots first; recipes when a
checked-in walk of a real category is wanted (e.g. the family parenting-plan
path as the reference example).

## 3. Gap 3 — real-mode `include_docx_template` (CRIFS)

`family_parenting_plan.docx` (and the dissolution petition) prepend the CRIFS
coversheet via bare `include_docx_template("crifs_mo.docx")`. Fixture mode
stubs it (`lambda n: ""`); mode A's real imported function has never been
exercised in the simulator context.

**Plan:** spike first — with an answered state (snapshot/recipe from §2),
does the real `include_docx_template` render without an attachment context, or
does it raise / need `current_context().attachment`? Then:

- If it renders: the `--output` artifact includes CRIFS; verify with `docx
  validate` (merged cover + body) and `docx read` (coversheet first). Optionally
  `docx diff` against the server-produced document when available.
- If it needs attachment context: seed the minimal context from the interview's
  attachment registry (the target template's owning document key → attachment
  data) OR add `--no-includes` to skip include calls with a clear warning, and
  keep the limitation documented. Do not fake the CRIFS content beyond what the
  real function produces.
- Note the `_use_jinja2` dichotomy: the package's `documents.py` uses
  `include_docx_template(letterhead, _use_jinja2=False)` for headers; CRIFS in
  the family templates is the default Jinja form. Verify both if a header/layout
  template enters the picture.

## 4. Suggested order of work

1. §1.1 gather-button describe fix + unit tests (unblocks agent-driven `set`
   on the most common screen type; smallest blast radius).
2. §1.2 `set` checkbox → `DADict` coercion + tests.
3. §2.1 snapshots + `--snapshot`/`--from-snapshot` flags + tests.
4. §1.3 reference-choice spike + describe `reference_to` fallback.
5. §3 include spike, then `--no-includes` and/or context seeding, with the
   family parenting-plan snapshot as the reference scenario.
6. §2.2 `drive --recipe` + `set --record` (optional nicety if snapshots prove
   sufficient for the team's workflow).
7. Re-run the parity checks from `template-render-design.md` §6/§7 and add a
   documented walk: snapshot the family parenting-plan state → render → `docx
   validate` artifact.

## 5. Testing plan

Unit (repo `tests/`, `SimpleNamespace` style as in `test_describe.py`):

- `describe_choices`: the `{'label': TextObject, 'key': TextObject}` button
  form emits `{value: <key text>, label: <label text>}`; existing list/tuple
  forms unchanged; `reference_to` fallback shape.
- `set` coercion: a `checkboxes`-typed described field wraps dict assignments
  in `DADict`; non-checkbox fields unaffected; `--code` bypass unchanged.
- Snapshot round trip: unpicklable entries dropped by `_picklable_view`;
  `--fresh`/`--from-snapshot` conflict validation.

Integration (manual; needs a real package venv, recorded not automated):

- In `automatedpleading`: `start` → answer Clio skip → document selection →
  party name → gather button — all from `status` descriptions alone (no
  guessing), the §1.1/§1.2 proof.
- `render family_parenting_plan.docx --from-snapshot <recorded>` reports
  `ok: true` and the `--output` artifact passes `docx validate` with CRIFS
  present once §3 lands.

## 6. Acceptance criteria

1. The gather button screen describes as `{'value': 'there_is_another', 'label': 'Continue'}`;
   `set x.button=there_is_another` advances from the description alone.
2. `set 'documents.selected_documents={"family_parenting_plan": true}'` works
   (coerced to `DADict`), with `--code` behavior unchanged.
3. `render --snapshot`/`--from-snapshot` round-trips a real family state and
   renders `family_parenting_plan.docx` OK without hand-answering.
4. Real-mode renders include the CRIFS content (or `--no-includes` skips it
   with a warning; no silent stubbing).
5. Existing `check`/`start`/`set`/`seek`/`render` behavior unchanged; repo
   suite green; parity checks from `template-render-design.md` still pass.

## 7. Out of scope (unchanged from v1)

PDF templates (`pdf template file`), attachment generators/bundles, redacted
twins, docxcompose merges, and doc-key template resolution remain future
work; the `render` command stays a single-template pipeline. Generated PDF
conversion is intentionally stubbed: DOCX artifacts are validated and saved,
while a deployment/server remains responsible for producing PDFs.