---
status: accepted
---

# Establish an executable AssemblyLine runtime compatibility contract

A docassemble webapp imports installed `docassemble.<package>` modules at
server startup, which is what registers `CustomDataType` classes such as
ALToolbox's `BirthDate`. The simulator did not run that pass, so AssemblyLine
4.8.0's standard birthdate field metadata (`datatype: BirthDate` with
`alMonthLabel` / `alDayLabel` / `alYearLabel`) was parsed as an overwritten
label and failed with `DASourceError` before the first Screen. The same failure
is a target-package blocker: a maintainer cannot tell an authored Interview
defect from a docassemble/AssemblyLine mismatch.

## Decision

- The simulator reproduces the webapp's startup module preload at bootstrap:
  installed package modules that define classes (or opt in with `# pre-load`,
  or call `docassemble.base.util.update`) are imported before any Interview is
  parsed, honoring `preloaded modules`, `module whitelist`, and
  `module blacklist`. Import failures are logged and skipped, as in the webapp.
- Before compiling a target Interview, the simulator compiles an in-memory
  probe containing that standard birthdate metadata with the real docassemble
  compiler. The probe is memoized per process.
- An unsupported pair becomes a `runtime-compatibility` failure carrying the
  runtime family, interpreter, installed versions, failing capability, tested
  matrix, underlying parser error, and recovery direction. It uses the normal
  JSON envelope and human `error:` output with no traceback, and exits `2`
  alongside the compile class.
- The probe never installs, upgrades, downgrades, edits, or rewrites packages,
  and a compatibility failure never creates or mutates Working state or a Saved
  session. Authored parse errors keep the existing compile-error behavior.
- The tested dependency matrix and recovery guidance live in
  `docs/runtime-compatibility.md`; the `da19`/`da110` groups and the
  real-runtime lane provision and exercise it.

## Consequences

- `docassemble-simulator` accepts a new stable failure kind and extends exit
  code `2`'s vocabulary. Automation can branch on
  `error.kind == "runtime-compatibility"` without parsing tracebacks.
- The simulator's startup cost includes importing installed package modules,
  matching the webapp rather than adding a private parser hook.
- A failing probe blocks all catalog/execution commands whenever AssemblyLine
  is installed, so an incompatible environment is identified before the target
  Interview is attempted.
- Compatibility is capability-tested, not version-asserted: a pair passes
  because the real compiler compiled the metadata, not because a version string
  matched.
