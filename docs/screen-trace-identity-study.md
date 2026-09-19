# Screen identity study

Evidence and recommendation for the canonical `Screen identity` used by the
`Screen trace` (issue #7). The probe covered docassemble 1.10.10 and 1.9.13,
a synthetic interview exercising every question category, and the real
`docassemble-walkup` AssemblyLine interview.

## Method

- Synthetic interview: explicit `id:` blocks, a generic-object question, an
  indexed `DAList` gathered with `sets:`, a `settrue` review screen, a
  validation-code question, a terminal question, and a true completion.
- Real interview: `docassemble-walkup` walked with a typing driver; screen
  outcomes dumped verbatim.
- Edits: insert an unrelated block before the first screen; compare
  `question_name`, `sought`, and `orig_sought` before and after.
- Families: every probe repeated on 1.9.13 and 1.10.10.

## Raw fields

| Screen | `question_name` | `sought` | `orig_sought` | `fields` |
| --- | --- | --- | --- | --- |
| explicit `id:` field screen | `ID marital status` | `marital_status` | `marital_status` | `marital_status` |
| explicit `id:` inside a list loop | `ID client identity` | `clients[i].name.first` | `clients[0].name.first` | `clients[i].name.first`, ... |
| generic object | `Question_2` | `x.name` | `clients[0].name` | `x.name` |
| indexed list (`sets:`) | `Question_3` | `clients[i].complete` | `clients[0].complete` | `clients[i].name`, ... |
| `settrue` / review | `ID review` | `null` | `null` | `review_ok` |
| `deadend` terminal | `Question_5` | `null` | `null` | — |
| `deadend` terminal with id | `ID done` | `null` | `null` | — |
| true completion | — (`kind: finished`) | — | — | — |
| validation failure | unchanged active screen | unchanged | unchanged | unchanged |

`question_name` is `"ID " + id` whenever the block declares an `id:`
(docassemble's compiler assignment, verified in both supported `parse.py`
lineages). Generated names are `Question_<n>`.

## Stability under an inserted block

| Screen | Before | After |
| --- | --- | --- |
| `id: intro` | `ID intro` | `ID intro` |
| generic object | `Question_2` + `x.name`/`clients[0].name` | `Question_3` + same pair |
| `deadend` terminal | `Question_4` | `Question_5` |

Explicit ids and the `sought`/`orig_sought` pair are insertion-stable.
Generated `Question_<n>` names are positional and never identity.

## Canonical identity hierarchy

First match wins; the rule name is stored with the key (`rule` provenance).

1. `explicit-id` — `question_name` starts `ID ` → `id:<id>`. Covers field,
   review, `settrue`, continue, and terminal screens. The id wins even when
   the screen also carries a `sought` pair (`ID client identity`).
2. `targeted-variable` — `sought == orig_sought` → `var:<path>` with numeric
   indices normalized (`clients[0].name` → `clients[i].name`).
3. `generic-object` — roots differ and the `sought` root is the placeholder
   (`x.name` vs `clients[0].name`) → `generic:<resolved base><sought tail>`,
   e.g. `generic:clients.name`.
4. `list-target` — `sought` carries the placeholder index
   (`clients[i].complete` vs `clients[0].complete`) → `list:<sought>`.
5. `field-tuple` — no usable target → `fields:<sorted canonical variables>`.
6. `category` — no target and no fields → `screen:<kind>/<question_type>`.

### Normalization decisions

- Numeric literal indices (`[0]`, `['0']`) become `[i]`. Authored string keys
  are preserved. `[i]` is used rather than a simulator-invented `[*]` because
  it is the authored loop variable docassemble reports.
- Generated `Question_<n>` names are never emitted.
- The `ID ` prefix is stripped for `id:` keys.
- A root segment of exactly twelve ASCII letters in mixed case is treated as
  docassemble's generated instance name (`get_unique_name()` is
  `random_string(12)`) and rejected; derivation falls through. Observed leak:
  an error naming `CMWHWUrsOoma['advance_directive']` after a partial
  checkbox answer. The heuristic can, in principle, reject an authored
  twelve-letter mixed-case root; falling through to the field tuple or
  category is a degradation, not a mismatch.
- `--full-text` compares question/subquestion text after collapsing runs of
  whitespace and stripping the edges.

## Collisions and repeats

- Repeated identities observed: `ID client identity` and
  `ID address and contact` appear once per client; `ID client address and
  contact` was re-asked with the same identity (`sought:
  clients[i].mobile_number`) repeatedly while the typed value did not
  satisfy assembly; a validation failure leaves the active screen unchanged
  and is recorded against the same identity with the rejected assignments.
- Multiplicity is therefore a comparison-level property. The recorder stores
  a 1-based `occurrence` per identity (per phase when a phase is tagged).
- The `category` fallback can collide when an interview has more than one
  id-less, field-less terminal or continue screen. The first such screen in a
  trace keeps the base key; later ones get a trace-local `#<n>` suffix in
  first-seen order. This is the weakest rule and is intentionally
  positional: it is documented, and the `rule` provenance makes it visible.

## Recommendation

Adopt the hierarchy above as the initial identity rule, with `rule`
provenance recorded on every entry. The identity study's counterexamples are
encoded as the first fast tests for the canonicalizer.
