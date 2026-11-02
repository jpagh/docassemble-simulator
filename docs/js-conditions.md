# Declared JavaScript conditions

The simulator evaluates a bounded subset of `js show if`, `js hide if`,
`js enable if` and `js disable if`.
This is **not JavaScript/script execution**: no DOM, jQuery, Python objects,
custom helpers, browser callbacks, uploads, requests, or general host functions
are exposed. `esprima2>=6.0.0` (imported as `esprima`, not the old `esprima`
distribution) parses syntax; the simulator interprets only the allowlist below.

## Supported contract (#94)

Exactly one expression, with an optional terminating semicolon:

- String and numeric literals (including signed numeric literals), `true`,
  `false`, `null`, and `undefined`.
- Parentheses, `!`, `&&`, `||`, `==`, `!=`, `===`, `!==`, `<`, `<=`, `>`, `>=`.
- Direct `val("literal input name")` calls. No dynamic names or other functions.

Precedence comes from the parser. Truthiness, Boolean/number/string coercion,
loose versus strict equality, UTF-16 string comparison, and short-circuiting
follow JavaScript primitive semantics, not Python truthiness/equality. Array literals,
objects, regexes, BigInt, arithmetic, assignments, multiple statements and all
other syntax are unsupported, **even in an unreachable branch**.

For example, with an integer `children_count` input on this screen:

```yaml
fields:
  - Count: children_count
    datatype: integer
  - Is the child still living?: the_child_living
    datatype: yesnoradio
    js show if: val("children_count") == 1
  - Are all children still living?: all_children_living
    datatype: yesnoradio
    js show if: val("children_count") > 1
```

Count 1 requires only the singular answer; count 2 only the plural answer.
Other required fields remain required. `val()` reads a numeric **input string**:
`val("children_count") === 1` is false, unlike loose equality with 1. Submitted
numeric spelling is retained for this operation (`1.0` is the string `"1.0"`).
Unanswered text/numeric inputs yield `""`, unchecked checkboxes yield `false`,
and unanswered radios or absent controls yield `null`, not `undefined`.
Boolean radio choices yield booleans; ordinary scalar radio values are strings.

Dependencies are collected over the **whole expression**, separately from
short-circuit evaluation. A condition activates only if at least one dependency
binds to a rendered input **and** appears in upstream's ask-time trigger `vars`.
A namespace variable is not a browser control. A second missing scalar control
returns `null`; it does not prevent a condition with a real controller running.
`true || val("children_count")` still has a controller. Constants alone do not.
Upstream's literal-call regex is narrower than the AST parser: for example,
`val( "children_count" )` can parse but register no upstream trigger. We preserve
that distinction instead of silently broadening activation.

Without a trigger, **both show-if and hide-if remain initially hidden**. This
is a separate `no-trigger` state, not false followed by sign inversion.

## Presentation, persistence and submission

The screen captures the rendered expression, sign/mode, upstream trigger names,
complete literal dependencies and supported rendered browser bindings once from
`ask()` results. Mako is not rendered again during submission. These ordinary
JSON-compatible facts survive saved-session reload; the metadata is not mutated
by attempted submissions. Old saved screens without the additive metadata are
still readable and retain their previous behavior; use `refresh` or `start` to
capture JS conditions for them.

Description and the shared whole-screen submission acceptance use the same
condition evaluator. Post-assignment visibility controls rendered defaults,
optional blanks, requiredness and constraints for both `Answer` and automatic
seek/replay. Rejection never persists attempted values. ADR-0013's explicit
assignment policy is unchanged: explicit assignments to described hidden fields
are retained if the transaction succeeds; omitted hidden fields are not blanked.

Unsupported expressions/bindings/values have `visible: null`,
`js_condition_state: unsupported`, an explanatory `visibility_note`, and a
structured `js_condition_diagnostic` (`kind`, `code`, `message`). A normal or
automatic submission is rejected with typed **`js-condition-unsupported`**, not a
false visibility result or requiredness exemption. Ordinary `Answer` returns a
failed outcome. Automatic seek/replay retains its established successful walk
**envelope with a rejected attempt**: `auto_stop.reason` is `unsupported JS condition`,
`auto_stop.submitted` is false, and the retained `auto_trace` attempt has `ok: false`
and the identical typed error/diagnostic. It cannot advance that screen, and its
attempted namespace changes are rolled back. `--partial`, `--strict` and
`--no-validate` do not bypass this capability boundary. For supported expressions,
partial/strict retain their existing required-warning policy; no-validate skips
only authored validation. If assignment authorization/parsing already failed,
that original rejection retains its dispositions and active-field guidance;
unsupported requiredness guidance adds the capability `diagnostic` instead of
an uncomputable `missing_required` list. `exec` remains the explicit state-surgery escape hatch.

Screen traces retain **presentation** visibility/required facts, not the attempted
POST's operation-local decisions. Captured JS metadata/diagnostics do not change
identity, comparison projections or the trace schema (ADR-0012).

## Bounds and next slices

Source and scalar strings are capped at 4096 characters; expression ASTs at 256
nodes and depth 32; evaluation at 256 node visits. The parse cache holds at most
128 expressions. Syntax and parser-recursion failures have predictable diagnostic
codes, as do unsupported ASTs, binding/value failures and limits. These bounds
mitigate parser work; they are not a hard hostile-input wall-clock/memory sandbox
(esprima may process a regex before the AST allowlist rejects it).

## Indexed and grouped browser bindings (#95)

Rendered dotted, indexed and generic field paths are resolved at presentation.
The frozen binding records both the authored browser identity and its resolved
namespace target (for example `attorneys[i].type` -> `attorneys[0].type`, or
`x.type` -> `creditor.type`). Only actual rendered fields can create bindings;
`val()` arguments never authorize namespace traversal. Server-suppressed inputs
are absent, even if a saved namespace value exists. With another real trigger,
their value is JS null and their negation is true. Without any trigger the
wrapper remains hidden, including for strict negative comparisons.

Checkboxes/multiselects expose arrays of rendered choice strings in rendered
order. Option addresses such as `liked_fruits['Apple']` expose booleans, and
unoffered options remain absent/null. Empty arrays are truthy, unlike Python
lists. Each `val()` call returns a fresh array, so repeated grouped calls compare
strictly unequal even when their contents match. Strict comparisons preserve
array identity/type; loose equality or
ordering requiring array coercion returns an explicit `coercion` capability
diagnostic rather than guessing a string conversion. Object selection widgets
remain unsupported bindings.

Nullable radios expose unanswered JS null separately from the selected string
`"None"`. Explicit blank submissions remain unanswered; selecting `None`/`null`
is a real radio answer for requiredness. Frozen Mako expressions support JSON
Boolean and string interpolation (`${json.dumps(flag)}` and
`${json.dumps(marker)}`), including saved reload and worker transport. These are
documentation examples, not Mako usage observed in the corpus conditions.
Generated JS visibility is applied after native rendering metadata and is not
overwritten by native `ok`; authored native+JS conditions on the same field are
still rejected by both upstream compilers.

## Chained visibility and defaults (#96)

Chained show/hide conditions settle in rendered field order, beginning with
upstream's initially hidden JS wrappers rather than reusing stale presentation
visibility. Hidden controllers return JS null even when explicit or saved values
exist in the namespace. Rendered controls still register triggers while hidden;
controller eligibility affects lookup, not static dependency discovery. Disabled controls also return null; enablement is settled separately from visibility
(see #97 below).

Presentation and submission share this bounded settling evaluator. Each visibility
pass allows at most twice the field count plus one rounds and detects repeated
visibility states. Contradictions report `js-condition-unsupported` with diagnostic
code `nonconvergence`, affected fields and recovery guidance. An unsupported or
nonconvergent presentation does not freeze the attempted decision: a later
assignment may resolve it. Only rendered expressions/bindings are frozen.

The existing native/default loop reapplies this settled visibility after values
change. Defaults introduced by the current submission are removed when hidden;
explicit assignments and pre-existing values remain untouched. Requiredness,
optional blanks and constraints consume the same final screen decision, including
in early assignment-error guidance, seek/replay and workers. A rejection preserves
the presentation and rolls back attempted namespace changes. Activated automatic
walks may retain target and rejected-attempt metadata, never attempted POST facts.
Trace facts remain presentation facts; chained presentation visibility can therefore
intentionally change a golden's compared facts without changing identity/schema.

This deterministic bounded contract does not emulate general browser event ordering,
side-effecting JS or asynchronous callbacks. General helper-function execution is
not planned here.

The implementation keeps rendered binding adaptation, settling and enablement
separate without widening the language: `_js_conditions.py` owns parsing,
dependency discovery, primitive interpretation and rendered-value adaptation;
`describe_fields` captures ask-time metadata; `_submission.py` is the sole
acceptance owner. Its `CapabilityRejected` verdict lets ordinary and automatic
operations apply their existing transaction/reporting policy without losing
rejected automatic attempt records. No AST or interpreter-specific JS code enters saved state.

## Source and browser evidence

Verified against installed docassemble-base/webapp **1.9.13 and 1.10.13**:

- `parse.py`: `js show if` / `js hide if` compilation registers literal-call
  trigger `vars`; `Question.ask()` renders `extras['show_if_js']` once. Both
  families reject authored native and JS show/hide on the same field with
  `You cannot mix js show if and non-js show if` (1.9:4210/4229;
  1.10:4003/4022). Separate formatter branches are not evidence that such an
  authored combination is supported. Generated/native `ok` metadata interaction
  is a distinct binding concern for #95, not same-field condition composition.
- `standardformatter.py`: mode-0 `.dajsshowif` wrappers start `display: none`
  for either sign; the native empty-choice path suppresses actual inputs.
- `static/app/app.js`: `getField()` excludes absent/disabled/hidden controls;
  `val()` preserves numeric/text input strings, translates radio `True`/`False`
  to booleans and returns null for missing/unanswered controls. The
  `.dajsshowif` initializer installs evaluation only for matching trigger inputs;
  the handler applies the sign after evaluating and hides/disables the opposite
  branch. Its bounded `daTriggerAllShowHides` retries contradictory conditions up
  to 100 times; the simulator instead detects repeated states and rejects them.

`tests/test_js_conditions.py` contains source-named description/primitive and
resource-bound comparisons. `tests/test_issue95_js_conditions.py` adds indexed/generated/generic bindings,
conditional absence, nullable/grouped values, rollback, replay and worker transport.
`tests/test_issue94_js_conditions.py` exercises the
real compiler and typed operations on both families, including ordinary answers,
automatic submission, rollback, Mako freezing, defaults, constraints and no-trigger
behavior. Browser evidence is reproducible without adding a JS runtime dependency:

```sh
mkdir -p /tmp/dasim94-browser
npm install --prefix /tmp/dasim94-browser --no-save playwright
# Requires locally installed Google Chrome. Use each provisioned family's app dir.
node scripts/probe-js-conditions.mjs \
  /tmp/dasim94-browser/node_modules/playwright/index.mjs \
  .venv-da19/lib/python3.14/site-packages/docassemble/webapp/static/app
# Repeat with .venv-da110.
```

The focused Chrome **156.0.8078.76** probe passed the original 20 comparisons per family:
count-switch, strict/loose numeric equality, absent second controller,
short-circuit dependency registration, no-trigger show/hide, numeric blanks,
radio blanks, Boolean radio values, all three AutomatedPleading expression shapes,
checkbox-option/group arrays and the selected `"None"` radio string. It executes the **installed upstream**
`getField`, `val` and show/hide registration/handler source with upstream jQuery
on a minimal scalar-control DOM. It is not a full hosted interview/server test.
The 1.10 accessibility-announcement helper is included with setup incomplete, so
announcements cannot affect the visibility comparison.

The #96 extension additionally verifies default-prefilled radio/modulation/antenna
chains, reverse and restored transitions, hidden/disabled controller null lookup,
a missing second controller and contradictory visibility (100 rounds without
convergence) on both families. It executes upstream handlers and repeats manual
triggers like the upstream outer loop; it marks input IDs seen to isolate that loop
from recursive change dispatch. This is evidence for lookup/settling, not a claim
of general browser event-loop emulation. `tests/test_issue96_js_conditions.py`
covers the shared execution gates, default removal, explicit/saved precedence,
transactional rejection, saved reload, worker transport, seek and replay on both
runtime families.


## Separate enablement (#97)

`js enable if` and `js disable if` use the same supported expressions, static
rendered trigger discovery and bounded settling as show/hide. They change
`enabled`, never `visible` or `rendered_input`. A bare scalar country predicate
such as `val('country')` enables the state control when the country choice is
nonempty; disable-if reverses only an actually triggered result. Without a
trigger, either sign preserves the upstream initial enabled state (normally
true, or false for an authored disabled input). Constants do not trigger.
A no-trigger wrapper never receives upstream's `isVisible` lookup marker:
its control stays enabled and participates in submission, but `val()` returns
null when another condition reads it. Lookup availability is separate from
submission eligibility.
Unsupported enablement is `enabled: null` with the same explicit capability
diagnostic/rejection, not a hidden field or a validation bypass.

All gates share settled eligibility: visible, enabled, actually rendered inputs
are required/blanked/defaulted/constrained. A disabled controller returns null
even if a saved or explicit value exists. Rendered identity still registers its
trigger. Re-enabling restores frozen authored requiredness and constraints.
Defaults introduced during this submission are removed if disabled; saved and
explicit values are never cleared. Explicit assignments to described disabled
controls remain authorized and applied under ADR-0013, deliberately broader than
a browser POST. Their rendered constraints are skipped while disabled, just like
other disabled values; re-enabling enforces them again. This does not bypass
assignment parsing/custom datatype hooks or authored validation code.

Enabled facts survive saved reload and workers; answers and automatic seek/replay
use the shared acceptance decision. Older screens lacking enabled metadata retain
the previous eligible behavior. Traces retain enabled as **debug-only metadata**:
optional-field enablement-only changes intentionally do not diverge. Effective
requiredness changes do participate in the existing comparison, so goldens for
formerly required disabled inputs can require review. No identity/schema change.
Rejected attempts retain unchanged presentation facts.

`tests/test_issue97_js_conditions.py` covers the required/optional/default/constraint
matrix, both condition forms, no-trigger initial state, re-enable transitions,
ineligible dependencies, rollback, saved reload, automatic paths and workers on
both runtime families. The browser probe additionally exercises upstream mode-1
handlers for country/state scalar selections, reverse transitions, visible but
disabled controls, null lookup, disabled exclusion from jQuery Validate form gates
and serialized POST/default values, plus no-trigger state for both signs. This is
a focused minimal-DOM comparison, **not** a hosted-interview or general-script test.
The demo country/state interview's asynchronous choice-update script is not
implemented; no dynamic choices, custom callbacks or network actions are claimed.
