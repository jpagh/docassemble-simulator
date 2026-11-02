# Observed JavaScript conditional scope

Read-only investigation by two `openai-codex-work/gpt-6.1-sol` subagents, synthesized by the parent. No interviews, authored scripts, or templates were executed. Source counts are not include-expanded runtime counts or browser compatibility results.

## Scope and provenance

- `/Users/jack/Projects/docassemble-yaml`, revision `638fb49f92ae2c11a322e737705cd235a32fb546`, clean: all 966 checked-in examples and 3 miniature demo-package interviews inspected. Reference/regression fixtures and installed packages were inspected separately. All 2,078 YAML files under `lsp/` were enumerated/scanned; installed copies add no conditional forms.
- `/Users/jack/Projects/docassemble-automatedpleading`, revision `4d71243705b13ad224d13fdf25bb8a3be1c551a9`, clean: all 36 active interview YAML files, one source-data YAML, and eight local archived YAML files inspected. Dependencies and scratch copies excluded from product counts.
- YAML node parsing was cross-checked with whole-file text scans. Demo parse gaps were Jinja and intentionally invalid regression fixtures; every textual condition was reconciled. AutomatedPleading retained eight raw-YAML parse gaps in Jinja-containing chunks of four active/archive documents; full text and local include/macro structure were inspected. Unavailable configuration-selected includes are not covered.

## Result

| Corpus | Show | Hide | Enable | Disable | Total |
| --- | ---: | ---: | ---: | ---: | ---: |
| Demo examples | 9 | 1 | 1 | 0 | 11 |
| AutomatedPleading active interviews | 3 | 0 | 0 | 0 | 3 |
| AutomatedPleading archives | 0 | 0 | 0 | 0 | 0 |
| **Primary total** | **12** | **1** | **1** | **0** | **14** |

The 14 conditions comprise 13 distinct normalized expressions. Every function call is a literal-name `val()` call. None uses methods, JS property access, arithmetic, regex, DOM/jQuery, asynchronous calls, custom helpers, or Mako interpolation inside the condition.

## Exhaustive primary expressions

Demo paths below are relative to `docassemble-yaml/lsp/tests/fixtures/examples/`. Lines identify directive keys.

| File:line | Modifier | Expression |
| --- | --- | --- |
| `country-state.yml:18` | enable | `val('country')` |
| `jsshowif.yml:18` | show | `val("fruit") == "apple"` |
| `jsshowif.yml:21` | hide | `val("fruit") == "mango"` |
| `jsshowif.yml:36` | show | `val('cuisine') == "Chinese food"` |
| `jsshowif.yml:45` | show | `val('watches_tv') == true` |
| `jsshowif.yml:50` | show | `val('listens_to_radio') == true` |
| `jsshowif.yml:57` | show | `val('listens_to_radio') == true` |
| `jsshowif.yml:60` | show | `val('favorite_modulation') == 'FM'` |
| `jsshowif.yml:76` | show | `val("liked_fruits['Apple']") && val("liked_fruits['Plum']")` |
| `jsshowifmaybe.yml:16` | show | `val("likes_mushrooms") == true` |
| `jsshowifmaybe.yml:20` | show | `val("likes_mushrooms") == "None"` |

AutomatedPleading paths are relative to `docassemble/automatedpleading/data/questions/`.

| File:line | Modifier | Expression |
| --- | --- | --- |
| `account_settings.yml:359` | show | `val("firmdata.attorneys[i].signature_yn") && !val("firmdata.attorneys[i].signature_replace")` |
| `x_category_dept_of_revenue.yml:241` | show | `val("M.dept_of_revenue.dlr.address.state") !== "MO"` |
| `x_category_foreclosure.yml:452` | show | `val("M.foreclosure.deed.creditors[i].type") !== "deed"` |

Observed language surface: literal `val` calls, strings, `true`, loose equality `==`, strict inequality `!==`, conjunction `&&`, negation `!`, and bare-value truthiness. Brackets/dots inside field-name strings are addresses, not arbitrary JS member access.

Separate positive LSP regression fixtures add two `=== true` conditions (`regressions/large_valid_interview.yml:36,41`). Negative fixtures test missing/nonliteral dependencies, no dependency, malformed JS, and nonstring YAML values. They are not normal demo examples.

## Field semantics are the main work

1. **Static dependencies, independent of short-circuit evaluation.** Discover every literal call, then intersect with actual on-screen inputs and upstream compiled trigger metadata. An existing namespace variable is not sufficient. No matching input means no evaluation, not false plus hide inversion.
2. **Indexed identities.** AutomatedPleading uses `[i]` names; map these through the rendered question context, not unrestricted Python/JS evaluation.
3. **An absent secondary controller can be valid.** The signature condition's `signature_replace` field is server-conditionally present. `signature_yn` can supply the trigger while absent replacement yields browser `null`; `!null` is true. Do not require every referenced input to exist.
4. **Negative comparisons with unanswered controls.** `null !== "MO"` and `null !== "deed"` are true. Treating missing answers as a false condition would break both examples.
5. **Control-specific values.** Boolean controls, ordinary strings, checkbox-option addresses, and yes/no/maybe's string `"None"` need browser-shaped values. `"None"` is not JS null.
6. **Chains and enablement.** `favorite_antenna` depends on a conditionally shown radio. `country-state` requires enablement distinct from visibility. The latter interview also has asynchronous choice updates, which are not part of its bare `val` predicate.

All primary conditions have source-level same-question dependencies. Actual rendering, hidden/disabled lookup, indexed substitution, and event settling still require runtime tests. The signature example is the explicit caveat to assuming every declared dependency is rendered.

## Other JavaScript exists, but is a separate capability

The demo corpus contains 19 `script:` blocks and six `features.javascript` declarations. Scripts include jQuery events, charts, `getField/getFields/setField`, `action_call`, `get_interview_variables`, timers, `parseInt/parseFloat`, DOM mutations and generated JSON. Referenced helpers include date-input replacement, suggestions, and a custom SSN validator. None is called by the 11 conditions.

AutomatedPleading contains 22 active `script:` blocks (21 distinct bodies), 19 with Mako, five asset declarations referencing six local assets, and seven JavaScript action strings. These implement account/address suggestions, bundle checkbox updates, datepickers, confirmation UI, clipboard copying, navigation, and uploads. Its `dw_uploader.js` is a browser application with asynchronous networking, not a predicate helper. Eight archived YAML files add seven script blocks but no conditions.

Important examples outside condition scope:

- `x_documents.yml:235`: bundle lookup, `forEach`, `JSON.stringify`, and `setField` side effects.
- `upload.yml:300`: `DWFileUploader.start()` and generated configuration.
- `workflow_management.yml:151`: event handlers and `action_perform()`.
- `account_settings.yml:413,573`: generated selector data and field population.

Supporting conditions does not imply supporting these scripts or complete interactive journeys. Scripts can still affect input values in the browser, so browser parity tests must identify script-dependent screens rather than claim all interviews work from predicate coverage alone.

## Recommendation based on the requested representative scope

These corpora do **not** establish a need for PythonMonkey or a general JS engine for #93. A small, explicit evaluator over esprima2 ASTs can cover all 14 observed conditions syntactically, without executing scripts or importing a native engine.

### Required dependency (approved)

Add **`esprima2>=6.0.0` as a direct simulator runtime dependency** in `pyproject.toml` and update `uv.lock`. Import it as `esprima`; do not add the older `esprima` distribution, which shares the import name. Do not borrow the LSP virtual environment or assume docassemble provides it: neither provisioned docassemble lane nor the AutomatedPleading environment currently includes either distribution.

Validate dependency resolution and parser behavior on CPython 3.10 and both supported real-runtime lanes. Installation must be handled by the simulator's normal package dependencies, not a manual prerequisite. This is a required implementation step; the dependency has not yet been added.

### Expression and integration scope

Keep the earlier documented-expression plan as a modest superset: parentheses, `!`, `&&`, `||`, equality/inequality and ordering comparisons, primitive literals, and literal `val`. Ordering/numeric comparisons are required by issue #93's reported count example even though absent from these checkout conditions. Mako-rendered primitive expressions from the supplied documentation remain planned coverage, not observed corpus usage.

Implement source-wide dependency discovery, actual-screen binding, JS primitive semantics, all four modifier policies, explicit unsupported diagnostics, and browser differential tests. Do not promise general JS support. If real conditions later require methods/custom helpers beyond this subset, use collected examples to decide whether to expand or adopt an engine.

The recommendation narrows the earlier hypothetical engine proposal because the user explicitly designated these examples and their main project as the normal scope. It does not assert that arbitrary JavaScript or browser functionality is unnecessary in every interview.
