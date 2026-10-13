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
  Visibility is now evaluated against the submitted namespace after explicit
  assignments and rendered defaults, before blank synthesis and the required
  gate. This mirrors show-if interaction and jQuery Validate's default
  `ignore: ":hidden"`: a field hidden by the current submission is not required.
  Fields that remain visible keep their described requiredness; computed
  requiredness is not re-evaluated. Early input-rejection guidance uses the
  same updated visibility without committing attempted values. Explicitly
  supplied field assignments still take precedence, even when they hide a
  field. `--partial` evaluates visibility after assignments but does not apply
  defaults or synthesize blanks. The omitted-hidden-field POST divergence
  remains deliberate, not an unresolved requirement of this change.
- An explicitly empty submission for a visible required field (`field=`, `field=None`, or a value that coerces to `None`) fails the required gate with an "is empty" message; a quoted `"None"` remains an ordinary string value. This is the API equivalent of a browser refusing to submit a blank required input. `--partial` downgrades it to a warning like any other missing required field. An object group field that rendered no choices is exempt: docassemble clears and gathers it rather than applying a selection (ADR-0014), so there is none to demand.
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
