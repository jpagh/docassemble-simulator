---
status: accepted
---

# Compatibility-matrix dependency alerts are accepted, not patched

`docassemble-base` and `docassemble-webapp` pin their whole dependency tree
with `==`, so the `da19` and `da110` lanes install exactly the versions upstream
chose. Several of those versions carry published advisories. `nltk==3.9.2`
alone accounts for 42 of GitHub's 123 reports, and every `docassemble-base`
release up to and including 1.10.13 pins it. The alerts describe the runtime
being emulated, not a defect the simulator introduced. The alternative —
overriding upstream's pins — would make the real-runtime lane exercise a
dependency set that no docassemble server runs.

## Decision

- Neither lane overrides a docassemble pin, and `da19` keeps its deliberately
  old 1.9.13 fixture even though it carries the most advisories.
- The simulator ships only `pyyaml`, so no advisory present in `uv.lock`
  reaches an installed `docassemble-simulator`.
- Lock refreshes stay deliberate: raise the group specifier or run
  `uv lock --upgrade`, re-run `mise run test:all-da`, then update the matrix in
  `docs/runtime-compatibility.md`. The modern lane moved 1.10.10 to 1.10.13
  this way, with no change in test outcome.
- Alerts are dismissed as tolerable risk pointing at this record, rather than
  left open and re-triaged.
- The residual risk is bounded by what the simulator is. It runs one Interview
  in the operator's own interpreter, and a docassemble Interview already
  executes arbitrary Python through its own `code:` blocks and installed
  modules, so an advisory in the pinned NLP or HTTP stack grants an attacker
  nothing that running an untrusted Interview did not already grant. What is
  left is supply-chain exposure in `uv.lock`, which the published package never
  installs.

## Considered options

Measured against the same 123 advisories. The model reproduces GitHub's count
exactly on the current lock, so the rows are comparable.

| Option | Open advisories |
| --- | --- |
| Current lock | 123 |
| `uv lock --upgrade` | 123 |
| Python 3.12+, drop `da19`, newest docassemble | 61 |
| …plus `override-dependencies` | 1 (unpatched upstream) |

A plain lock upgrade clears nothing: `uv.lock` is universal, so the `da19` and
Python 3.11 branches keep the vulnerable versions resolved in the file even
after the modern lane moves forward. Only deleting a lane or overriding
upstream changes the reported set, and both are the fidelity loss this record
rejects.

## Consequences

- The security tab is not expected to be green. A new advisory against a pinned
  docassemble dependency is normal, not a regression, and is dismissed against
  this record rather than treated as a defect.
- Reading a lane's real dependency set means reading `uv.lock` or that lane's
  interpreter, not the security tab.
- Four advisories have no patched release anywhere upstream, so they cannot be
  resolved without dropping the emulated package entirely.
- The 123 reports are the price of the compatibility matrix, not of a version
  floor: every branch of the lock is load-bearing for a target the project
  intends to support, and each pins its own exact transitive set. Both
  alternatives measured under "Considered options" leave the count unchanged
  for that reason, and marking `da110` as 3.12-only clears nothing either —
  `docassemble-base 1.10.5` pins a subset of what 1.9.13 already pins.
- The declared Python floor is narrower than the range the simulator is for.
  The legacy target is docassemble 1.9.13, which upstream supports on Python
  `>=3.9`, but the package declares `>=3.11` and its own code needs 3.11:
  `tomllib` at 17 sites with no `tomli` fallback, `enum.StrEnum` at 6,
  `typing.Self` at 1. Lowering the floor is a separate decision, and the lock
  is not the obstacle — resolving for 3.10 reuses 1.9.13's pins and adds only a
  `markitdown 0.1.3` branch, so it introduces no new advisory.
