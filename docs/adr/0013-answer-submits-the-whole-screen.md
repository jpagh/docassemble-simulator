---
status: accepted
---

# `answer` submits the whole screen

The CLI's `answer` operation is the simulator's browser-shaped submission path. It must model what a real form POST does, because otherwise the simulator can complete an interview that no browser user could: it could leave required fields blank (warn-only), and it could set variables that are not on the current screen and thereby skip screens. Recording screen traces made both observable.

## Decision

- A browser submits the whole page. docassemble's webapp adds blank visible fields as `None` (`_empties`), unchecked checkboxes with their false values, and hidden fields as `None`, then filters the submission to the current question's `authorized_fields`. `answer` now does the equivalent for the active screen outcome:
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
- The submission is scoped to the fields the screen described, including resolved generic-object targets (`x.date` and `rav.date`). A variable that is not on the active screen is an `answer-input` failure. Screens with no described fields (continue, field-less deadend) keep accepting explicit assignments; `exec` remains the escape hatch for deliberate state surgery.
- `answer --partial` restores the previous per-field behavior: only the submitted fields are applied, missing required fields become warnings, and lazy re-seeking may re-present the same screen. `--strict` keeps promoting those warnings to errors.
- `--no-validate` skips only the interview's `validation code`. The required-field gate and blank/default synthesis still apply because they are part of a form submission, not validation code. `--partial` is the explicit opt-out.
- Prefilled values are never overwritten: a field that already has a value in the namespace is left alone.
- Explicitly empty submissions follow the same per-datatype conversion (`count=` becomes `0`, `ratio=` becomes `0.0`, `agree=` becomes `None`), so a caller cannot accidentally park a string in a numeric or boolean field.
- `eval` reprs its result inside the prepared runtime context; gatherable objects such as `DADict`/`DAList` previously failed to render outside it.

## Hidden fields, empty answers, and signatures

- Hidden (`show if`) fields are not defined and not assigned. The user submitted the visible page, and assigning a field the page did not offer can stomp state the flow set elsewhere. Hidden fields are therefore never required either.
- An explicitly empty submission for a visible required field (`field=`, `field=None`, or a value that coerces to `None`) fails the required gate with an "is empty" message; a quoted `"None"` remains an ordinary string value. This is the API equivalent of a browser refusing to submit a blank required input. `--partial` downgrades it to a warning like any other missing required field.
- Signature fields are never required and become `DAEmpty()` when untouched, so templates and documents that reference them render as empty instead of failing the flow. Signature capture itself is not part of the CLI submission path.

## Known gaps

- `object_multiselect` and `object_checkboxes`, and custom datatypes, are left undefined because a fabricated empty object or value can corrupt interview logic; the flow may re-seek those optional fields.

## Considered options

1. **Warn-only required fields (previous behavior).** Stable for partial drivers, but it is a false-green: a run can finish with required answers never given, and off-screen variables can skip screens.
2. **Enforce required, reject off-screen variables, and synthesize blanks as one change (chosen).** Fidelity to the webapp, at the cost that adding a required field to an existing screen requires updating drivers that answer it. That is a real interaction change the browser also enforces, and a screen trace surfaces the changed field set through content comparison.
3. **Enforce only behind `--strict`.** Cheaper to adopt, but leaves the default path non-faithful and keeps the completion blind spot.

## Consequences

- A green `answer` run now implies a browser user could have submitted that screen: required fields are present and no off-screen state was injected.
- `sets:` questions are submitted once. Screen traces record one entry per screen presented, so occurrence counts mean "shown again" (loops, validation re-asks) instead of "the flow sought another variable from the same screen". Traces recorded before this change need re-recording.
- `--partial` preserves compatibility for drivers that intentionally answer a subset of required fields; it is a mode, not a fallback the simulator silently chooses.
- The screen outcome's `fields` are the source of truth for what was shown. Fields whose visibility or requiredness would change because of the submitted values are handled on the next presentation, mirroring a browser that re-renders with the new state.
