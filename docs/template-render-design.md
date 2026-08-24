# Template Render Command — Design

Status: proposal, ready for implementation
Date: 2026-08-24
Scope: add a `render` command to `docassemble-simulator` that renders a converted `.docx`
template through the real docxtpl + docassemble Jinja pipeline, making the simulator a
standalone superset of the ad-hoc `template-render-harness.py` that lives in the
`docassemble-automatedpleading` package (`var/interview-test-framework/`).

## 1. Motivation

A docassemble server request has two halves that never touch each other:

1. **Interview logic** — which screens does the flow hit, do variables resolve, does
   validation pass?
2. **Document assembly** — after the interview finishes (`DAErrorNoEndpoint`), each
   attachment's `.docx` is rendered through docxtpl + docassemble's Jinja environment.

The simulator today covers half 1 completely (check/questions/index/start/set/seek/get/…)
but stops at the exact moment a server would start half 2: `current_screen()` reports
`kind: "finished"` and nothing else happens. `data/templates/` is never touched.

The only local coverage for half 2 is `template-render-harness.py` — a standalone script
that pushes one template through the render pipeline against a **hand-built fake
namespace**. It has demonstrated weaknesses:

- **Namespace drift.** The fake `M`/parties/venue tree and the function stubs
  (`currency`, `pronouns`, `family_child_support_allocations`, …) are maintained by hand
  and silently diverge from the interview. Concrete example (2026-08-24): the harness
  failed on a correct template because its fake namespace lacked
  `family_support_obligation` — a function the real interview imports for free.
- **Approximations.** Radios are hardcoded as ints with comment workarounds, `as_kind`
  is a manual lambda, `currency` is `"$%s" % v` instead of the real locale-aware function.
- **Per-package, perpetually out-of-sync.** It lives in the target package's gitignored
  `var/`; one copy per package.

The simulator already has the **real** namespace for free: the interview's `modules:`
imports land in `user_dict` during assemble (this is exactly why `_picklable_view` must
drop callables before pickling), so rendering against the assembled session state uses
real `DWThing`/`DAList`/`DWParty` objects, real helper functions, real venue objects — no
fake to maintain.

## 2. Goal

`docassemble-simulator render TEMPLATE.docx` is a standalone, agent-friendly replacement
for `template-render-harness.py` (and the follow-up `probe-render-session.py`):

- Renders the template through the **same docxtpl pipeline** the harness used, which is
  the **same pipeline the server uses** under the hood.
- Renders against **real session state** by default (mode A), with a **fixture mode**
  (mode B) for template-only checks when no flow data is available.
- Reports render failures with the paragraph line number, in human text and `--json`,
  and is exit-code distinguishable (mirroring `start`/`set`).
- Keeps every existing command's behavior unchanged.

Non-goal for v1: full attachment assembly (multi-attachment generators, redacted twins,
docxcompose merges, CRIFS coversheet includes, fillable-PDF rendering). That is future
work (see §9) and would build on this command.

## 3. What the harness pipeline does (the exact steps to reproduce)

From `template-render-harness.py main()` — these are the operations that catch errors the
simulator's `check` cannot, and that `render` must reproduce verbatim:

1. `DocxTemplate(path)` + `.render_init()` — compile the docx's `{% %}`/`{{ }}` markers
   into a Jinja2 template.
2. Insert `\n` before every `<w:p` (`re.sub(r"<w:p([ >])", r"\n<w:p\1", xml)`) so Jinja
   syntax errors report a real paragraph line instead of line 1 of one giant XML line.
3. `fix_quotes` (from `docassemble.base.helpers`) — convert curly “smart” quotes *inside*
   `{{ }}` expressions to straight quotes. Mandatory: templates deliberately keep curly
   quotes in body text (byte-identical conversion bar), but a curly quote inside a Python
   expression is a syntax error; the server applies the same helper.
4. `.patch_xml(xml)` — docxtpl's XML-escaping wrap for the template markers.
5. `custom_jinja_env().parse(xml)` — compile-time validation (Jinja *syntax* only).
6. `.render(context, jinja_env=custom_jinja_env())` — the real evaluation; surfaces
   undefined variables, wrong object shapes, missing filters.
7. Optional `EXPECT_MISSING VAR` assertion — prove a variable *stays* undefined through
   the render (the load-bearing “demand-preserving” check: a correct template must keep
   demanding a field, never silently swallow it).

## 4. Design

### 4.1 Command surface

```
docassemble-simulator render TEMPLATE.docx [options]
```

Positional `TEMPLATE` is a template filename resolved under the package's
`data/templates/` directory (e.g. `family_parenting_plan.docx`). Resolving against
`<root>/docassemble/<pkg>/data/templates/`; error clearly if not found.

Options:

| Flag | Meaning |
|---|---|
| `--root` / `--interview` | Inherited from the common parent parser (existing behavior). |
| `--fresh` | Discard any saved session and run a fresh flow before rendering (like `start`). Without it, use the saved session if one exists, else a fresh flow. |
| `--no-flow` | Skip the assemble pass entirely; render against the saved session state only. |
| `--fixture PATH` | Mode B: exec a Python script in the render namespace, then render against it (harness-parity mode; see §4.4). |
| `--expect-missing VAR` | Assert `VAR` stays undefined through the render (parity with the harness's `EXPECT_MISSING`). Exit 2 if it resolves. |
| `--output DIR` | Write the rendered `.docx` into `DIR` (default: `var/render/` under the package root or a temp dir when no var dir exists). Without it, render is validate-only. |
| `--json` | Machine-readable result (existing convention). |

Exit codes: `0` rendered OK (all assertions held); `2` render error / failed expectation
(mirrors `start`/`set` error-screen behavior, per code-review item 6).

`--json` result shapes:

```json
// success
{"template": "family_parenting_plan.docx", "ok": true,
 "paragraphs": 137, "artifact": "/tmp/.../family_parenting_plan.docx"}

// failure
{"template": "family_parenting_plan.docx", "ok": false,
 "error_type": "UndefinedError",
 "message": "'M.family.parenting_plan.child_support_total_amt' is undefined",
 "paragraph": 31}
```

### 4.2 How `render` gets its namespace (mode A — real state)

The default mode renders against the real interview namespace:

1. `Session(root)` + `load_interview()` (same as every command).
2. Load `session.pkl` if present, else a fresh `user_dict` (`fresh_user_dict()`).
3. Run one assemble pass inside `Session._in_interview(interview, user_dict)` —
   `interview.load_util()`, `run_prelude()`, `assemble()` — tolerating `DAErrorNoEndpoint`
   exactly like `current_screen()`. This repopulates the namespace the same way a server
   request does: imported modules (`modules:`) land as callables in `user_dict`, `M` and
   the package objects are live. This is the crucial difference from the harness: no fake
   namespace, no hand-maintained `fields` dict.
4. Render the template with `user_dict` as the Jinja context (§4.3).
5. If the flow stopped at an unanswered question that the template needs, the render fails
   with a normal seek-able `UndefinedError` — the user answers the screen with
   `start`/`set` and re-runs `render`, or uses `--fixture` to plant data directly.

The render context is `user_dict` itself: it already contains `M`, `nav`, the interview
imports, and `T`/`documents`/etc. as defined by the flow.

### 4.3 The render pipeline (shared by both modes)

New module `src/docassemble_simulator/render.py`:

- `find_template(root, filename) -> Path` — locate `data/templates/<filename>`.
- `prepare_docx_template(path) -> DocxTemplate` — the harness steps 1–5 (render_init,
  newline injection, `fix_quotes`, `patch_xml`, `custom_jinja_env().parse`). Keep the
  regex and quote logic byte-identical to the harness.
- `render_template(docx_template, context) -> None` — `.render(context,
  jinja_env=custom_jinja_env())`.
- `RenderError` — carries `paragraph` (extracted from the exception's line number via the
  injected newlines) and message.
- `assert_missing(context, var) -> None` — the `EXPECT_MISSING` semantics.

All rendering happens inside `Session._in_interview(interview, user_dict)` because two
docassemble dependencies need the thread context the server sets up:

- `custom_jinja_env()` filters and helpers may touch `this_thread` state.
- Templates that call `current_context()` need
  `this_thread.current_info["yaml_filename"]` — `_in_interview` already sets it, so mode A
  gets this for free (the harness stubbed it with a fake `x`).

### 4.4 Fixture mode (mode B — harness parity)

`render TEMPLATE.docx --fixture build.py`:

1. Fresh `user_dict`; `M` is a plain `DAObject` (or whatever the fixture builds).
2. Exec `build.py` inside `_in_interview` (reuse the `run_prelude` exec pattern — extract
   a small `exec_in_namespace(code, user_dict, interview)` helper so prelude, fixture, and
   `exec` share one path), then render.

The fixture script author builds the object tree exactly like the harness's
`build_namespace()` used to (fake parties, venue, `M.family.parenting_plan`, …). Mode B is
the migration path: the old `build_namespace()` body becomes a fixture script, so existing
template-only checks carry over unchanged. Document that mode B is for template-only
checks where the flow cannot reach the needed data; mode A is the default and better.

Implementation simplification: fixture mode can reuse the existing
`.dasimulator/` conventions — for example `render` also honors a
`<root>/.dasimulator/render-fixture.py` if present (mirroring `prelude.py`). Default when
no session and no fixture: a fresh flow (mode A).

### 4.5 Where it wires in

- `src/docassemble_simulator/render.py` — everything render-specific (pipeline, fixture
  exec, error typing, artifact writing).
- `src/docassemble_simulator/cli.py` — `cmd_render(args, root)` + the `render` subparser
  (parents=[common]), exit-code mapping, `--json` emission. Follow the existing
  command structure exactly (`cmd_check` is the closest template: it also loops a small
  flow over one interview and reports ok/failure).
- `README.md` + the help epilog — document `render` in the agent loop
  (“render a template against the session” after `start`/`set`).
- No changes to `check`: it stays compile-only; template checking is an explicit, separate
  command.

### 4.6 dyld / environment

No new work: `main()` already re-execs with `DYLD_FALLBACK_LIBRARY_PATH` set, and docxtpl
is available in the target package's venv (docassemble-webapp pulls it). `render` inherits
the documented install contract: run inside the target package's virtualenv.

## 5. Conventions this must respect

- Agent-friendly: human-readable text by default, `--json` for machines, no interactive
  prompts, small stateless commands, sessions persist under `<root>/.dasimulator/`.
- Exit-code discipline: `0` success, `1` usage/session errors (via the central
  `SessionError` catch), `2` render failure — consistent with `start`/`set`/`seek`.
- The formula `os.replace()` for atomic writes (code-review item 7) applies to writing the
  rendered artifact too.
- The render pipeline steps must stay in sync with the harness's: quote fixing and the
  paragraph-newline trick are *behavior*, not cosmetics — dropping them changes what the
  command catches.

## 6. Testing plan

Unit tests (repo has `tests/` with pytest; follow `test_bootstrap.py`/`test_describe.py`
style — pure/near-pure logic only, no docassemble required):

- `find_template`: resolution, missing-file error.
- `prepare_docx_template` on a tiny generated fixture docx (build one in-test with
  `python-docx`-free XML or a checked-in 1-paragraph template): newlines inserted per
  paragraph; smart quotes inside `{{ }}` converted, body quotes untouched.
- `RenderError` paragraph extraction from a line number.
- `assert_missing` semantics (missing → pass; resolved → error).
- CLI: argument parsing, exit codes, `--json` shape (mock the render stage).

Parity check (manual, needs a real package venv — recorded, not automated, same as the
existing harness):

- In `docassemble-automatedpleading`: `render family_parenting_plan.docx --fresh` against
  a completed session; then fixture mode with the old `build_namespace()` body; both
  report `Rendered OK`.
- Re-run the 2026-08-24 regression: the template calls `family_support_obligation`; with
  mode A (real imports) it must render without any fixture stub — demonstrating the drift
  fix.

Existing test suite must stay green; no existing command's behavior changes.

## 7. Acceptance criteria

1. `docassemble-simulator render family_parenting_plan.docx` inside the
   `automatedpleading` package, after `start` + a few `set`s, renders OK without any
   fixture or fake namespace.
2. Missing template variable → exit 2, message includes the paragraph line; `--json`
   matches the shape above.
3. `--expect-missing M.x` passes when `M.x` stays undefined, exits 2 when it resolves.
4. Fixture mode reproduces the old harness result on the same template.
5. `--output` writes a valid `.docx` (opens in Word/LibreOffice; `docx validate` clean).
6. `check`/`start`/`set`/`seek`/etc. unchanged; existing tests pass.
7. README + `--help` document the new command.

## 8. Risks / open questions

- **`current_context().attachment`** — templates that read
  `current_context().attachment` on the server get it from the attachment parser during
  assembly. Mode A does not run the attachments builder, so such templates need a fixture
  stub until §9 lands. Document this in the command help.
- **Session state completeness** — a half-answered session renders with whatever is
  defined; `UndefinedError` is the correct outcome and the intended signal to answer more
  screens. Do not silence undefined references.
- **Multiple templates / attachment resolution by doc key** — v1 takes a filename; doc-key
  lookup into the interview's attachment registry is a natural v1.1 (see §9).

## 9. Future work (explicitly out of v1)

- Full attachment assembly: run the package's own attachments builder / `generate_attachments`
  after `kind: "finished"` so multi-attachment bundles, redacted pairs, CRIFS /
  `include_docx_template` prepends, and docxcompose merges produce byte-identical
  server outputs. This is the “server's terminal phase” and the eventual superset of both
  `template-render-harness.py` and `probe-render-session.py`.
- Doc-key resolution (`render --dockey family_parenting_plan`) mapping to the attachment's
  template filename from the interview definitions rather than the filename convention.
- Fillable-PDF (`pdf template file`) rendering, which uses different machinery.
- Folding render results into `check` output as an opt-in (`check --render`).