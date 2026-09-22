# docassemble / AssemblyLine runtime compatibility

The simulator runs the real docassemble compiler inside the target package's
interpreter. This document is the executable compatibility contract: which
dependency combinations are tested, what the simulator verifies, and what a
failure means.

## Tested matrix

A dependency lower bound is not evidence that every later release works
together. The matrix below is provisioned from the `da19` / `da110` dependency
groups in `pyproject.toml` and exercised by the real-runtime lane
(`tests/test_real_runtime.py`, `mise run test:all-da`).

| Runtime family | docassemble-base | docassemble-webapp | docassemble.AssemblyLine | docassemble.ALToolbox |
| --- | --- | --- | --- | --- |
| legacy 1.9.x | 1.9.13 | 1.9.13 | 4.8.0 | 0.19.0 |
| modern 1.10.x | 1.10.10 | 1.10.10 | 4.8.0 | 0.19.0 |

Both lanes run on CPython 3.12+; the AssemblyLine release requires it. The
`da19` / `da110` groups attach the AssemblyLine packages only on Python 3.12+
for that reason. Anything outside a tested row is unclaimed: the simulator
reports the installed pair instead of guessing.

The matrix deliberately keeps a family split. `docassemble.AssemblyLine 4.8.0`
reaches `docassemble.ALToolbox 0.19.0`, whose `markitdown[pdf]` chain needs a
`pdfminer-six` release newer than the exact pin in `docassemble-base 1.9.13`;
the lock keeps the legacy lane installable by selecting a compatible
`markitdown` and constraining `onnxruntime`. Reproduce the lanes with:

```sh
mise run sync:da19
mise run sync:da110
mise run test:all-da
```

## What the simulator verifies

At bootstrap the simulator mirrors the docassemble webapp's startup import
pass: installed `docassemble.<package>` modules that define classes (or opt in
with `# pre-load`, or call `docassemble.base.util.update`) are imported before
any interview is parsed. That pass is what registers custom datatypes such as
ALToolbox's `BirthDate`. It honors the effective `preloaded modules`, `module
whitelist`, and `module blacklist` configuration and skips `# do not pre-load`
modules exactly as the webapp does. Import failures are logged and skipped, not
fatal.

Before compiling the target Interview, the simulator compiles an in-memory
probe containing the standard AssemblyLine birthdate field metadata:

```yaml
fields:
  - Birthdate: simulator_probe_birthdate
    datatype: BirthDate
    alMonthLabel: ${ word('Month') }
    alDayLabel: ${ word('Day') }
    alYearLabel: ${ word('Year') }
```

The probe runs only when an AssemblyLine/ALToolbox distribution is installed in
the target interpreter. It never resolves a package file, installs, upgrades,
downgrades, or edits anything, and it never creates Working state or a Saved
session. Its result is memoized per process.

You can inspect the runtime family, installed versions, and tested matrix with
`sim info --json` (the `runtime_compatibility` object). The probe itself runs on
`check`, `questions`, `index`, and every execution command.

## Session persistence

AssemblyLine's baseline includes `al_saved_sessions.yml`, whose `initial: True`
block writes session metadata through `update_session_metadata`. That function
is PostgreSQL-only (`pg_advisory_xact_lock`, `jsonb`, `||`, `CAST(... AS
jsonb)`), and docassemble itself does not support SQLite for its server
database. The simulator therefore substitutes no database at all:

- the simulator's generated defaults contain no `db` section, so nothing
  silently points at a nonexistent PostgreSQL server;
- `assembly line.update session metadata` defaults to `false`, matching a local
  run that has no server session store. The CLI's own saved state remains
  `.simulator/sessions/`;
- the AssemblyLine initial block, saved answer sets, and the interview list are
  deployment-only features. The simulator does not rewrite installed
  AssemblyLine functions to make them work locally.

A project can opt back in when it has a real docassemble database:

```toml
# .config/simulator/config.toml
["assembly line"]
"update session metadata" = true

[db]
prefix = "postgresql+psycopg2://"
name = "docassemble"
user = "docassemble"
password = "..."
host = "localhost"
port = 5432
```

Point this at an existing docassemble database (the `jsonstorage` table must
already exist); the simulator does not create schemas. Without that database,
leaving the default off keeps complete main journeys — start, questions,
documents, downloads — free of database access.

## Failure taxonomy

| Condition | Command result | What to do |
| --- | --- | --- |
| Missing `docassemble-base`/`webapp` | `input` error, exit 1 | Let preflight acquire them, or install into the target interpreter; `--offline` refuses acquisition |
| Installed runtime fails to import (native library, broken dep) | `input` error, exit 1 naming the native-library boundary | Fix the interpreter's native libraries; this is not a missing-package problem |
| Missing include (`docassemble.AssemblyLine:...`) | `compile` error, exit 2 with the definition and include path | Fix the authored include or install a package that provides it |
| Authored Interview syntax error | `compile` error, exit 2 | Fix the Interview definition; the simulator does not downgrade or rewrite it |
| Unsupported docassemble/AssemblyLine pair | `runtime-compatibility` error, exit 2 | Provision a tested pair, or run in a target interpreter that has one |

A `runtime-compatibility` failure carries the runtime family, interpreter,
installed package versions, failing capability, the tested matrix, the
underlying parser error, and a recovery direction. It is emitted as the normal
JSON envelope and as human-readable `error:` / detail lines; no traceback is
part of the contract. A compatibility failure never creates or mutates a Saved
session.

The probe specifically does not reclassify authored Interview errors: once the
probe passes, an authored parse error keeps its `compile` result. Missing
includes, missing core runtime packages, and native-library import failures keep
their own distinct messages.

## Recovery

Compatibility failures are environment problems, not Interview defects. Do not
edit `site-packages`, fork the parser, or strip `alMonthLabel` / `alDayLabel` /
`alYearLabel` metadata. Instead:

1. Install a tested pair into the target interpreter, for example
   `docassemble-assemblyline==4.8.*` with `docassemble-ALToolbox>=0.19,<0.20`,
   using the same interpreter that runs the simulator.
2. Re-run `sim check --json`. A compatible environment compiles the probe,
   compiles the target's AssemblyLine include, and `sim start` reaches the first
   Screen outcome.
3. If the pair is still rejected, capture the `runtime-compatibility` error;
   it names the exact runtime family, versions, and failing capability so the
   incompatibility can be reported upstream with a minimal reproduction.
