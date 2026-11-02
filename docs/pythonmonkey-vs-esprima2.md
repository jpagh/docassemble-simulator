# PythonMonkey versus esprima2 for issue #93

Research snapshot: **2026-11-01**. Research only; no dependency or implementation selection is approved. Read alongside [parser research](js-conditions-parser-research.md) and [function-scope research](js-condition-functions-research.md). Version-specific distribution contents below take precedence over moving README claims.

## Recommendation and decision table

**Do not adopt PythonMonkey 1.4.1 as an in-process, isolated condition evaluator.** It is a credible broad-JavaScript execution candidate, not a replacement for esprima2's dependency AST. Its current packaging requires Node/npm at installation, its normal import exposes Python execution to JavaScript, and its public API does not provide independent contexts or execution budgets. These are substantive deployment and isolation blockers, not reasons to implement JavaScript methods in Python instead.

**Go:** a small, disposable-process selection spike, if Node-at-build/install and explicit OS isolation are acceptable. **No-go:** production integration pending that spike and scope decisions. If the requirement is literally no Node dependency even during ordinary installation, the published dependency chain already fails it. A prebuilt, audited internal artifact could change deployment, but is extra packaging ownership, not a property of `pip install pythonmonkey`.

| Choice | Execution and discovery | Fit / decision |
| --- | --- | --- |
| esprima2 alone | AST parsing only; no execution, host APIs or JS coercion | Go for syntax/dependency discovery; **not** a broad-language evaluator |
| esprima2 + tiny evaluator | Implements only explicitly approved AST subset | Appropriate only if user chooses narrow semantics; fails broad standard-functions request |
| PythonMonkey alone | SpiderMonkey execution; no supported public AST extraction found | No-go for dependency discovery by observing calls; short-circuiting misses dependencies |
| esprima2 + PythonMonkey in existing simulator worker | Separate AST discovery and broad engine semantics | No-go as a fresh-state/sandbox design: shared context, Python bridge, native hangs/crashes affect interview worker |
| esprima2 + PythonMonkey in disposable restricted subprocess | Broad ECMAScript plus specified snapshot adapters; bounded parent wait | Conditional spike candidate; costs startup/IPC/RSS, OS restrictions and npm packaging |
| Browser with actual app scripts | DOM/jQuery/events/authored helpers; server needed for actions | Required if promise is unrestricted browser/docassemble behavior; larger separate mode |

Engine support does **not** supply `val`, docassemble helpers, DOM, jQuery or server actions. Product decisions in the previous research (helper scope, side effects, deterministic time/random, trigger differences) remain open; this note does not resolve them silently.

## Distribution, platforms and deployment

Primary metadata: [PythonMonkey 1.4.1 JSON][pm-meta], [esprima2 6.0.0 JSON][es-meta], [pminit 1.4.1 JSON][pminit-meta]. Latest versions were also checked against the unversioned PyPI JSON endpoints.

| Property | PythonMonkey 1.4.1 | esprima2 6.0.0 |
| --- | --- | --- |
| `Requires-Python` | `>=3.8,<4.0` | `>=3.10` |
| Published wheels | 40 interpreter/platform-specific wheels + sdist | `py3-none-any` wheel + sdist |
| CPython versions with wheels | 3.8, 3.9, **3.10, 3.11, 3.12, 3.13, 3.14**, 3.15 | Universal Python 3 tag; upstream CI explicitly tests 3.10–3.14 |
| Each CPython version above | macOS 11+ arm64 and x86-64; Linux `manylinux_2_31` aarch64 and x86-64; Windows `win_amd64` | Pure Python; no architecture-specific binary requirement |
| Not in PM wheel inventory | musllinux/Alpine, Windows ARM64/32-bit, Linux other architectures; no `cp314t` free-threaded wheel | Universal tag is not proof of every interpreter's runtime behavior |
| Compressed wheel sizes | About 13.0–22.4 MB, platform-dependent | 76,031 bytes |
| Direct Python dependencies | `aiohttp>=3.9.5,<4.0.0`, `pminit>=0.4.0`, Windows-only `pyreadline3>=3.4.1,<4.0.0` | None |
| Distribution license | MIT binding; embedded engine and npm dependencies need separate accounting | BSD-2-Clause |

These are **artifact availability**, not installation/conformance results on those platforms. CPython 3.10 and 3.14 have actual matching wheels, not just a permissive metadata claim. Linux requires the wheel's glibc baseline (2.31+) and matching architecture. No blanket promise follows for all future versions permitted by the project's `>=3.10` requirement.

The downloaded `cp314-cp314-macosx_11_0_arm64` wheel is 14,780,227 bytes compressed, 42,930,664 bytes summed uncompressed entries (not RSS), SHA-256 `431a25caa831bc691494dd8677ead697baddc47ccc60da01c4b84c95148b91f8`. It bundles `libmozjs-136a1.dylib`, but **zero `node_modules` entries**. Source `mozcentral.version` pins `6bca861985ba51920c1cacc21986af01c51bd690`; do not describe this release as executing the latest Firefox engine merely because the README says “latest”. No test262 completeness or latest-ECMAScript conformance was established.

### The easy-to-miss npm dependency

The release README explicitly says npm/Node is required during installation. This was independently verified in **pminit 1.4.1's published sdist**, not merely inferred from documentation:

* PyPI publishes only an 8,352-byte sdist for that pminit release, no wheel.
* Its Poetry build hook `post-install-hook.py` checks `shutil.which('npm')`, fails if absent, then executes `cd pythonmonkey && npm i --no-package-lock`.
* `pythonmonkey/package.json` requests `core-js ^3.35.1`, `ctx-module ^1.0.15`, and `events ^3.3.0`. The included lock lists 57 package entries including the root, but the command is **not `npm ci`**. Do not assume an immutable transitive JS set; verify the actual built artifact. Installation can involve npm registry access and lifecycle scripts.
* pminit's build dependencies include `poetry-core>=1.1.1` and `poetry-dynamic-versioning==1.10.1`. It builds a wheel containing the JS dependencies under PythonMonkey's package directory.

Thus the PM native wheel avoids compiling SpiderMonkey, **not** the pminit build/install step. No Node executable or browser is needed to run already-populated JS modules under SpiderMonkey according to the documented design; a clean Node-free runtime image still needs verification. Offline installation requires prebuilt/cache-complete artifacts, not only the PythonMonkey wheel. Do not bypass dependencies or import internals and call that the supported public installation.

Python transitive cost also exceeds the two direct package names: current [aiohttp 3.14.5 metadata][aio-meta] lists aiohappyeyeballs, aiosignal, attrs, frozenlist, multidict, propcache, yarl, plus async-timeout on Python <3.11 and typing_extensions on Python <3.13 (and their own dependencies). Optional speedups are not requested by PM. This is an illustrative current closure layer, **not a resolved simulator lock**: existing docassemble pins can select different aiohttp versions or conflict. Resolve separately against both target groups before approval.

When a matching PM wheel is unavailable, the [release README / build sources][pm-src] list bash, CMake, LLVM, Rust, Python headers, SpiderMonkey, npm, Poetry and its versioning plugin; Doxygen/Graphviz are documentation-only. This is not a reasonable silent fallback for a lightweight CLI dependency. Do not run upstream setup scripts merely to inspect them.

Licensing: PythonMonkey/pminit advertise MIT; esprima2 BSD-2-Clause requires retaining notices. SpiderMonkey is Mozilla code subject to [MPL 2.0 obligations][mpl] (file-level copyleft/source availability for covered distributed code, not automatic relicensing of this MIT application). The inspected wheel has the binding LICENSE entry, not an established complete third-party notice inventory. Verify engine source/notice compliance and the actual npm/Python dependency license inventory before redistributing binaries. This is an unresolved packaging audit, not a legal certification.

Maintenance evidence: PM PyPI releases include 1.3.0 (2025-09-25), 1.3.2 (2026-06-16), 1.4.1 (2026-10-16). README calls it MVP since September 2024 and maintained by Distributive. Current issues/PRs indicate activity, not a security SLA. [PR #509][pm-509] proposes a newer SpiderMonkey rebuild; it is not evidence that 1.4.1 includes it. esprima2 releases include 5.0.0 (2025-07-29), 5.0.2 (2026-04-21), 6.0.0 (2026-04-22); its [official source and CI][es-src] support modern syntax claims and 3.10–3.14 tests, not evaluator support or a security SLA. Its distribution imports as `esprima`; avoid coexistence with the older same-import-name distribution.

## API, execution and isolation evidence

The following is verified from **1.4.1 sdist files**, accessible via [versioned PyPI artifacts][pm-meta]; [official docs][pm-docs] and [repository][pm-src] are navigation aids and can move.

* `python/pythonmonkey/pythonmonkey.pyi` exposes `eval(code, options)`, `require`, `globalThis`, `isCompilableUnit`, `collect`, `wait`, `stop`, proxies and conversions. `eval` evaluates a **script**, returns its last expression, and supports filename/line/column, strict/module flags, etc. It is not an expression-only validator. Require a parsed single expression before wrapping it; parenthesization is not a security boundary. Compute the final truth value **in JS**, not Python `bool(result)` (empty arrays/objects are truthy in JS).
* Standard arrays/strings/callbacks, coercion, Math, JSON, regex, Date, collections, BigInt, Promise, Intl and other SpiderMonkey built-ins are available by documented design. The engine handles semantics the tiny evaluator would otherwise have to implement. Language feature availability and Python conversion support differ: `src/pyTypeFactory.cc` explicitly rejects Symbol conversion although Symbol is a JS built-in. Keep evaluation/result coercion inside JS and return a Boolean/diagnostic, not arbitrary objects.
* `src/modules/pythonmonkey/pythonmonkey.cc` creates `GLOBAL_CX` and a global object at initialization. It also creates a debugger global; that is **not** a user-facing independent-context factory. `require.py` explicitly says it reuses the global context, including its `vm.runInContext` implementation. CommonJS modules are cached singletons. Repeated `pm.eval`, an IIFE or a new `require` function does not reset globals, prototypes, lexical bindings or module state.
* Normal `__init__.py` imports `.require` and eagerly loads console, base64, timers, URL and XMLHttpRequest modules. `require.py` populates `globalThis.python.eval`, `.exec`, `.getenv`, `.paths`, output functions, plus `pmEval` and bootstrap capabilities. `.py` module loading is supported. **The default environment deliberately exposes Python and filesystem/network-related host capabilities**; refraining from passing our own callback does not remove them.
* [Open sandbox request #207][pm-207] explicitly describes Python/XHR/bootstrap exposure; [open context request #208][pm-208] asks for separate execution contexts. These corroborate source findings, but issue descriptions are not an escape audit. Deleting a few global names or hiding identifiers in a closure is not a demonstrated sandbox. Neither AST function-name filtering nor blocking direct `python` references prevents dynamic access in general JS.

**Freshness:** a new OS process with a newly initialized interpreter is the straightforward supported reset boundary identified here. Do not fork an already-imported native engine and assume correctness; spawn/exec before importing PM. Reusing a child between conditions trades away demonstrated freshness. Fresh processes still do not fix Date, random, locale/timezone, filesystem/network inputs or version drift. A deterministic contract needs approved clock/random policy, pinned artifacts, fixed locale/timezone and no ambient host inputs. Do not claim that replacing `Date.now` alone covers all nondeterminism.

### Limits, async and safety

No timeout/interrupt/memory/stack-limit option or context constructor was found in the public stub/README. Searching binding sources found no synchronous execution interrupt integration. Native initialization sets `JSGC_MAX_BYTES` to `(uint32_t)-1`; this is not a useful configurable per-expression memory budget. SpiderMonkey's underlying embedding APIs are not automatically available through PythonMonkey. `collect()` is GC, not a quota. `stop()` stops pending asynchronous jobs and unblocks `wait()`, **not** a documented way to interrupt `while (true) {}` or a pathological regex. This is source/API evidence, not a measured infinite-loop test.

Promise/awaitable bridging and timers depend on a running Python event loop. `wait()` awaits outstanding async work; never automatically call it on arbitrary predicates. The browser evaluates the conditional's immediate result, without awaiting Promises; a Promise is immediately truthy. Decide explicitly whether async-containing predicates are unsupported or preserve immediate truthiness while discarding effects. Evaluate Boolean conversion inside JS to avoid unnecessarily exporting a Promise. Scheduling jobs may itself require an event loop; this path needs testing, not assumptions. [Issue #507][pm-507] reports Windows 3.13/PM **1.3.1** loop detection failures; it is a reported compatibility risk, not proof that 1.4.1 fails.

A parent-enforced wall-clock deadline with terminate → short grace → kill → reap can stop a native hang. For general expressions, isolate the evaluator from interview state and credentials, cap input/output, and apply CPU/address-space/process limits appropriate to the OS (Linux cgroups/rlimits, Windows job objects or equivalent). Network/filesystem restrictions need an OS sandbox/container policy; **a killable subprocess alone does not prevent exfiltration or file modification via the Python bridge**. Limit/clean up descendant processes too. Cross-platform isolation is a go/no-go deliverable, not something this research established. For trusted authored code, state that trust assumption explicitly rather than advertising a sandbox.

There is no browser DOM/window/document implementation established by PM. It does supply some host conveniences (XHR, timers, URL, base64); their existence increases capabilities, not docassemble fidelity. `getField`, jQuery, plugins, user scripts, action calls and DOM change cascades still need an explicit adapter or browser mode.

## `val` and dependency discovery stay separate

Retain the issue's invariant: a literal `val("field")` dependency must intersect **actually rendered input identities**; namespace membership is insufficient. Scan the complete AST before execution: `true || val("x")` and `false && val("x")` still have dependencies. Instrumenting `val` during evaluation cannot discover unreachable calls. Constant expressions/no matching trigger mean **no evaluation**, not false followed by show/hide sign inversion. Preserve compiled trigger metadata discrepancies already documented in the earlier research; do not silently expand val-only policy to `getField`.

PythonMonkey's public API exposes no AST parse result. `isCompilableUnit` is a compilability hint, not dependency metadata. Recommended complement: esprima2 `parseScript(..., tolerant=False)`, exactly one `ExpressionStatement`, then full AST traversal for direct one-string-literal `val` calls and source/AST budgets. Pin/test the intersection of parser syntax and engine syntax; a newer parser can accept a feature the bundled SpiderMonkey does not. Alternative: a vendored JS AST parser executing inside the engine, with separate dependency/licensing/resource costs. Neither a regex alone nor runtime call tracing is an equivalent replacement. Keep unsupported syntax/dependency/host errors separate from a false predicate.

Adapter recommendation (design to validate, not implemented): transmit a bounded JSON screen-value snapshot and define `val` in JS over JS-owned data. Do not expose the live interview dictionary or Python objects; PM proxies can share mutable backing data. JSON must be passed as data (e.g. a string argument to a fixed bootstrap using `JSON.parse`), not concatenated into executable code. Restrict serialization to browser-shaped values, reject nonfinite/unsupported values, and handle missing keys without inherited-property lookup.

Source `src/jsTypeFactory.cc` maps **Python `None` to JS `undefined`**, while `pm.null` maps to JS `null`; `src/pyTypeFactory.cc` reverses these mappings. A Python `val` callback returning `None` for browser-null is therefore wrong. JSON null becomes actual JS null; if undefined is genuinely required, use an explicit tagged/omitted-value contract, not accidental conversion. Undefined/missing is also distinct from a rendered unanswered input. JS numbers return Python floats; Python ints normally become JS numbers (precision risk above 2^53), with separate BigInt support. Strings, booleans, arrays and null must follow the **rendered control**, not Python's stored datatype.

Validate against the installed browser `val` behavior documented in the companion research: ordinary input values usually strings (including numeric-looking text), checkboxes booleans, groups arrays, radios null/boolean/string, object choices decoded identities, missing/disabled/ineligible inputs null. Visibility, duplicates, hidden ancestors and dynamic dependent fields affect these values. Snapshotting must specify when those facts are captured; no claim of full browser parity follows from correct engine coercion. Never use Python `==` or `bool` to stand in for JS equality/truthiness.

## Fit with existing simulator workers and tests

Local seams inspected: [`pyproject.toml`](../pyproject.toml), [`.mise.toml`](../.mise.toml), [`worker.py`](../src/docassemble_simulator/worker.py), [`runner.py`](../src/docassemble_simulator/runner.py). Runtime deps today are PyYAML and conditional tomli. Package supports Python >=3.10; `test:py310` provisions `.venv-310`, and `test:runtime` includes that plus isolated da19/da110 lanes. Existing research and local lane layouts use 3.14 for the real runtimes. A universal-looking metadata range cannot substitute for testing both 3.10 and 3.14 and resolving each mutually exclusive runtime group.

`SimulatorRunner.open()` starts a persistent target-interpreter subprocess; request transport has a parent deadline, and termination escalates to kill. `worker._run_deadline` uses main-thread SIGALRM/setitimer. Do **not** assume Python signal handling interrupts a native JS call promptly. In-process PM import adds native engine/module initialization once but leaves JS state across operations and lets a fault kill the interview worker. Runner-level termination is a last resort, not expression-local recovery or an OS capability sandbox; direct worker/API usage also cannot rely on that parent being present.

A separate evaluator child adds process startup/import, serialization, and native-engine RSS. Spawning once per condition could be disproportionately expensive on screens with many conditions and visibility settling passes. Spawning once per screen/pass amortizes cost but needs an explicit state-sharing contract; pooling is not automatically fresh. Prefer measuring a fresh child per evaluation first, then assess approved batching, rather than silently weakening isolation. No latency/RSS measurements were taken; compressed wheel size is not memory usage. A child can avoid importing docassemble and receive only a snapshot, so it need not duplicate interview initialization. Its lifecycle and descendant cleanup must integrate with worker shutdown/timeouts and all entry points, not only the runner convenience API.

## Small selection spike and approval gates

Time-box to an isolated throwaway environment/branch, **not** a dependency change to #93. Use only authored bounded fixtures; no arbitrary interview scripts. Record exact artifact hashes, OS/architecture, Python version, and resolution output.

1. **Packaging gate:** build/install locked PM 1.4.1 + pminit artifacts in disposable CPython 3.10 and 3.14 environments on macOS arm64 and Linux x86-64; include Windows amd64 if supported deployment is promised. Resolve against da19 and da110 separately. Demonstrate installation failure without npm, controlled build with npm, and runtime startup without Node/network. Inventory resulting npm packages/licenses and measure installed size. If Node-at-install is forbidden, stop here unless a separately approved binary redistribution plan exists.
2. **Semantics gate:** esprima2 dependency pass plus JS-owned JSON snapshot adapter. Test #93 count swapping; `===`/coercion; strings/arrays/callbacks/regex/Math; null versus undefined; numeric strings; checkboxes/groups/objects; short-circuit dependency discovery; no-trigger show/hide baselines; malformed and engine-unsupported syntax. Compare a small fixture set to actual app.js in both runtime versions using a browser oracle. Do not infer all helpers from these tests.
3. **Isolation/reproducibility gate:** two sequential evaluations that mutate a global and `Array.prototype`; show a fresh child resets both. Check default Python/XHR exposure using benign presence checks, not host exploitation. Demonstrate filesystem/network denial in the proposed OS envelope, no credential/environment leakage, deterministic approved clock/random policy, and no live Python object sharing. If this cannot be demonstrated, restrict to explicitly trusted code or reject the candidate.
4. **Resource/recovery gate:** in externally supervised children only, bounded fixtures for an infinite loop, expensive regex, controlled allocation, Promise/timer scheduling and exceptions. Enforce wall deadline and memory limit; kill/reap; verify no orphan jobs/processes, bounded output and an intact simulator worker/session. Verify immediate Promise truthiness or explicit unsupported diagnostics, never accidental awaiting. `stop()` is not the synchronous-timeout test.
5. **Cost gate:** record cold/warm import time, per-condition latency and peak RSS for 1/10/100 simple conditions, fresh-process and explicitly scoped screen-batch variants. Include worker startup and snapshot IPC, not only `pm.eval` microbenchmarks. Set acceptable budgets with the project owner before choosing reuse. Then run fast, py310 and both real-runtime lanes for any proposed integration.

Selection outcome should be a measured table of these gates, not “SpiderMonkey runs JavaScript”. **Current blockers:** Node/npm installation policy, default Python/host exposure, no public fresh contexts or per-evaluation limits, unmeasured child overhead, unresolved complete dependency/license closure, and unverified browser adapter parity. Engine breadth and actual 3.10/3.14 wheels are positives, but do not cancel those blockers. If rejected, investigate another maintained engine binding with appropriate context/limit/packaging APIs; esprima2 remains useful and is still not an execution alternative.

## Verification scope and sources

Commands used read local research/config/worker code, fetched PyPI JSON and official sources/issues with `curl`, downloaded/extracted published sdists under `/tmp/pm-research`, and inspected one wheel with Python `zipfile` (including SHA-256 comparison to metadata). **No package installation, upstream setup/build execution, native import, JS execution, benchmark, or browser probe was performed.** Platform/runtime and isolation conclusions are therefore metadata/source-based; pending behavioral checks are called out above. No project code, lock, issues, or pre-existing research/audit files were edited.

[pm-meta]: https://pypi.org/pypi/pythonmonkey/1.4.1/json
[es-meta]: https://pypi.org/pypi/esprima2/6.0.0/json
[pminit-meta]: https://pypi.org/pypi/pminit/1.4.1/json
[aio-meta]: https://pypi.org/pypi/aiohttp/3.14.5/json
[pm-docs]: https://docs.pythonmonkey.io/
[pm-src]: https://github.com/Distributive-Network/PythonMonkey
[es-src]: https://github.com/s0md3v/esprima2/tree/10e784b7ee903b81a430bc0f98f6fd92db8e700b
[mpl]: https://www.mozilla.org/en-US/MPL/2.0/FAQ/
[pm-207]: https://github.com/Distributive-Network/PythonMonkey/issues/207
[pm-208]: https://github.com/Distributive-Network/PythonMonkey/issues/208
[pm-507]: https://github.com/Distributive-Network/PythonMonkey/issues/507
[pm-509]: https://github.com/Distributive-Network/PythonMonkey/pull/509
