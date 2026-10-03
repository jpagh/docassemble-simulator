---
status: accepted
---

# Object group answers follow the server's two paths

docassemble applies an `object_checkboxes`/`object_multiselect` answer one of
two ways, keyed on `parse.is_empty_mc(field)`: whether the field's `choicetype`
is `compute`/`manual` and its resolved choice list came back **empty**
(`parse.py:302-313`; 1.9 `parse.py:510-521`). The simulator previously
implemented only the empty-choice path and applied it to every submission, so
every group answer ran `clear()` and then re-appended the ticked choices. That
destroyed list elements that had no checkbox on the screen, dropped the
server's membership guard, and made an unticked choice indistinguishable from a
choice that was never rendered.

## Decision

Both paths are implemented, and the two are mutually exclusive.

- **Path A — the field rendered choices.** The formatter emits one checkbox per
  choice and registers every one of them in the `_checkboxes` hidden input with
  `'False'` (`standardformatter.py:1777-1791`), so an unticked choice arrives as
  `'False'` rather than being absent. For each choice the server does
  (`views.py:1430-1440` → `1699-1711`; 1.9 `server.py:8206-8229`):

  ```python
  if <ticked>:
      if <data> not in <var>.elements:
          <var>.append(<data>)
  else:
      if <data> in <var>.elements:
          <var>.remove(<data>)
  ```

  The simulator does the same, exec'd in the interview namespace: a ticked
  choice is appended only when absent, an unticked choice is removed only when
  present, membership is tested against `.elements`, and **the list is never
  cleared**. An element with no checkbox on the page is left alone.

- **A rendered choice missing from the submission is unticked.** This follows
  directly from `_checkboxes` registering every rendered choice: the browser
  always posts one value per rendered checkbox. It applies to both the dict and
  the list answer form. `--code` remains the escape hatch for deliberate state
  surgery.

- **Path B — the field rendered no choices.** The field lands in the
  formatter's `hiddens` (`standardformatter.py:1293-1297`) and reaches the
  server as `empty_fields`: round-tripped through the `_empties` hidden input
  (`standardformatter.py:1876-1877`, consumed at `views.py:873-890`), or
  recomputed as `field_info['hiddens']` (`views.py:981`). `STRICT_MODE` defaults
  to false (`webapp/config.py:163`), but both settings converge on the same
  contents. The server (`views.py:1756-1759`; 1.9 `server.py:8262-8265`):

  ```python
  # fmt: off
  ensure_object_exists(sub_indices(key, user_dict), empty_fields[orig_key], user_dict)
  exec(key + '.clear()', user_dict)
  exec(key + '.gathered = True', user_dict)
  # fmt: on
  ```

  The simulator does the same. This path is also reached for a visible
  zero-choice group the caller never submitted, because the browser still posts
  such a field as empty. A *required* zero-choice group is cleared and gathered
  too, and is exempt from the simulator's required gate: the field offers
  nothing to tick, so demanding a selection would reject a screen the server
  accepts (ADR-0013 records the gate exemption).

- **Every mutation lands on the field's resolved target.** On a generic-object
  screen the field is posted under the placeholder (`x.shortlist`) while
  `orig_sought` names the resolved root (`rav.shortlist`). Both paths resolve
  the name through the same `targets` map the other field assignments use, so
  an explicit submission and the blank-synthesis path never operate on two
  different lists.

- **Answers are applied by exec'ing in the interview namespace, not by calling
  methods directly.** docassemble's frame-walking utilities resolve the
  interview from the caller's frame — 1.9's `get_user_dict()` scans frames for
  `_internal` (`base/functions.py:5789-5790`) while 1.10 keeps it in a context
  variable (`get_user_dict = get_current_user_dict`, `base/functions.py:3998`).
  A list whose `append` or `remove` resolves interview state (walkup's
  registry-backed `ObjectReferenceList`, for example) therefore only works when
  invoked from inside the namespace, which is where the server invokes it.

## Recorded divergences

- **Path A writes `gathered = True`; the server does not.** This is simulator
  bookkeeping, not fidelity: without it the screen's `<var>.gathered` seek never
  resolves and the same screen re-asks forever (`docs/fidelity-round-2.md` §1).
- **A visible group with choices that the caller never submits stays
  undefined.** The `answer` blank-synthesis pass leaves object groups alone
  (`_browser_blank`), and `_blankable_fields` skips any field whose variable
  already evaluates. Resolving this interacts with ADR-0013's separate
  "prefilled values are never overwritten" rule and is left to its own decision.
- **An unknown choice key is a CLI error.** The simulator raises
  `unknown object choice <key> for <variable>`; the server silently skips a
  posted key it cannot resolve. This is a deliberate diagnostics affordance for
  a CLI driver, and it is the same shape the simulator already used for keys it
  intended to apply.

## Considered options

1. **Keep clear-then-append for every submission (previous behaviour).** Simple
   and stable for drivers that submit the whole group, but it silently deletes
   off-screen elements and cannot represent "unticked but still on screen".
2. **Implement both paths, keyed on the rendered choice list (chosen).**
   Matches the server, and the one new inference — absent rendered choice means
   unticked — follows from the `_checkboxes` mechanism rather than from
   convenience.
3. **Implement Path A and leave Path B undefined.** Smaller diff, but keeps the
   simulator diverging exactly where the flow re-seeks an empty group.

## Consequences

- Answering an object group no longer removes elements the screen did not offer.
  A driver that relied on clear-then-append to reset a list must now reset it
  explicitly.
- Order is preserved for choices already in the list, because an existing
  selection is no longer removed and re-appended.
- An empty-choice object group becomes a defined, empty, gathered list instead
  of an undefined variable, so a flow that re-seeks it now sees `len() == 0`.
- `remove()` runs interview code during an answer, so a group whose items
  resolve interview state now exercises the same frame contract on untick as on
  tick.
