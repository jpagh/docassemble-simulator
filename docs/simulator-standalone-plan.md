# Standalone simulator and jda #2 resolution

Status: proposed; no implementation or issue changes made.

Source: https://github.com/jpagh/jda/issues/2

## Decision

Run docassemble-simulator inside the target interview package's Python environment, with that package and its dependencies installed. Remove jda's in-process simulator integration rather than pretending a globally isolated jda tool has access to project dependencies.

Keep `docassemble-simulator` as the canonical command and add `sim` as a console-script alias to the same `docassemble_simulator.cli:main` entry point. `sim` is optional convenience: its generic name may collide, so all documentation must retain the canonical command as a fallback. This is a project-installed CLI, not a requirement to use `uv tool install`.

## 1. Re-scope the issue before implementation

Add a decision comment to jda #2 explaining the replacement contract. The original requirements to retain `jda sim`/`jda simulator`, bundle a runtime in a jda extra, and run the simulator on every jda-supported interpreter are intentionally superseded, not implemented.

Create linked simulator issues for any remaining runtime/acceptance work below. Inventory existing tests before creating duplicate work: this repo already documents legacy 1.9.8+ and modern runtime support, missing-runtime acquisition, actionable compatibility errors, and isolated cross-family test environments. These are progress, not proof that the original AssemblyLine interview works.

## 2. Add and verify the standalone alias

In this repo:

- Add `sim = "docassemble_simulator.cli:main"` to `pyproject.toml`; retain the existing entry point.
- Build and install the distribution in a disposable environment. Test both installed scripts, not just direct calls to `main`.
- Assert equivalent help, argument handling, exit codes, and JSON envelopes. Help must work without docassemble installed and must not install runtime packages.
- Document project-environment installation and invocation, including an explicit interpreter command for non-uv projects. For uv-managed packages, show adding the simulator as a development dependency and using `uv run sim`; explain that the project/package dependencies must also be installed.
- State that a global tool environment is not the target package environment. Document native dependencies, supported runtime families, offline behavior, and the DOCX/PDF boundary.

## 3. Remove the jda integration

In jpagh/jda:

- Remove `simulator` from optional dependencies and remove the simulator dependency from `all`; retain `all` as the remaining integrations aggregate, currently LSP only.
- Remove the simulator proxy module, both command registrations, simulator-specific help/manual/capability entries, and forwarding tests.
- Update installation docs and add a breaking-change migration note: install the simulator in the interview project's environment, then replace `jda sim ...` or `jda simulator ...` with `uv run sim ...` or that environment's `docassemble-simulator ...`.
- Refresh the lockfile and test that base jda and its remaining extras neither install nor advertise the simulator. Unknown removed commands should receive the normal CLI error; do not retain a hidden runtime proxy.
- Run the full jda suite and its configured lint/type/format checks.

Publish the simulator alias before the jda removal release. Existing canonical-command users remain unaffected.

## 4. Establish the runtime acceptance contract

Use installed standalone scripts in a disposable target-project environment as the highest useful seam, replacing the original `jda sim` seam.

### Successful environments

- Install a minimal interview package plus a separate include-providing package with real package metadata; do not merge YAML sources.
- Exercise `check`, `start`, `answer`, and `exec` along a deterministic path. Verify included definitions are registered consistently and do not gain duplicate labels.
- Add a representative real AssemblyLine-dependent fixture, with a recorded compatible dependency set. The original report used AssemblyLine 4.8.0; verify a compatible combination rather than assuming that version supports every runtime/interpreter.
- Exercise DOCX generation at the supported simulator boundary and verify that requested PDF conversion receives the documented deployment-only diagnostic.
- Run the core contract against the supported legacy and modern runtime families. Test AssemblyLine only in combinations explicitly established as supported.
- Record fixture Python/package versions and reproducible provisioning commands. Project environments own runtime constraints; do not introduce a universal bundled runtime or impose a test fixture's pins on every user.

### Failed environments

Test missing base/webapp packages, an installed but incomplete runtime, unsupported runtime versions, missing include packages, and native-library import failures. Use offline mode for deterministic missing-package cases; separately test documented acquisition behavior without silently upgrading existing runtime packages.

For each failure verify:

- An environment failure is distinguishable from an authored interview error.
- The message names the failing component and supplies a recovery command targeting the same interpreter, or appropriate native-library guidance.
- Runtime incompatibility is caught before interview processing where possible; missing includes receive a useful diagnostic when resolved.
- JSON preserves the existing success/error contract and exit status, rather than a primary opaque traceback.
- Diagnostics do not expose credentials; use sentinel secrets in tests.

Preserve server independence: these tests must not need or mutate a Server or Playground.

## 5. Close with evidence, not installation metadata

Track every original #2 requirement as already verified, completed by this work, explicitly superseded by the project-environment decision, or transferred to a linked simulator issue.

Prefer keeping #2 open until the removal/migration release and the representative standalone acceptance path pass. If it is closed earlier as a jda integration retirement, explicitly say runtime follow-ups remain open and link them; do not describe the entire reported runtime problem as fixed.

Final resolution should link the jda removal, simulator alias release, acceptance tests and tested version combinations, plus any remaining interview-specific limitations. No claim is made that arbitrary runtime/package combinations or local PDF conversion are supported.
