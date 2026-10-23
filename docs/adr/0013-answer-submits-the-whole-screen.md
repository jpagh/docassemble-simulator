---
status: accepted
---

# `answer` submits the whole screen

The CLI's `answer` operation is the simulator's browser-shaped submission path. It must model what a real form POST does, because otherwise the simulator can complete an interview that no browser user could: it could leave required fields blank (warn-only), and it could set variables that are not on the current screen and thereby skip screens. Recording screen traces made both observable.

## Decision

- A browser submits the whole page. docassemble's webapp adds blank visible fields as `None` (`_empties`), unchecked checkboxes with their false values, and fields whose choices resolved empty as `None`, then filters the submission to the current question's `authorized_fields`. `answer` now does the equivalent for the active screen outcome:
  - submitted assignments are applied first and take precedence,
  - visible fields not submitted take their rendered default when one exists (the browser posts the prefilled value),
  - remaining visible optional fields take the value the browser would post for
    their datatype: an empty string for text and date inputs, `0`/`0.0` for
    numeric inputs, `None` for radio-style booleans (`yesnoradio`,
    `yesnomaybe`), `threestate`, empty-choice checkbox groups, and object
    selections; a checkbox-style boolean takes its unchecked value (`False`
    for `yesno`, `True` for `noyes`, matching the formatter's `_checkboxes`
    hidden input); an untouched checkbox group becomes an all-false `DADict`;
    and an untouched file field becomes `None`. A rendered default that cannot
    be applied falls back to the datatype's blank,
  - remaining visible required fields reject the submission as a `validation` failure naming each field. A required multiple-choice group (`checkboxes`, `multiselect`, `object_checkboxes`, `object_multiselect`) is empty when it holds no true selection, mirroring the browser's "check at least one option" rule; the submission is rejected with the same naming.
- An unknown-field or parse-error rejection retains the per-assignment dispositions and active-field guidance, and adds `missing_required` from those same live Screen facts. Valid assignments and valid rendered defaults count for that guidance; hidden and optional fields do not. This early assessment does not run authored validation or commit any attempted value.
- The submission is scoped to the fields the screen described, including resolved generic-object targets (`x.date` and `rav.date`). A variable that is not on the active screen is an `answer-input` failure. Screens with no described fields (continue, field-less deadend) keep accepting explicit assignments; `exec` remains the escape hatch for deliberate state surgery.
- `answer --partial` restores the previous per-field behavior: only the submitted fields are applied, missing required fields become warnings, and lazy re-seeking may re-present the same screen. `--strict` keeps promoting those warnings to errors.
- `--no-validate` skips only the interview's `validation code`. The required-field gate and blank/default synthesis still apply because they are part of a form submission, not validation code. `--partial` is the explicit opt-out.
- Prefilled values are never overwritten: a field that already has a value in the namespace is left alone.
- Explicitly empty submissions follow the same per-datatype conversion (`count=` becomes `0`, `ratio=` becomes `0.0`, `agree=` becomes `None`), so a caller cannot accidentally park a string in a numeric or boolean field.
- `eval` reprs its result inside the prepared runtime context; gatherable objects such as `DADict`/`DAList` previously failed to render outside it.

## Hidden fields, empty answers, and signatures

- Hidden (`show if`) fields are not defined and not assigned. The user submitted the visible page, and assigning a field the page did not offer can stomp state the flow set elsewhere. Hidden fields are therefore never required either.

  **Correction (ADR-0014).** The justification above does not match the server, and the difference is recorded rather than fixed here. Two separate mechanisms were conflated:

  - The `_empties` input this bullet cited is emitted from the formatter's `hiddens`, which is populated by `is_empty_mc` — a multiple-choice field whose resolved choices came back empty (`standardformatter.py:1293-1297`, `parse.py:302-313`) — not by `show if`.
  - A `show if` field is still rendered, hidden with `display: none` (`standardformatter.py:1331-1343`), so a browser does post it.

  **Amendment (issue #58).** The simulator still deliberately leaves omitted
  hidden fields undefined rather than synthesizing their posted blank values.
  Visibility is evaluated after explicit assignments, then omitted rendered
  defaults are resolved against that visibility. Defaults may reveal further
  defaults or hide earlier ones: visibility and defaults settle in at most
  twice the field count plus one rounds, rejecting an unstable cycle without
  saving. Defaults applied in this operation are removed if their fields become
  hidden; explicit assignments and pre-existing values are never removed.
  Blank synthesis and the required gate use the settled visibility. This mirrors show-if interaction and jQuery Validate's default
  `ignore: ":hidden"`: a field hidden by the current submission is not required.
  An undefined show-if controller hides its dependents at submission; other
  evaluation errors remain conservative. Generated controls retain their
  ask-time visibility expressions without re-running authored expansion.
  Captured compiled expressions are marshalled with the producing interpreter's
  `sys.implementation.cache_tag`. Before unmarshalling, a missing or mismatched
  tag rejects submission as a typed `state` failure: captured visibility cannot
  be reused under this interpreter; run `refresh` or `start` again. This guards
  code-format compatibility, not trust (saved sessions remain trusted pickles).
  Screens without captured code are unaffected.
  Fields that remain visible keep their ask-time requiredness; computed
  requiredness is not re-evaluated. Early input-rejection guidance uses the
  same updated visibility without committing attempted values. Explicitly
  supplied field assignments still take precedence, even when they hide a
  field. `--partial` evaluates visibility after assignments but does not apply
  defaults or synthesize blanks. The omitted-hidden-field POST divergence
  remains deliberate, not an unresolved requirement of this change.
- An explicitly empty submission for a visible required field (`field=`, `field=None`, or a value that coerces to `None`) fails the required gate with an "is empty" message, except an explicitly selected offered null radio choice as described below; a quoted `"None"` remains an ordinary string value. This is the API equivalent of a browser refusing to submit a blank required input. `--partial` downgrades it to a warning like any other missing required field. An object group field that rendered no choices is exempt: docassemble clears and gathers it rather than applying a selection (ADR-0014), so there is none to demand.

  **Amendment (issue #71).** Replay's per-step submissions carry the same explicit-blank intent as an ordinary `answer`, so a raw empty or `None` replay override on a visible required field fails the required gate and stops the walk with the required-missing `auto_stop` payload. Replay no longer lets browser-style coercion turn an empty numeric override into `0`/`0.0` and commit it: a replay can no longer commit a screen state a browser user could not have submitted. Literal text, `None`, yesnoradio, and non-required numeric fields keep their existing behavior, and an explicit `0`/`0.0` is still accepted.

  **Amendment (issue #83).** An explicitly selected null-valued radio choice
  is an answer, not an unanswered control. A nonblank `None` or `null` token
  selecting a rendered choice whose value is null passes the required gate
  and stores `None`. This applies to radio inputs (including `threestate`)
  through ordinary answers and native replay overrides, under either the
  generic placeholder or resolved target name. Selection intent belongs to
  this submission, not to every null in the namespace: omission without an
  answer/default, an explicit empty string (even when coerced to `None`), and
  a null where no null choice was offered still reject. Text/numeric blank
  rejection, submission-time visibility, remaining rendered constraints,
  authored validation, and transactional rollback are unchanged. Input-error
  guidance also counts a successfully applied null selection as answered.

  This follows the installed 1.9.13 and 1.10.13 browser/server sources:
  `standardformatter.py` renders radio choices with
  `value=str(pair['key'])` (1.9:3048; 1.10:3047), so a null choice posts the
  nonempty string `None`. Required radios use jQuery Validate's checked-control
  count, not the resulting Python value (`static/app/jquery.validate.js`:
  1083-1089, 1402-1415 in both families). The server converts a posted
  `threestate` token other than `True`/`False` to `test_data = None` and
  assignment source `None` (1.9 `server.py`:8024-8033; 1.10
  `interview/views.py`:1517-1526). Thus checking that radio satisfies browser
  requiredness while legitimately storing Python null.
- Signature fields are never required and become `DAEmpty()` when untouched, so templates and documents that reference them render as empty instead of failing the flow. Signature capture itself is not part of the CLI submission path.

## Known gaps

- Custom datatypes are left undefined because a fabricated empty value can corrupt interview logic; the flow may re-seek those optional fields.
- `object_multiselect` and `object_checkboxes` are no longer a gap: ADR-0014 specifies both server paths, including the empty-choice case that clears the group and marks it gathered.

## Considered options

1. **Warn-only required fields (previous behavior).** Stable for partial drivers, but it is a false-green: a run can finish with required answers never given, and off-screen variables can skip screens.
2. **Enforce required, reject off-screen variables, and synthesize blanks as one change (chosen).** Fidelity to the webapp, at the cost that adding a required field to an existing screen requires updating drivers that answer it. That is a real interaction change the browser also enforces, and a screen trace surfaces the changed field set through content comparison.
3. **Enforce only behind `--strict`.** Cheaper to adopt, but leaves the default path non-faithful and keeps the completion blind spot.

## Consequences

- A green `answer` run now implies a browser user could have submitted that screen: required fields are present and no off-screen state was injected.
- `sets:` questions are submitted once. Screen traces record one entry per screen presented, so occurrence counts mean "shown again" (loops, validation re-asks) instead of "the flow sought another variable from the same screen". Traces recorded before this change need re-recording.
- `--partial` preserves compatibility for drivers that intentionally answer a subset of required fields; it is a mode, not a fallback the simulator silently chooses.
- The screen outcome's `fields` remain the source of truth for what was
  rendered, including requiredness, defaults, and choices. Issue #58 supersedes
  the original next-presentation visibility rule: submission-time show-if
  visibility determines which fields are blanked or required. This evaluation
  uses an operation-local copy; persisted screen outcomes and screen-trace
  field facts continue to describe the presentation, not the attempted POST.
  Existing trace schemas remain readable. Goldens whose formerly rejected
  submissions now hide their dependents must be re-recorded.



## Rendered browser constraints (#62)

Whole-screen submission checks fields visible under the post-submission
show-if state described above against the runtime-computed min/max and
minlength/maxlength facts before authored validation code. Fields hidden by
submitted assignments or rendered defaults are not blanked, required, or
constrained. Range
bounds are inclusive. Text length counts JavaScript UTF-16 code units (with
carriage returns removed); group lengths count selected choices. Optional
empty inputs remain exempt, like jQuery Validate. Numeric text controls with
an explicit step enforce jQuery's zero-based multiple rule, not an HTML
number input's min-based step offset. Slider scale is a rendering mode, not a
precision constraint; scale and slider step are recorded but not validated.
Time and datetime controls do not receive lexical min/max checks: their
text/widget inputs accept informal values for which string ordering is not a
browser rule. Date controls retain calendar-date bounds. Registered custom
datatypes receive their native validation/transform hooks with rendered
constraints and resolved custom parameters. An explicitly submitted empty
string still runs those hooks: webapp `views.py` only uses `skip_if_empty` /
`empty()` when `set_to_empty` comes from `_empties`, not when `raw_data == ""`.
No custom empty value is fabricated for omitted controls.
`--no-validate` does not bypass this form gate; `--partial` bypasses it, retaining
its existing authored-validation and required-warning semantics. `exec` is
unchanged. Constraint facts are debug-only in trace comparison (ADR-0012).
