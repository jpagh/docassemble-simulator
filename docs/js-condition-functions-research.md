# JavaScript conditional functions: browser scope versus simulator scope

## Recommendation

The request to evaluate **standard JavaScript and docassemble-provided functions** is broader than the tiny interpreter proposed in [the parser research](js-conditions-parser-research.md). Do not grow that interpreter method by method and call it JavaScript support. **esprima2 parses; it does not execute JavaScript.**

Smallest coherent approach for broad *language* semantics: an isolated embedded JS engine, with screen snapshots and explicitly specified docassemble host adapters. Use an AST/dependency pass separately from execution, not a standard-library method allowlist. This supports coercion, string/array methods, callbacks, Math and regex without reimplementing them in Python. It does **not** satisfy unrestricted browser/docassemble compatibility: DOM, jQuery, plugins, event ordering, authored scripts, and server actions require a browser environment. If “all docassemble JS” is literal, choose a real-browser harness with the actual app scripts and, for server operations, a test interview server. That is a materially larger runtime mode, not an engine option.

Research only. No implementation or dependency selection is approved by this note.

## Evidence and reference map

Inspected installed **docassemble 1.9.13 and 1.10.13** in `.venv-da19/lib/python3.14/site-packages/docassemble/` and `.venv-da110/lib/python3.14/site-packages/docassemble/`. Below, `app.js` means `webapp/static/app/app.js` in the **1.10.13 lane**, unless specified. These files differ; do not transfer line numbers between versions. The sibling checkout was `b821998ddd9442f80456ed1649f38afee0a3b877`; its example corpus was also checked.

Official references (live pages inspected): [fields: js show if](https://docassemble.org/docs/fields.html#js%20show%20if), [functions: val](https://docassemble.org/docs/functions.html#val), [getField](https://docassemble.org/docs/functions.html#getField), [getFields](https://docassemble.org/docs/functions.html#getFields), [setField](https://docassemble.org/docs/functions.html#setField), [action_call](https://docassemble.org/docs/functions.html#action_call), [get_interview_variables](https://docassemble.org/docs/functions.html#get_interview_variables). Source navigation: [upstream app.js](https://github.com/jhpyle/docassemble/blob/b821998ddd9442f80456ed1649f38afee0a3b877/docassemble_webapp/docassemble/webapp/static/app/app.js), [parse.py](https://github.com/jhpyle/docassemble/blob/b821998ddd9442f80456ed1649f38afee0a3b877/docassemble_base/docassemble/base/parse.py). Local installed line references below are authoritative for the inspected lanes; the checkout links are not asserted to be identical distributions.

## Trigger invariant — independent of what can execute

**Require a valid literal dependency on an actually rendered on-screen field before evaluation.** Namespace presence alone is insufficient; a rendered unanswered field can still be a dependency. “Rendered” means the input exists on the screen, not necessarily that its wrapper is currently visible. Scan all branches: `true || val("x")` still supplies a dependency. A helper internally reading `val` does not register its caller as a dependency.

* Official fields documentation explicitly requires literal `val("field")`, and illustrates `someCondition && (true || val("variable"))`. This supports the user's **val-only** requirement.
* Actual compiler extraction recognizes **`val`, `getField`, and `daGetField`**: `base/parse.py:4168–4195` (1.9), `3959–3988` (1.10; regex calls at 3971/3985). It is regex extraction, not semantic JS analysis. Whitespace, escaped strings, comments and name suffixes can differ from a clean AST policy. `da_val` is a real callable alias but is not separately named in this regex; the unanchored `val` alternative can nevertheless match its suffix. Do not describe this regex as an exact three-identifier allowlist.
* `app.js:5437–5647` intersects compiled `vars` with rendered DOM names and alternate lookup names; only then installs handlers containing direct `eval(jsExpression)` (5484). Initial manual triggers run at 5911–5924; change/file events and cascading dependent-field changes are also wired. 1.9 equivalents: dependency setup 5396 onward, `eval` 5445, initial triggers 5908 onward.
* No registered trigger means no evaluation. Formatter `base/standardformatter.py:1339–1357` initializes mode-0 show/hide wrappers hidden, but not mode-1 enable/disable wrappers. Therefore no-trigger is **not false plus sign inversion**; preserve baseline state. This is source inference, not a cross-version browser test.

Decision needed: retain the documented/user val-only AST contract, or accept literal `getField`/`daGetField` dependencies for browser fidelity? Either choice must retain actual-screen intersection. Preserve compiled metadata and diagnose extraction discrepancies; do not silently broaden registrations. Evaluate the rendered/Mako-expanded expression, but do not assume that changes the compiler's original dependency extraction.

## Inventory: callable is not equivalent to safe/pure

Direct browser `eval` has no function-name allowlist. It can see globals (and surrounding handler scope). The useful compatibility inventory is therefore by capability, not every internal app helper.

| Actual functions / source | Behavior and simulator category |
| --- | --- |
| `val`, alias `da_val` (1626–1715) | Read adapter, **not** a Python namespace lookup. Calls `daGetField`; missing/disabled/ineligible fields return null. Checkboxes yield booleans, groups arrays, radios null/booleans/strings, object choices decoded names, other inputs generally DOM strings. Preserve these distinctions rather than returning Python numeric values indiscriminately. |
| `getField`, `daGetField` (1342–1421); `getFields`, `daGetFields` (1318–1330) | DOM element/group lookup versus enumerating lookup-map names. `getField` filters disabled/hidden ancestors and handles duplicate fields, groups and optional exclusion container. Returning a plain value is wrong. Element `.value`, `.checked`, `.files`, traversal or jQuery wrapping require a DOM contract. Docs describe group fieldsets; inspected code also returns group DIVs: test actual markup rather than trusting a generic element model. |
| `setField`, `daSetField` (1470–1625); `setChoices`, `daSetChoices` (1422–1469) | DOM mutation and change-event propagation; choice/plugin updates. Not read-only condition helpers. Repeated condition evaluation can repeatedly cause effects/cycles. |
| `atou`, `utoa`, `dabtoa`, `daatob` (1293–1304); `JSON_stringify` (2891–2896) | Encoding utilities reproducible for primitive/JSON inputs, but use browser `atob`/`btoa`, URI conversions and `window.utoa`. `JSON_stringify` additionally escapes non-ASCII code units. Engine globals alone do not necessarily supply browser base64 APIs. |
| `jsonIfObject`, `underscoreToCamel`, `camelToUnderscore` (4219–4243; also present in 1.9 at 4169–4193) | Small data transformations; candidates to run as JS rather than port. Internal globals, not evidence of a stable public API. `jsonIfObject` JSON-encodes objects **and booleans**. |
| `url_action`, `da_url_action` (1798–1822) | Constructs a URL from interview/embed location state and encoded arguments. Does not itself issue a request, but is not context-free; implementation also assigns global `data`. |
| `action_call`, `da_action_call`, `url_action_call` (1823–1868); `action_perform` and its `da_`/`url_` aliases (1869–1906); `action_perform_with_next` and aliases (1907–1951) | Credentialed AJAX/server actions; callbacks, CSRF and interview state. Perform variants update screen/spinner state. Not a safe pure adapter. |
| `get_interview_variables`, `da_get_interview_variables` (1952–1971) | Credentialed asynchronous GET, callback receives variables. Does not synchronously return a variable dictionary. |
| `flash`, `da_flash` (1767–1797), `daShowHelpTab` (1759–1761), `daPost` (210–225), `daGeolocate` (977 onward), `daPreloadImage` (1755–1758) | UI mutation, form navigation, device permission/state or resource loading. Other app globals cover signature canvas, maps, validation, chat/socket and uploads; these are browser/service capabilities, not candidates for a predicate function allowlist. |
| `$`/jQuery; user-defined globals | Available when loaded, but arbitrary DOM/plugin or authored behavior. `daGlobalEval = eval` at 1061, custom datatype JS at 52, and custom scripts at 3126–3130 show actual injection/execution seams. An engine cannot discover/reproduce these from field values alone. |

Async is especially misleading: upstream immediately tests the return value of `eval`; it does **not await** it. A Promise or jqXHR is truthy immediately, not the eventual callback's boolean. Preserve that distinction rather than inventing async predicate semantics.

## Standard JavaScript and observed use

Language-level candidates include `String`, `Number`, `Boolean`, `parseInt`, `parseFloat`, `isNaN`/`Number.isNaN`; string `.trim`, `.toLowerCase`, `.includes`, `.startsWith`, `.slice`, `.split`, `.replace`; array `.includes`, `.indexOf`, `.some`, `.every`, `.map`, `.filter`, `.join`; `.length` (a property); `Math.abs/min/max/round`; regex literals/`RegExp.test`; `JSON.parse/stringify`, object operations, dates and URI encoding. These are **standard capability examples, not claims that all occur in corpus conditionals**. See [ECMAScript built-ins](https://tc39.es/ecma262/#sec-fundamental-objects). Callbacks, Unicode, numeric coercion and regex behavior make a Python method-by-method approximation expensive and unreliable.

Evidence boundaries:

* Installed demo `data/questions/examples/jsshowifmaybe.yml:16–21` uses `val("likes_mushrooms") == true` and `== "None"`; the sibling has the same example and links to the [official recipe](https://docassemble.org/docs/recipes.html#js%20show%20if%20yesnomaybe).
* Installed AssemblyLine 4.8.0 `data/questions/al_visual.yml:266–309` uses literal `val`, `===`, `&&`, `||` and parentheses. These sampled conditionals do not justify claiming regex/Math demand already measured.
* Official fields docs use checkbox-key dependencies, Mako/`json.dumps`, and arbitrary JS expressions. Official functions docs illustrate `getField('dessert').value`, and `parseFloat(val('weight'))` / `parseInt(val('size'))` in a script calling `action_call` (script usage, not a conditional).
* Upstream itself uses `.split/.map/.join/.replace` in the case-conversion helpers and regex/JSON/string operations above. Browser availability is broader than the small observed conditional corpus.

Even standard JS is not uniformly pure: `Math.random`, current `Date`, locale/Intl and timezone depend on environment; regex `lastIndex`, array mutations, getters, callbacks, `eval` and `Function` can change state or run arbitrary code. Browser globals such as `fetch`, timers, `document`, `localStorage`, `navigator` and `atob` are host APIs, not guaranteed by an ECMAScript engine. Fresh contexts, clock/random policy and explicit host capabilities are necessary for reproducibility.

## Runtime choices and safety

1. **AST allowlist interpreter:** light packaging and narrow auditable semantics, but fails the requested broad-language scope. Keep only if the user explicitly chooses a small documented subset. Adding a parser does not resolve JS evaluation fidelity.
2. **Embedded engine + adapters (recommended for broad expressions):** execute standard language semantics in the engine; marshal a screen snapshot into JS and define read adapters there where feasible. Avoid exporting a general Python object bridge. Keep dependency registration separate. Unknown host requirements must produce an explicit unsupported diagnostic, not false. A minimal value-only adapter scope is a product boundary, not “all docassemble JS.” DOM adapters beyond that need an approved observable contract.
3. **Real browser + upstream render/scripts:** smallest *semantic* route to broad DOM/jQuery/authored-script fidelity without rebuilding a browser. Larger operational footprint: browser binaries, HTML/assets, session state, event settling, test server for actions. A static HTML harness alone cannot reproduce authentication/server workflows. Use as a differential oracle even if option 2 is chosen.

Verifiable engine example, **not a dependency recommendation**: [quickjs PyPI metadata](https://pypi.org/pypi/quickjs/json) reports 1.19.4 and CPython 3.10 wheels for x86-64 macOS, Linux and Windows; no 3.10 macOS ARM wheel was listed. `Requires-Python` is empty, so wheel presence is not a general compatibility guarantee. Its [binding README](https://github.com/PetterS/quickjs/blob/master/README.md) now says archived for lack of maintenance; it documents `set_time_limit`, `set_memory_limit`, `set_max_stack_size`, pending-job execution and context thread affinity. This establishes feasibility, **not** suitability for this project's 3.10 and newer lanes. A maintained binding/engine, current platform wheels, licensing and limit behavior require a separate selection spike; nothing was installed or executed here.

Apply source-size/parser-depth limits before AST work, engine CPU/memory/stack limits, and a separately killable worker with wall-clock limits. Regex, loops, callbacks and pending jobs can exhaust resources. A native engine is not a security boundary by itself. Browser mode likewise needs process/container isolation, network/filesystem restrictions and disposable sessions; deny production credentials and external effects by default. Neither parsing nor a host-function name allowlist prevents dynamically constructed code from running in a general engine.

## Product decisions before implementation

* Is the promise broad **ECMAScript + specified screen-read APIs**, or actual browser/docassemble execution including DOM mutations, user scripts and server actions? Recommend the former as an explicit scope choice, browser mode if the latter is required.
* Should `getField`/`daGetField` register dependencies, or retain the user's val-only contract? What happens when AST and upstream compiled regex metadata disagree?
* Are authored helper functions supplied and executed, and are side effects rejected, isolated/discarded, or persisted with upstream event ordering? Do not silently load arbitrary interview scripts into the core simulator.
* Should time/random/locale be fixed, configurable or unsupported? What timeout and unsupported-expression diagnostics are acceptable?

Next step: choose that contract, then prototype an engine adapter and browser differential tests for all four js* modifiers, no-trigger states, value types, duplicate/group fields, cascading events and exceptions in both runtime lanes. This note does not expand the existing implementation issue or approve a particular runtime package.
