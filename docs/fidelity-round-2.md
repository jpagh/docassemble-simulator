# Answer Fidelity and Seed Setup (Round 2)

Status: proposal, ready for implementation
Date: 2026-08-25
Scope: findings from live verification of `c2b9e66` against the
`docassemble-automatedpleading` family flow. One simulator bug to upstream,
usage/error-message improvements, the full seed-setup story (proven to be the
real B2 fix), and two open items for the real-state render tail.

## Context

The `c2b9e66` B2 fix (objselections in `fresh_user_dict`, answer-side
`_apply_object_assignment`, selectcompute describe) was verified by driving
the family interview with zero workarounds. Two outcomes:

1. The attorneys-screen **ask-side crash was not a simulator code gap** — it
   was the seed script violating the A3 contract. Docassemble keys object
   choices by `base64(instanceName)` and `eval()`s that instanceName back into
   the session; `DAObject.__init__` **ignores an `instanceName=` kwarg** (the
   name comes from the first positional arg or frame-`co_names` inspection), so
   every "stable" name in the seed was silently random
   (`has_nonrandom_instance_name=False`), and `DAList.append` renames such
   items into random containers. Fixing the seed fixed the crash.
2. The answer path has one genuine simulator bug (`gathered` gate, §2), and
   `set`/template usage has contract details an agent can't guess (§3, §4).

This document turns those findings into implementable work.

## 1. Upstream the `gathered` fix (simulator bug, one line)

- **Location**: `session.py`, `_apply_object_assignment` (object_checkboxes
  branch).
- **Bug**: `if hasattr(target, "gathered"): target.gathered = True` — on a
  docassemble `DAObject`, `hasattr` is **always False** for unset attributes
  (their `__getattr__` raises), so the flag never lands. The selection is
  stored, but the screen's `x.attorneys.gathered` seek never resolves → the
  same screen re-asks forever.
- **Fix**: set `gathered` unconditionally, wrapped for non-DAObject targets:

  ```python
  try:
      target.gathered = True
  except Exception:
      pass
  ```

  (Already verified locally: with the gate dropped and no driver workaround,
  both attorneys screens answered and advanced.)
- **Test**: extend `tests/test_detect_session.py::TestObjectAssignments` —
  after an `object_checkboxes` assignment, assert `target.gathered is True`.

## 2. Object-answer contract for `set` and the Session API

Verified requirements for answering object-reference fields:

1. **Deliver the screen**: `apply_assignments(..., screen=screen)` must be
   passed so object fields are detected and routed through
   `_apply_object_assignment`. `cmd_set` already does this; script drivers
   using the Session API must too. Document it on the `set` help text.
2. **Answer shape must be JSON**: `{"<safeid key>": true}` (double-quoted
   keys, JSON booleans). `parse_value()` tries JSON, then Python literal, then
   string; a single-quoted `{'<key>': true}` fails literal_eval (Python wants
   `True`) and degrades to a string → `_apply_object_assignment` raises
   `ValueError: ... must be a mapping or list`.
   - **Improvement**: if `parse_value` degrades to a string for a value that
     parseable-looks like a mapping containing `true`/`false`/`null`, raise a
     helpful error instead of the generic mapping-or-list message, e.g.:
     "object answers must be JSON: {\"<key>\": true}".
   - Alternative (decide in implementation): make `parse_value` accept
     JSON tokens inside single-quoted Python-literal input. Prefer the clearer
     error over a permissive parser.

## 3. Seed setup beyond the base A3 contract (verified additions)

The A3/README contract ("pre-create server-scoped globals with stable
non-random instanceNames") is correct but was not sufficient on its own for a
clean real-state run. Verified additions, all reproducible from the family
interview:

1. **Stable names are positional or explicit** — never `instanceName=` kwargs:
   ```python
   def _named(name):
       obj = DAObject()          # or a typed class
       obj.instanceName = name
       obj.has_nonrandom_instance_name = True
       return obj
   ```
   Put this pattern (or `fix_instance_name`) in the README example; the
   current example silently produces random names.
2. **`Address` components**: templates call `firmdata.firm.address.line_one()`
   — `line_one()`/`line_two()`/`block()` are **methods** on
   `docassemble.base.util.Address` that read `.address` (street), `.city`,
   `.state`, `.zip`. Set `.address`, never `.line_one` (assigning the method's
   name shadows it → `TypeError: 'str' object is not callable`).
3. **Reference-resolution roots**: this interview's reference lists resolve
   instanceName paths through `get_info()` (see `get_referenced_item_parts` in
   `functions.py`); on the server an `initial` code block calls
   `set_info(...)` to register roots. The simulator does not reproduce that,
   so the seed must:
   ```python
   from docassemble.base.functions import set_info
   set_info(firmdata=firmdata, M=M)   # any top-level DAObject root the
                                      # interview references
   ```
   - Follow-up (recommended): the simulator should register the session's
     top-level DAObject roots itself on each pass (mirroring the server's
     `initial` behavior) so seeds don't need to. Spike first: confirm which
     roots `set_info` needs across `fresh_user_dict` + `run_config` + render.
4. **Template built-ins**: `ordinal_number` (and similar docassemble base
   functions) are template globals on the server; the simulator's docx render
   context only exposes the namespace, so bind them in the seed (as the render
   fixture already did):
   ```python
   from docassemble.base.functions import ordinal_number  # noqa
   ```
   - Consider (optional): inject docassemble base text functions as render
     globals so the seed doesn't need to.
5. **Attorney data**: CRIFS renders `P.rep.attorneys.item(0).bar_id` and
   `.email`; give seeded attorneys `bar_id`, `email`, `name`, and set
   `gathered`/`there_are_any` on the lists. (Template-driven; document as
   "add whatever the interviewed templates read".)

## 4. Error messages and CLI exposure ("not turnkey" story)

The tool is deliberately setup-requiring; make that legible:

1. **`check`/`info`**: add a soft preflight note when a flow hits unresolved
   reference resolution (root-object warning is already logged by the
   interview) or when templates reference `.bar_id`-style attorney fields —
   point at the README seed contract. Keep it a warning, not a failure.
2. **Ask-time `DASourceError` from parse.py:6483**: when the cause is a
   choice-object `NameError` (random container), the error is opaque. The
   simulator can wrap/prepend: "a choice object has an unstable
   instanceName — pre-create it with a stable name in
   `.config/simulator/config.py` (see README 'Pre-seed server-scoped
   globals')".
3. **`_apply_object_assignment` ValueError**: add the JSON hint (§2).
4. **README**: fold §3 into the existing seed-script guidance (the current
   example must use positional/explicit names, and mention `set_info` roots +
   `Address`).

## 5. Open items (real-state render tail)

Verified states, not yet root-caused to a single line:

1. **`result['filename']` is None at save time** (parse.py:6997,
   `save_numbered_file(result['filename'] + '.docx')`) after the docx renders
   fully. The attachment name (`Parenting Plan - ${M.parties.client}
   (Original)`, x_attachments.yml:1894) renders fine (`M.parties.client` is a
   valid entity), so the None comes from the compiled attachment filename
   path. The family attachment blocks are generic-flow attachments not
   reachable via `questions_by_name` in the probe — spike: locate the compiled
   `Attachment`/options for the variable
   `x.original[0].family_parenting_plan` and check whether a
   `filename:`/name-derived default yields `None`; fix direction is either an
   interview-side `filename:` option or a simulator default-naming fallback.
2. **`--no-flow` render of the saved session reports "attorneys.gathered is
   not defined" (paragraph 19) even though the executed session shows
   `gathered: True`** — the strict render path evaluates the reference lists
   differently (and logs "Root object 'firmdata' not found" before failing).
   Verify whether the seed's `set_info` (§3.3) + the §1 fix make the render
   pass; if not, the render context needs the same root registration the
   drive's answer path had.
3. Party members carry random-identifier instanceNames
   (`M.parties[0]['01a03a…']`), which is how this interview builds entities —
   fine once references resolve; no action beyond verifying.

**Implementation note:** the two render-tail items are addressed in the
simulator. Nameless compiled attachments receive a safe fallback filename and
simulator-local file storage through the bootstrap hooks; `--no-flow` also runs
the authored seed without assembling, so `set_info()` roots are available to
strict template rendering. The fallback is limited to `filename is None` and
does not change explicitly named attachments.

## 6. Implementation order, tests, acceptance

Order (each independently shippable):

1. §1 `gathered` gate + test.
2. §2 parse/JSON error hint + `set` help text.
3. §3.3 `set_info` root registration spike → implement simulator default
   (register top-level DAObject roots per pass) with seed fallback; test that
   a minimal reference list resolves during render.
4. §4 docs + CLI messaging (README, `check`/`info` preflight, wrapped
   errors).
5. §5.1 filename-None spike (open).

Acceptance criteria:

1. Drive with no workarounds: seed per §3 + upstreamed §1 fix → both
   attorneys screens answer, the interview completes, and
   `family_parenting_plan.docx` renders through CRIFS.
2. `test_detect_session` covers the `gathered` flag and a JSON-answer
   path; new tests for `set_info` root resolution.
3. Error messages from §4 reproduced, improved, and covered by tests where
   feasible.
4. README and `docs/workspace-layout-and-fidelity.md` (A3) match the §3
   contract (stable names, `set_info`, `Address`, JSON answers).
5. Either the filename-None and paragraph-19 render items are resolved or
   documented as blocked with the reproduction above.