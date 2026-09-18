# Simulator Workspace Layout and Flow-Fidelity Fixes

Status: implemented
Date: 2026-08-24
Scope: two coupled pieces of work on `docassemble-simulator`:

- **A. Workspace layout** — a clear commit/ignore line: a per-package
  `.simulator/` directory that is **entirely gitignored runtime state**, plus
  committed **authored** files under `.config/simulator/`, following the
  [`mise`](https://mise.jdx.dev/configuration.html) / `jda`
  (`/Users/jack/Projects/jda`) project-config pattern (`.config/mise/config.toml`,
  `.config/jda/config.toml`): discovered by walking up from the package root,
  committed defaults plus gitignored local overrides.
- **B. Flow-fidelity fixes** — five simulator gaps found while live-driving
  the `automatedpleading` family flow against `render` (2026-08-24), each with
  a reproduction and a fix.

A and B are coupled only by the config machinery (item B5 is the "config is
actually loaded" implementation that A defines).

## Part A — workspace layout

### A1. The commit/ignore line

**Everything under `.simulator/` is gitignored runtime state** — sessions,
generated config, render outputs. **Everything committed is authored, under
`.config/simulator/`** — the config pair and optional fixture. The rule: if
the file is *generated or mutated during a run*, it lives in `.simulator/`
and is ignored; if it is *authored by hand and only read*, it lives in
`.config/simulator/` and is committed.

| File | Kind | Location | Commit? |
|---|---|---|---|
| Config (TOML) | authored | `.config/simulator/config.toml` | yes |
| Local config overrides (TOML) | authored | `.config/simulator/config.local.toml` | **no** |
| Setup script (`config.py`) | authored | `.config/simulator/config.py` | yes |
| Render fixture (optional) | authored | `.config/simulator/fixture.py` | yes |
| Session state | runtime | `.simulator/session.pkl` (+ `.tmp`) | no |
| Effective config (generated YAML) | runtime | `.simulator/config-effective.yml` | no |
| Rendered artifacts | runtime | `.simulator/render/` | no |

`.gitignore` entry:

```
.simulator/
.config/simulator/config.local.toml
```

`.config/simulator/` mirrors mise's `.config/mise/` for projects that group
config into a directory; root-level loose files remain supported for
root-style setups (A2).

### A2. Config file discovery and merge (mise/jda pattern)

Follow `jda`'s `PROJECT_CANDIDATES` exactly, with `simulator` substituted:

```
simulator.local.toml          # uncommitted local overrides (gitignored)
.simulator.local.toml
simulator.toml
.simulator.toml
simulator/config.toml
.simulator/config.toml
.config/simulator.toml
.config/simulator/config.local.toml
.config/simulator/config.toml
```

- Walk up from the package root to the filesystem root; load every existing
  candidate at every level; **deep-merge**, nearest/higher-precedence wins;
  `*.local.toml` beats its non-local sibling within a level. Missing files
  contribute nothing; a malformed lower-priority file still fails loudly
  (jda convention).
- **Recommended primary** (new packages and this repo):
  `.config/simulator/config.toml` — the directory-grouped candidate, the same
  shape mise uses for `.config/mise/config.toml`.
- **Global config**: `$DOCASSEMBLE_SIMULATOR_CONFIG` if set, else
  `$XDG_CONFIG_HOME/docassemble-simulator/config.toml`. Global is the lowest
  layer; any project candidate overrides it. (This replaces the current
  `~/.config/docassemble-simulator/config.yml`, whose historic `jinja data`
  leak — see B5 — ends with this work.)
- **Schema**: simulator-owned policy is reserved under `[simulator]`:
  `missing_runtime = "install"|"disabled"`, `background_actions =
  "foreground"|"disabled"`, `offline`, and `render_bindings`. Docassemble
  pass-through keys include `jinja data`, `timezone`, and supported database or
  Redis settings. Example:

  ```toml
  timezone = "America/Chicago"

  [jinja-data]
  jinja_config_automatedpleading_document_categories = { family = "Family" }

  [simulator]
  background_actions = "foreground"
  ```

- **Effective config is YAML**: docassemble reads `get_configuration()` from
  a YAML file (`DA_CONFIG_FILE`). The simulator merges the TOML layers itself
  and writes the merged result as YAML to `.simulator/config-effective.yml`
  (root-scoped, first-call-wins in `prepare_environment`, exactly like the
  current code-review-plan-item-2 fix — just pointed at the new paths). The
  authored file is TOML for mise-parity; the runtime handoff to docassemble
  stays YAML.

### A3. Setup script — `.config/simulator/config.py`

The setup script lives at `.config/simulator/config.py`,
paired with the TOML. It is optional because standard local service substitutes
are built in. It is executed only as authored seed code; a render fixture must
be selected explicitly with `--fixture`. It keeps its job: exec in the session namespace before
every flow pass to stub server-only dependencies (firm data, OAuth, matter
lookups, …). It is authored and committed; the code resolves a single fixed
path (no discovery needed for code, unlike the data config). A render fixture is selected explicitly with `--fixture`; the presence of
`.config/simulator/fixture.py` does not change the default saved-session source.

**The seed contract — pre-create server-scoped globals.** Some docassemble
globals are defined outside a package's main flow: account-scoped, backed by
server-only storage, or defined in auxiliary YAML (`account_*.yml`) that the
interview only reaches after login. These must be instantiated and stubbed in
`config.py` **ahead of time**:

- `DAGlobal` — e.g. `firmdata` (a `DAGlobal`/`DWGlobal` bound to a storage
  key) for firm/onboarding data
- `DARedis`, `DAStore`, `DACloudStorage`
- `DAOAuth`, `DAWeb`, `DAGoogleAPI`

If the flow meets any of them lazily, docassemble's generic machinery creates
a throwaway container with a random `instanceName` and no backing data, and
never persists the container into `user_dict`. Object-reference fields then
build selection keys from those random names (`parse.py:6478-6481` safeids the
key and execs it) and crash with `NameError`. Pre-creating them with stable
`instanceName`s and `gathered = True` makes choice keys real, evaluable
variables.

**Naming and reference roots matter.** Do not rely on an `instanceName=`
keyword: use a positional name where supported, or assign
`obj.instanceName = name` and `obj.has_nonrandom_instance_name = True` after
construction. The latter works across docassemble object classes and prevents
`DAList.append()` from renaming the item into a random container. Register
server-scoped roots with `set_info()` as a portable seed fallback:

```python
from docassemble.base.functions import set_info

set_info(firmdata=firmdata, M=M)
```

The simulator also registers top-level `DAObject` roots after each seed/pass,
mirroring the server's `initial` code block. For `Address`, set `.address`,
`.city`, `.state`, and `.zip`; `line_one()`, `line_two()`, and `block()` are
methods. Seed the fields templates actually read (for example attorney
`name`, `bar_id`, and `email`) and mark reference lists `gathered` and
`there_are_any`.

Object-reference answers sent to `set` use the browser shape as JSON:
`{"<safeid-key>": true}`. A Session API caller must pass the current
`screen` to `apply_assignments()` so the object field can be detected.

### A4. Naming: `config.py` (inside the config directory)

Inside `.config/simulator/`, **`config.py` is the right name** — it is
unambiguously "the simulator config's Python half", sitting next to
`config.toml`, exactly as mise groups `.config/mise/config.toml`. The earlier
objection (a bare root `config.py` is not the config) dissolves once the
directory scopes it. Keep the two halves separate: `config.toml` is the
declarative, mergeable data; `config.py` is the executable seed code. Do not
fold them into one file — executing committed "config" code at load is the
footgun `mise`/`jda` deliberately avoid. One-line docstring on `config.py`
states the seed-session contract.

### A5. Defaults and capability boundary

With no configuration, the simulator uses an in-process fake Redis, local file
storage, generated effective YAML, debug mode, localhost, `en_US`, `US`, the
local timezone (falling back to `America/New_York`), and foreground background
actions. It substitutes no server database: AssemblyLine's PostgreSQL-backed
session metadata is disabled by default and can be enabled only alongside a
real `db` configuration. These are simulator stubs and are not PostgreSQL,
Redis, Celery, or server storage. DOCX output is supported; generated PDF
conversion is unavailable, no converter is invoked, and PDF-only download
verification is deferred to a real deployment/staging environment. Run `info`
for this report or `config --json` for redacted settings and pass-through keys.
The default categories are: fake Redis and foreground actions are in-process
stubs; database-backed session features are a capability boundary; generated
files are local filesystem behavior; debug/host/locale/country and timezone are
pass-through defaults; DOCX/PDF is a capability boundary.
Render
bindings may be written as `[simulator.render_bindings]` (or legacy top-level
`[render-bindings]`), with optional `[render-bindings."poa.docx"]` entries.
`--bind x=clients[1]` overrides a template-specific value, which overrides the
default map. Bindings are evaluated only in the ephemeral render namespace and
never mutate sessions or snapshots.

### A6. Rollout (no migration machinery)

The simulator has only ever run against `docassemble-automatedpleading`, so
no generic migration code is needed:

- Manually move this repo's authored config to `.config/simulator/` and
  delete any legacy runtime state.
- **Pull the leaked `jinja data` back out of the global config**
  (`~/.config/docassemble-simulator/config.yml`) into the package's
  `.config/simulator/config.toml`, then delete (or leave empty) the global
  file.
- Remove legacy runtime-path mentions from `src/`, `README.md`, and the `docs/`
  design docs; point everything at the A2/A3 paths.

## Part B — flow-fidelity fixes

All five were reproduced live against `automatedpleading` while driving the
family interview to a renderable state. The first four are small and each has
a crisp unit test; the second is a spike plus `set`/`describe` support.

### B1. `fresh_user_dict()` drops server `INITIAL_DICT` keys

- **Reproduction**: any `object_radio`/`object_checkboxes` screen crashes with
  `KeyError 'objselections'` — the field-processing path in
  `docassemble.base.parse` writes `_internal['objselections'][saveas] = {...}`
  (parse.py:6471) and the simulator's hand-rolled `_internal` lacks it.
- **Fix**: build `_internal` from `docassemble.base.parse.INITIAL_DICT`
  (parse.py:192) instead of the current subset; keep the simulator's
  `modtime`/`tracker` etc. as-is otherwise. Add the missing `answers`,
  `misc`, `informed`, `objselections` keys.
- **Test**: a fresh session dict contains the same `_internal` keys as
  `INITIAL_DICT`; an object-field screen processes without the KeyError.

### B2. Reference-object fields are unanswerable (`set` + describe)

- **Reproduction**: the party-attorney screen (`object_checkboxes` over
  `firmdata.attorneys`, reference lists in general) dies at field-processing
  time with `NameError: name '<opaque safeid>' is not defined`.
- **Verified root cause (2026-08-25)**: the crash is in docassemble's **own**
  ask-time objselections construction, not the answer path. `firmdata` is a
  `DAGlobal`/`DWGlobal` defined only in `account_*.yml` (out of the main
  flow), so when the flow meets it, docassemble's generic machinery rebuilds
  it as a throwaway container with a random `instanceName`
  (`CdMQNhwvyTHW`, `has_nonrandom_instance_name=False`) whose elements are
  renamed `<random>[i]` on append. At parse.py:6478-6481 docassemble then
  safeids that instanceName as the choice key and execs
  `_internal['objselections'][saveas][key] = <random>[i]` — the container
  `<random>` is never in `user_dict` → `NameError`. The answer-side plumbing
  added by `c2b9e66` (`_apply_object_assignment`, objselections in
  `fresh_user_dict`, selectcompute describe) is correct but unreachable.
- **Server mechanism** (parse.py:6466–6481): before asking, docassemble
  builds `_internal['objselections'][saveas] = {safeid_key: real_expr}` from
  the computed selections; the browser answers with the **safeid keys**, and
  the answer value for `object_checkboxes` is a dict of keys (not a list).
- **Fix (canonical, via the seed contract — A3)**: pre-create the
  server-scoped globals in `config.py` with **stable, non-random
  `instanceName`s** so choice keys are real, evaluable variables
  (`firmdata.attorneys[0]` instead of `<random>[0]`). This prevents the
  lazy container creation entirely. Answer-side support remains as follows:
  1. `describe_choices`: emit the key (`safeid`) plus a human label (done in
     `c2b9e66`; keep `reference_to` as fallback), and
  2. `apply_assignments`: when the target field is an object field, resolve
     the answer through `objselections` (done in `c2b9e66`; accept keys still
     allows `--code` object assignment as an escape hatch).
- **Test**: `set` answers the attorneys screen from its description alone;
  the answer object matches what server `process_selections` produces.

### B3. `MinimalHooks.get_default_timezone()` stubs `""`

- **Reproduction**: the moment any date machinery runs (immediately after the
  last parenting-plan question), assembly dies with
  `ValueError: ZoneInfo keys must be normalized relative paths, got:` — the
  stub returns `""` and `docassemble.base.dates` calls `ZoneInfo("")`.
  The webapp hook reads the configured `timezone` (webapp/config.py:45–50,
  `tzlocal` fallback).
- **Fix**: `MinimalHooks.get_default_timezone` returns the configured
  timezone (the value the simulator merges from
  `.config/simulator/config.toml`, falling back to `tzlocal`, then
  `America/New_York` — webapp parity) instead of `""`.
- **Test**: with `timezone = "America/Chicago"` in the package config,
  `get_default_timezone()` returns `America/Chicago`; a date-using flow pass
  does not raise.

### B4. Gather-button choices describe as `'complete'`, not the real key

- **Reproduction**: the "Gathering x …" continue button's value is dynamic —
  `current_context().variable.split('.')[-1]` — so `describe_choices`
  renders the evaluated fallback (`'complete'`), and `set x.button=complete`
  does **not** advance; the real answer is the sought variable's last segment
  (`there_is_another`). Agents cannot answer gather screens from the
  description alone.
- **Fix**: when describing a screen, set
  `this_thread.current_info['variable'] = <sought/orig_sought>` (the server
  sets this per screen), so dynamic keys evaluate correctly;
  `describe_choices` then renders `{'value': 'there_is_another', …}`.
- **Test**: describe of the gather screen shows `there_is_another`;
  `set x.button=there_is_another` advances using only the description.

### B5. Per-package config is not actually loaded

- **Reproduction**: the CLI help says package configuration is loaded
  automatically`, but `cli.main()` only merges `--config`. The family
  `jinja data` works only because it leaked into the **global** home config
  historically. Adding `timezone:` to the package config had no effect.
- **Fix**: implement Part A's discovery (walk-up TOML candidates, deep-merge,
  root-scoped `.simulator/config-effective.yml` YAML handoff). This makes B3
  testable and removes the historic home-config-reliance in one move.
- **Test**: a package `.config/simulator/config.toml` with `jinja data`/
  `timezone` applies without touching the global config; a malformed
  lower-priority file fails loudly (jda convention).

## Suggested order

1. **A** (layout + discovery + TOML→YAML effective config).
2. **B5** config pipeline + tests; pull the family `jinja data` out of the
   leaked global config into `.config/simulator/config.toml`.
3. **B1** (`INITIAL_DICT`) + **B3** (timezone) — small, independent.
4. **B4** describe-variable context — small, independent.
5. **B2** spike → `describe`/`set` object-field support — the largest;
   needs the B1 fix to be meaningful.
6. Re-run the family-flow drive: `render family_parenting_plan.docx` against a
   real-state session should reach `ok: true` with CRIFS in the artifact
   (validated with `docx validate`).

## Acceptance criteria

1. `.simulator/` is 100% gitignored runtime state; committed per-package
   authored files live under `.config/simulator/` (grep-clean of
   `.simulator`; this repo's files migrated by hand per A5).
2. Config discovery/merge matches jda: walk-up, nearest wins, `local` beats
   committed, global overridable; `.config/simulator/config.toml` takes
   effect without writing to the global config.
3. `timezone`/`jinja data` from the package TOML reach docassemble via the
   YAML effective config.
4. Object-reference fields (attorneys-style) are answerable from the
   description; gather buttons show `there_is_another`; date machinery runs
   without the `ZoneInfo` error; fresh sessions carry full `_internal` keys.
5. Repo test suite green (new unit tests for each item); the family-flow
   drive in `automatedpleading` reaches a real-state render of
   `family_parenting_plan.docx` with the CRIFS include.