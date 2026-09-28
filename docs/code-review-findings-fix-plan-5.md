# Code-review findings fix plan 5 (0384cbc)

Status: proposed — not yet implemented.

Review scope: `origin/main...HEAD` on `main` (1 commit, 3 files, +172/−8),
two-axis review of `0384cbc fix: apply object checkbox answers in the interview
namespace`.

Spec source: the commit body. No issue is linked to the commit and no spec file
exists under `docs/`, `specs/`, or `.scratch/`, so the commit body's stated
contract and its verification claim are the spec.

Standards sources: `CONTEXT.md`, `docs/agents/domain.md`, `docs/adr/0001`–`0013`,
`README.md` §Development, and the conventions previously enforced in
`docs/code-review-findings-fix-plan-3.md` / `-4.md`.

Server references, each verified against the installed runtimes rather than
recalled: 1.10 `docassemble/webapp/interview/views.py`, 1.9
`docassemble/webapp/server.py`, `docassemble/base/standardformatter.py`,
`docassemble/base/parse.py`, `docassemble/webapp/config.py`.

## Correction to the review that produced this plan

The review reported "the server never clears". **That is wrong**, and the
correction sharpens the fix rather than weakening it.

`views.py:1756-1759` (1.9 `server.py:8262-8265`) does exactly this, in the
interview namespace:

```python
# fmt: off
if empty_fields[orig_key] in ('object_multiselect', 'object_checkboxes'):
    ensure_object_exists(sub_indices(key, user_dict), empty_fields[orig_key], user_dict)
    exec(key + '.clear()', user_dict)
    exec(key + '.gathered = True', user_dict)
# fmt: on
```

So `clear()` + `gathered = True`, exec'd in `user_dict`, is a real server
behaviour — the commit's frame reasoning is sound and the `clear()` is not
invented. The real defect is that the simulator implements **only this path**
and applies it to **every** group submission, while the server has two
mutually exclusive paths.

## Findings being fixed

Standards:

- **S1** — the frame/namespace decision behind `0384cbc` lives only in a code
  comment (`execution.py:1588-1591`). ADR-0013 owns the `answer` contract and
  already names `object_checkboxes`. `fix-plan-4.md:54` is direct precedent:
  "only recorded in a code comment … It needs an ADR."
- **S2** — the carrier-plus-`exec` dance is now duplicated inside
  `_apply_object` (radio branch `1558`; group branch `1591-1601`), beside the
  pre-existing `__dasimulator_value` idiom at `1381-1392`/`1403`.
- **S3** — `__dasimulator_object` is seeded as `None` and only becomes a real
  object per iteration; the name hides the placeholder role.
- **S4** — the carrier is written into the interview namespace and popped
  unconditionally (`1605`), so a pre-existing key of that name is **destroyed**
  rather than restored. `_set_namespace_value` (`1381-1392`) already does the
  save/restore correctly.

Spec:

- **P1 (worst)** — the two server paths are conflated, so off-screen list
  elements are destroyed, the `.elements` membership guard is lost, and an
  unticked choice is indistinguishable from a choice that was never rendered.
- **P2** — `eval(variable, namespace)` (`1572`) is now a bare existence probe
  whose result is discarded, leaving the target resolved twice.

Fidelity, found while verifying P1:

- **F1** — the server's normal per-choice path is not implemented at all.
- **F2** — an *unsubmitted* object group whose choices resolved to zero stays
  undefined (`_browser_blank`, `execution.py:1350-1352`), where the server
  clears and gathers it.

## The server contract, exactly

Both paths are keyed on `parse.is_empty_mc(field)` (`parse.py:302-313`, 1.9
`parse.py:510-521`): true when `choicetype` is `compute`/`manual` **and** the
resolved pairlist is empty. The paths never both run, because the per-key loop
skips a field that is in `empty_fields` (`views.py:1437`, `1595`:
`elif set_to_empty in ('object_multiselect', 'object_checkboxes'): continue`).

**Path A — the field rendered choices.** The formatter emits one checkbox per
choice and registers *every* one in the `_checkboxes` hidden input with
`'False'` (`standardformatter.py:1777-1791`), so an unchecked choice is posted
as `'False'`. The server resolves each posted key
(`views.py:1430-1440` → `1699-1711`, 1.9 `server.py:8206-8229`) to
`_internal['objselections'][<var>][<choice>]` and then:

```python
if <ticked>:
    if <data> not in <var>.elements:
        <var>.append(<data>)
else:
    if <data> in <var>.elements:
        <var>.remove(<data>)
```

- no `clear()`, and **no `gathered` write**;
- membership is tested against `.elements`, not the object itself;
- elements with no checkbox on the page are left alone;
- a posted value outside `'True'`/`'False'`/`'None'` is silently skipped;
- if the target is undefined, the browser's `<var>.gathered` post drives
  `ensure_object_exists` before the appends (`views.py:1202-1207`).

**Path B — the field rendered no choices.** The field lands in the formatter's
`hiddens` (`standardformatter.py:1293-1295`) and reaches the server as
`empty_fields`, either round-tripped through the `_empties` hidden input
(`standardformatter.py:1876-1877`, consumed at `views.py:873-890`) or recomputed
as `field_info['hiddens']` (`views.py:981`). `STRICT_MODE` defaults to false
(`webapp/config.py:163`), but **both** modes end up with the same contents, so
Path B behaves identically either way. It is the `clear()` + `gathered` snippet
quoted above.

`DAList` proxies `append`/`remove`/`clear`/`elements` to its underlying list, so
this generated source is portable across `DAList` and walkup-style reference
lists.

## Target design (work items 1–4)

Deepen the seam into one helper per concern instead of one function that does
everything.

### 1. One namespace-exec seam (S2, S3, S4)

```python
#: Slot used to carry a Python value into an exec'd interview statement.
_CARRIER = "__dasimulator_object"


def _exec_in_namespace(namespace, source, carrier=_MISSING):
    """exec ``source`` with the interview namespace as globals, optionally
    binding the carrier slot, restoring whatever occupied it.

    Answers are applied by exec'ing in the namespace rather than by calling
    methods directly because docassemble's frame-walking utilities (1.9's
    ``get_user_dict()``, which scans frames for ``_internal``) resolve the
    interview from the caller's frame.
    """
```

`_set_namespace_value` is refactored onto it, so the carrier save/restore has
exactly one implementation and a pre-existing carrier key is restored rather
than dropped. The `__dasimulator_object` spelling then appears once (S3), and
the "why exec" rationale is stated once.

### 2. Name the existence probe (P2)

```python
def _ensure_object_target(namespace, variable, datatype):
    """Create the group target when the flow has not defined it yet."""
```

Absorbs the discarded `eval` probe at `1572` and its `ensure_object_exists`
fallback, giving the double resolution a name and a reason.

### 3. Split the two paths (P1, F1, F2)

```python
def _apply_object_choice(namespace, variable, datatype, value, choices):
    """Path A: the browser posted one key per rendered choice."""


def _clear_empty_object_group(namespace, variable, datatype):
    """Path B: the field rendered no choices (``parse.is_empty_mc``)."""
```

`_apply_object` becomes a dispatcher: radio datatypes keep their current branch;
`choices` empty selects Path B; otherwise Path A.

Path A builds a per-choice state map before mutating:

```python
states = {key: False for key in choices}  # every rendered choice is posted
for key, raw in _iter_object_choices(value):
    if key not in choices:
        raise ValueError(f"unknown object choice {key!r} for {variable}")
    states[key] = _checkbox_state(raw)
```

where `_checkbox_state(raw)` returns `True` for `True`/`"True"`, `False` for
`False`/`"False"`, and `None` for `"None"`/anything else — the last mirroring
the server's `continue`, which leaves the element untouched rather than
removing it. Path A then applies each choice with the membership guard, using
`.elements`:

```python
for key, state in states.items():
    if state is None:  # 'None' or an out-of-trio value: the server `continue`s
        continue
    if state:
        f"if {_CARRIER} not in {variable}.elements:\n    {variable}.append({_CARRIER})"
    else:
        f"if {_CARRIER} in {variable}.elements:\n    {variable}.remove({_CARRIER})"
```

The discriminator is `objselections[variable] == {}`. docassemble derives
`objselections` from the same pairlist `is_empty_mc` inspects, so the two are
equivalent for a well-formed screen; the equivalence is asserted by test rather
than assumed.

**Deliberate divergence, to record not hide:** Path A keeps writing
`gathered = True`, which the server's Path A does *not* do. Without it the
screen's `<var>.gathered` seek never resolves and the screen re-asks forever
(`docs/fidelity-round-2.md` §1). It is simulator bookkeeping, not fidelity, and
the ADR must say so.

### 4. Reach the empty-choice path for unsubmitted groups (F2)

`_blankable_fields` skips any field whose variable already evaluates, so an
undefined zero-choice group is the only case that reaches `_browser_blank`.
`_submit_screen_fields` (`1284`) gains an explicit branch: when the datatype is
an object group and the field described no choices, call
`_clear_empty_object_group` instead of assigning a blank.

## 5. ADR, and two corrections to ADR-0013 (S1)

New `docs/adr/0014-object-group-answers-follow-the-two-server-paths.md`
(`status: accepted`), recording: the frame reason for exec'ing in the namespace;
the two paths and their discriminator; the `.elements` guard; the `gathered`
divergence and its justification; the list-form inference below; and the
retained divergences.

`docs/adr/0013-answer-submits-the-whole-screen.md` needs two corrections, both
of which this research falsified:

1. "Hidden (`show if`) fields are not defined and not assigned" conflates
   `show if` with the server's `hiddens`/`empty_fields`, which are **empty
   multiple-choice** fields (`is_empty_mc`), not show-if-hidden ones. Object
   groups among them *are* cleared and gathered.
2. "Known gaps — `object_multiselect` and `object_checkboxes` … are left
   undefined because a fabricated empty object or value can corrupt interview
   logic" is superseded by Path B, which clears and gathers instead.

`CONTEXT.md`'s "Screen submission" entry gains the group-field rule.

## 6. List-form inference (call out for review)

`_apply_object` accepts a JSON list as well as the documented dict. Under the
browser model every rendered choice is posted, so for a **list** submission an
on-screen choice that is absent from the list is **unticked** and therefore
removed. This is the only genuinely new inference in the change, it changes
list-form semantics, and the ADR must state it. `--code` remains the escape
hatch for deliberate state surgery.

Deliberately **not** in scope: a visible group with choices that the caller does
not submit at all. Today it is left undefined, and resolving it interacts with
ADR-0013's separate "prefilled values are never overwritten" rule. It stays a
documented boundary with a test, and gets its own decision later.

## Tests

Characterisation first — each new test must fail against `origin/main`'s
`execution.py`, verified with the throwaway-worktree technique used for this
review (`git worktree add … HEAD`, then `git checkout origin/main -- src/…`).

`tests/test_detect_session.py`:

- Upgrade `NamespaceAwareList` to model `DAList` (`.elements`, `append`,
  `remove`, `clear`) so the generated source's `.elements` access is exercised
  at the stub seam. Today's double is a bare `list` subclass and would not
  catch a wrong membership target.
- Path A: an element with no checkbox on the page survives; an existing
  selection is not duplicated; an unticked choice currently in the list is
  removed; surviving order is preserved; `'None'` leaves the element alone.
- Path B: a zero-choice group is cleared and marked gathered, including when
  the caller never submitted it (F2).
- Both `append` and `remove` resolve `get_user_dict()` (extends the existing
  frame test, which only covers `append`).
- `gathered is True` after a normal selection — the `fidelity-round-2.md` §1
  regression guard.
- Unknown keys are rejected whether ticked or unticked.
- `object_radio` still assigns (guard for the untouched branch).

`tests/test_real_runtime.py`:

- Extend `OBJECT_NAMESPACE_FIXTURE` with a pre-seeded element that has no
  checkbox, and assert it survives, that the unticked choice is removed, and
  that `gathered` is true — on both families.
- Add a zero-choice fixture (`choices: |` evaluating to `[]`) asserting the
  clear-and-gather path on both families.

## Order of work

1. ADR-0014 plus the ADR-0013 corrections land **first**, so the contract is
   agreed before behaviour moves.
2. Characterisation tests (red on both lanes).
3. `_exec_in_namespace` seam and the `_set_namespace_value` refactor —
   behaviour-preserving, so the fast suite must stay green (S2, S3, S4).
4. `_ensure_object_target` extraction (P2).
5. Path A / Path B split and the dispatcher (P1, F1).
6. F2 wiring in `_submit_screen_fields`.
7. Lanes below.

## Validation

- `mise run lint` (ruff, tombi, pyrefly) clean; `mise run test:fast` green.
- `mise run test:all-da` green. README names this the manual regression gate
  that must pass before runtime-compatibility changes merge (issue #1, story
  23).
- Corpus lane over the object-checkbox fixtures
  (`examples/object-checkboxes-custom.yml`, `object-checkboxes-copy.yml`,
  `testobjectlist.yml`).
- Re-run the walkup object-references repro on 1.9 — the motivating case for
  `0384cbc`.
- Revert-check: the change is confined to one commit and is revertible on its
  own; the ADR commit is independent of it.
