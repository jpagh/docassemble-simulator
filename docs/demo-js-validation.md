# Demo JavaScript validation results

## Verdict

**All 11 demo `js show/hide/enable if` expressions passed the tested behavioral
scenarios on both supported runtime families. General JavaScript demos are not
fully supported:** the simulator does not execute authored scripts, external JS
assets, DOM events, AJAX callbacks, or client-side check-in requests.

Startup success must not be read as browser-feature success.

AutomatedPleading was excluded; another agent is validating that project.
No simulator production code, corpus expectations, or external interview sources
were changed during this validation.

## Provenance and scope

- Simulator baseline: `7d99f08431d36ba30a74e5de5c43f02244e37685`.
- External checkout: `../docassemble-yaml`, revision
  `638fb49f92ae2c11a322e737705cd235a32fb546`, clean during validation.
- Scanned all 966 `lsp/tests/fixtures/examples/*.yml` files. Selected files
  containing `js show/hide/enable/disable if`, `script:`, `javascript:`, `.js`
  references, or quoted `javascript` response/log arguments, plus `js*` filenames.
- The broad filename match also selected two JSON-only examples,
  `json-response.yml` and `jsondemo.yml`. Thus the sweep covered **29 JS-related
  demos plus two extra JSON examples**, not 31 distinct JS implementations.
- The miniature `demo_package` has no JS/script references; its three interviews
  passed the runner's additional compile smoke check in each lane.
- Runtimes: docassemble base/demo/webapp **1.9.13** (`.venv-da19`) and **1.10.13**
  (`.venv-da110`), both using Python **3.14.10** for this run.

## Executed checks

### Canonical package compile/start sweep

Each selected original fixture was staged over its corresponding installed
package using the existing corpus runner, with network-denied execution,
prepared NLTK data, four workers, and a 45-second per-phase timeout.

| Lane | Compile | Start | Unexpected failures | Drift/timeouts/crashes |
| --- | --- | --- | --- | --- |
| 1.9.13 | 31/31 passed | 30/31 passed | `ml-ajax.yml` | 0 |
| 1.10.13 | 31/31 passed | 30/31 passed | `ml-ajax.yml` | 0 |

The initial modern attempt omitted prepared NLTK data and failed during runtime
initialization. Those results were discarded and the sweep rerun successfully
with the existing NLTK cache. No interview verdict below derives from that
initial setup failure.

Machine-readable output (ignored local artifacts):

- `.simulator/demo-js-da19/{summary.json,results.jsonl}`
- `.simulator/demo-js-da110/{summary.json,results.jsonl}`

These include fixture SHA-256 values, package identities, versions, error details,
and reproduction commands. They are local evidence, not checked-in fixtures.

### Real-interview conditional behavior

`tests/test_demo_js_examples.py` runs the **complete, unmodified YAML contents**
of the three conditional examples through typed execution operations. The
existing acceptance harness copies these standalone interviews into a temporary
`docassemble.acceptance` package; the compile/start sweep above additionally tests
their original canonical package identities.

Result: **6 pytest cases passed** (three interviews × two families). Their loops
exercise 19 scenario iterations per family:

- `jsshowif.yml`: three complete four-screen journeys (apple/mango/other;
  Chinese/French cuisine; TV/radio on/off; FM/AM; checkbox combinations), plus
  all eight TV/radio/modulation combinations and all four Apple/Plum combinations.
  The neither-fruit case explicitly submits an all-false checkbox dictionary.
  Checks cover initial visibility, conditional requiredness, rejection rollback,
  reload of saved screens, hidden values not being synthesized, chained hidden
  radio lookup, and arrival at the final thank-you screen.
- `jsshowifmaybe.yml`: complete journeys for true, false, and null. Checks verify
  the distinct `"None"` branch, required dependent answers, rollback, and final
  output without defining hidden dependent fields.
- `country-state.yml`: initial state field visible but disabled; blank submission
  requires country only; selecting US requires state; rejection rolls back;
  submitting US/NY reaches the final New York/United States screen. **This tests
  the `js enable if` predicate, not asynchronous country-specific choice updates.**

These assertions cover both truth outcomes of all 11 authored conditions.
No `js disable if` occurs in this demo corpus.

### Upstream browser semantics probe

Reran `scripts/probe-js-conditions.mjs` against each installed upstream `app.js`
using Chrome **156.0.8078.76**. Both passed: 26 predicate observations, chained
settling checks, and four enablement/no-trigger configurations. This corroborates
control lookup and predicate behavior on a minimal DOM; it is **not** a hosted
execution of the demo interviews or a test of their authored scripts.

## Per-example result and remaining limitation

All rows compiled on both lanes. “Starts” below means only that the first screen
was successfully described; it does not imply script execution or completion.

| Example | Startup, both lanes | Behavioral result / browser-dependent gap |
| --- | --- | --- |
| `ajax-calc-javascript.yml` | Starts | JS background hint/check-in delivery not executed. |
| `ajax.yml` | Starts | Verify button, submit interception, AJAX, and flash updates not executed. |
| `background-tail.yml` | Starts | Redis/background job, polling, and DOM log appending not journey-tested. |
| `background_action_javascript.yml` | Starts | Background JS notification/alert not executed. |
| `chart.yml` | Starts | Chart.js download/rendering not executed; chart is downstream of collection questions. |
| `chat.yml` | Starts | Live-chat services and external JS not verified. |
| `country-state.yml` | Starts | Enablement and US/NY completion pass; asynchronous choice refresh remains unsupported/unverified. |
| `customdate.yml` | Starts | Custom date-widget asset not executed. |
| `external_files.yml` | Starts | External JS/CSS effects not executed. |
| `fields.yml` | Starts | JS date/time DOM decoration not executed. |
| `flash.yml` | Starts | Client-side flash messages not executed. |
| `getField.yml` | Starts | DOM click/getField update not executed. |
| `getFields.yml` | Starts | “Decide for me” field population not executed. |
| `googlechart.yml` | Starts | Google chart loader/rendering not executed; chart is downstream. |
| `html.yml` | Starts | JS date/time DOM decoration not executed. |
| `js_action_call.yml` | Starts | Button callback, JSON response delivery, and DOM results not executed. |
| `js_url_action.yml` | Starts | JS-generated action link and click navigation not executed. |
| `js_variables.yml` | Starts | Asynchronous variable fetch and DOM insertion not executed. |
| `json-response-ajax.yml` | Starts | JS parsing, shipping-cost callback, and DOM result not executed. |
| `json-response.yml` | Starts | Extra JSON-only filename match; external response journey not tested. |
| `jsondemo.yml` | Starts | Extra JSON-only filename match; external API submission not tested. |
| `jsshowif.yml` | Starts | All eight predicates and full journeys pass, including radio chain and checkbox conjunction. |
| `jsshowifmaybe.yml` | Starts | Both predicates and all three true/false/null journeys pass. |
| `ml-ajax.yml` | **Fails** | Missing server-backed machine-learning method; see below. |
| `pytojs-dom.yml` | Starts | Later DOM-data lookup and console output not executed. |
| `pytojs-hidden.yml` | Starts | Later hidden-input `setField` mutation not executed; cannot claim the browser's modified color value. |
| `pytojs-log.yml` | Starts | Generated JS log variable and later console output not executed. |
| `pytojs-script.yml` | Starts | Later generated JS variable and console output not executed. |
| `script.yml` | Starts | Delayed “Pretty please?” DOM update not executed. |
| `setField.yml` | Starts | “Decide for me” scalar/checkbox population not executed. |
| `val.yml` | Starts | Click handler and DOM insertion not executed. |

### Observed failure: `ml-ajax.yml`

Both families fail at startup with:

```text
'DummyObject' object has no attribute 'save_for_classification'
```

The example creates a `SimpleTextMachineLearner` and immediately calls
`ml.save_for_classification(phrase)`. The available headless runtime supplies a
placeholder rather than the server-backed learner. The corpus runner classifies
this as a service blocker, not a JS parser failure. It remains unexpected in
this run; no new exemption was added to make the sweep green.

## Advice

1. The bounded conditional feature is validated against the **actual demo YAML**,
   not just synthetic issue fixtures, within the scenarios above.
2. Do not advertise general JS demo compatibility. Scripts that populate fields,
   mutate hidden inputs, disable submission, or update AJAX choices can change
   interview answers and flow, not merely appearance.
3. Treat `country-state` as partial support: its enablement predicate works, but
   non-US choice refresh needs browser/server validation or an explicit supported
   check-in/event model. US/NY success alone does not establish that capability.
4. Testing all remaining authored JS behavior requires a hosted docassemble server
   plus a browser harness and relevant services/assets. That is a separate
   capability from running the simulator's predicate evaluator.
5. The ML failure deserves a typed capability boundary (or a reasoned service
   exemption) rather than interpreting successful compilation as runnable support.

## Reproduction

Conditional journeys:

```sh
DASIMULATOR_REAL_PYTHON="$PWD/.venv-da110/bin/python" \
DASIMULATOR_REAL_PYTHON_19="$PWD/.venv-da19/bin/python" \
  .venv/bin/python -m pytest -q -o addopts='' tests/test_demo_js_examples.py
```

Compile/start selection and sweep:

```sh
match=$(.venv/bin/python - <<'PY'
from pathlib import Path
import re
root = Path('../docassemble-yaml/lsp/tests/fixtures/examples')
selected = []
for p in sorted(root.glob('*.yml')):
    text = p.read_text()
    if (re.search(r'^\s*(?:js (?:show|hide|enable|disable) if|javascript|script)\s*:', text, re.M)
            or re.search(r'''\.js\b|['"]javascript['"]''', text)
            or p.name.startswith('js')):
        selected.append(p.name)
print(r'^(?:' + '|'.join(re.escape(n) for n in selected) + r')$')
PY
)
for lane in da19 da110; do
  DASIMULATOR_DEMO_PYTHON="$PWD/.venv/bin/python" scripts/test-demo-corpus \
    --python "$PWD/.venv-$lane/bin/python" --compile --start --match "$match" \
    --prepare-runtime-data --nltk-cache-dir "$PWD/.simulator/nltk-cache" \
    --jobs 4 --timeout 45 --output ".simulator/demo-js-$lane"
done
# Each lane exits 1 because ml-ajax remains a recorded failure.
```

Upstream minimal-DOM probe (requires the already-installed Playwright package
and Google Chrome):

```sh
for lane in da19 da110; do
  node scripts/probe-js-conditions.mjs \
    /tmp/dasim94-browser/node_modules/playwright/index.mjs \
    ".venv-$lane/lib/python3.14/site-packages/docassemble/webapp/static/app"
done
```
