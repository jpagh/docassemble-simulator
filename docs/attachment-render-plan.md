# Attachment-Aware DOCX Rendering — Plan

Status: proposed (reviewed against `docassemble_simulator_handoff.md`, 2026-11-24)
Scope: `sim render` (CLI, persistent worker, `RenderRequest`) renders DOCX templates
through the compiled interview's `attachments:` blocks for saved-session and snapshot
sources, so attachment-scoped bindings (`field code:`, `fields:`, `redact:`, generic
`x`, index `i`, `current_context().attachment`) need no `--bind` reconstruction.

## 1. Review of the handoff

Verdict: implement. The goal, invariants, and verification plan are sound. Four
details change, all discovered by running docassemble's own machinery in both
runtime families (`.venv-da19`, `.venv-da110`) against a throwaway package with a
generic `x.original[0]` / `x.redacted[0]` attachment pair (`field code: {S: raw(x)}`,
`fields:`, `redact: True/False`, `include_docx_template('partial.docx')`).

### 1.1 Verified (no change needed)

- Attachment machinery is callable outside the assemble pass inside the simulator
  runtime. Assembling a single entry yields a `DAFileCollection` whose
  `info['attachment'] == {'name': <question name>, 'number': <index>}`,
  `info['formats'] == ['docx']`, and `.docx.path()` points at a numbered file in
  `.simulator/files`.
- DOCX-only is already guaranteed for `attachments:` blocks by
  `_runtime.install_attachment_filename_fallback()` → `_without_pdf_conversion()`;
  a `valid formats: code: documents.valid_formats` (`['docx','pdf']`) entry renders
  DOCX and never invokes a converter. A PDF-only entry already raises
  `PDFConversionUnavailable` (ADR-0006), which render must map to `pdf-unavailable`.
- `interview.cache_documents` is `True`: without undefining the target,
  `finalize_attachment` returns the stale `DAFileCollection` file unchanged. With
  `undefine`, a template edit on disk is picked up on the next render.
- The saved-session pickle is byte-identical after an ephemeral attachment render.
- `include_docx_template('partial.docx')` subdocs render inside the attachment path
  and see the same `x`/`S` bindings.
- `redact: False` preserves text, `redact: True` emits redaction marks; the
  `redact()` filter and `current_context().attachment` both work through this path.
- `S.notary_block_yn` fails as `UndefinedError: 'documents[...].notary_block_yn' is
  undefined` with `error.filename == '<template>.docx'` and `error.lineno is None`.

### 1.2 Corrections to the handoff

1. **Assemble one entry directly, not through `interview.askfor`.**
   `askfor` works, but it (a) finalizes *every* attachment in the declaring block, so a
   gap in an unrelated sibling aborts the render (reproduced: a session that assembled
   `original` but not `redacted` failed on the sibling), (b) converts the template
   `UndefinedError` into a re-seek and finally `DAErrorMissingVariable`, losing
   paragraph/filename attribution, and (c) may seek and execute other blocks (and could
   surface a question) while chasing the missing variable — violating invariant 3.
   Use `Question.prepare_attachment()` + `Question.finalize_attachment()` for the one
   matched entry: same real machinery (`fields`, `field code`, `code`, `redact`, docx
   templates, include passes, reference updates, numbered-file saving), with precise
   single-document semantics and preserved error attribution.
2. **`--expect-missing` needs alias resolution.** Attachment-path failures name the
   *resolved* variable (`documents['petition'].notary_block_yn`), not the written alias
   (`S.notary_block_yn`), and `paragraph` is normally unavailable (`lineno is None`).
   Match the expectation against the literal operand *and* the resolved alias derived
   from the attachment's `field code`/`fields` bindings; report both in `details`.
3. **Instance discovery must not use `attrList`.** DAObjects assembled in code blocks
   have an empty `attrList`; attributes live in `vars(obj)` / `elements`. Walk
   `elements` (dict/list/set) and `vars()` with an id-based cycle guard and a depth cap.
4. **Do not re-enter the runtime context.** The action already runs inside
   `_prepared_operation`; a nested `runtime_context()` replaces `this_thread`, clears
   `current_package`, and makes `include_docx_template` and `url_for` fail. Assembly
   runs in the entered context only.

### 1.3 Additions the handoff leaves implicit

- `RenderResult.attachment` so an auto-resolved render says which attachment it used.
- A published-attachment manifest entry for the produced DOCX (otherwise the render
  path never calls `url_for` and reports no attachments).
- Worker/CLI validation for "exactly one of template/attachment" and fixture rejection.
- Documented deviations: only the targeted entry is assembled (siblings reuse the
  session's cached collections or are not assembled at all), and question-level
  `undefine`/`need`/`reconsider` directives are *asking* semantics and are skipped.

## 2. Vocabulary (add to `CONTEXT.md`)

- **Attachment entry**: one compiled `attachments:` list item on a `Question`
  (its `variable name` template, `docx template file`, `fields`, `field code`,
  `redact`, and index within the block).
- **Attachment target**: the attachment entry plus the concrete `x` expression and index
  values that turn its variable-name template into one interview variable.
- **Attachment candidate**: an attachment target that the render namespace already
  holds as an assembled `DAFileCollection`, identified by the collection's
  `instanceName`.
- **Attachment render**: a render that assembles one attachment target through
  docassemble's attachment machinery instead of evaluating the raw template.

## 3. Interface

### 3.1 CLI

```sh
# explicit target (positional TEMPLATE becomes optional)
docassemble-simulator render \
  --attachment "documents['dor_variance_petition'].original[0].dor_variance_petition" \
  --output /tmp/petition-orig.docx

# automatic resolution for saved/snapshot sources
docassemble-simulator render brcr_eoa.docx --output /tmp/brcr.docx

# existing behaviour is unchanged
docassemble-simulator render form.docx --fixture fixture.py
```

| Rule | Behaviour |
| --- | --- |
| `TEMPLATE` and `--attachment` | exactly one; both → `input` error naming both |
| `--attachment` + `--fixture` | `input` error (fixture cannot assemble) |
| `--attachment` + `--fresh` | allowed; assemble runs first unless `--no-assemble`, and the attachment's roots must then exist |
| `--bind` | applied to the ephemeral namespace *before* assembly, so `fields`/`field code`/templates see it |
| `--expect-missing` | supported in both modes (see §6) |
| `--save-snapshot` | unchanged: written by execution before the render action |
| `--no-assemble` | unchanged: skips the interview assemble pass; attachment assembly still runs |
| `[simulator.render_bindings]` | table defaults apply; per-template keys apply only when `TEMPLATE` is given |

### 3.2 Worker request

```json
{"operation": "render", "attachment": "documents['stage'].original[0].petition", "output": "out.docx"}
```

`template` becomes optional; `attachment` is accepted with the same hyphen/underscore
spellings as the other fields. Exactly one is required, and a malformed request stays a
typed `input` protocol error. `_render_worker_fields` grows from seven to eight values.

### 3.3 Python API

```python
@dataclass(frozen=True)
class RenderRequest:
    template: str | None  # was: str
    source: RenderSource
    assemble: bool
    save_snapshot: Path | None = None
    output: Path | None = None
    expect_missing: str | None = None
    bindings: tuple[tuple[str, str], ...] = ()
    attachment: str | None = None  # appended so positional callers are unaffected


@dataclass(frozen=True)
class RenderResult:
    template: str
    paragraphs: int
    artifact: Path | None = None
    attachment: str | None = None  # canonical variable name for attachment renders
```

`template` in the result is the requested filename for auto-resolution, or the target
entry's (first) static DOCX filename for explicit `--attachment`.

### 3.4 JSON results

```json
{"ok": true, "command": "render",
 "result": {"template": "petition.docx",
            "attachment": "documents['petition'].original[0].petition",
            "paragraphs": 14, "artifact": "/tmp/petition-orig.docx"},
 "attachments": [{"filename": "Petition_Original.docx", "extension": "docx", "...": "..."}]}
```

```json
{"ok": false, "command": "render",
 "error": {"kind": "input",
           "message": "petition.docx matches 2 attachment variables; pass --attachment",
           "details": {"candidates": ["documents['petition'].original[0].petition",
                                      "documents['petition'].redacted[0].petition"]}}}
```

```json
{"ok": false, "command": "render",
 "error": {"kind": "render", "message": "'documents['petition'].notary_block_yn' is undefined",
           "details": {"template": "petition.docx", "error_type": "UndefinedError",
                       "paragraph": null,
                       "sought_variable": "documents['petition'].notary_block_yn",
                       "expected_missing": "S.notary_block_yn"}}}
```

## 4. Resolution

### 4.1 Explicit target (`--attachment VAR`)

1. Enumerate attachment entries: every question in `interview.all_questions` with
   `attachments`, recording `question`, `attachment`, the raw `variable_name` template,
   the static `docx_template_file` filenames (`FileInPackage.original_reference()`;
   code-based template files are not statically known), and `question.is_generic`.
2. Match `VAR` against each entry's template:
   - leading `x` is a placeholder only when `question.is_generic` (root expression
     group), and `[i]`…`[n]` are index placeholder groups (`-?\d+`);
   - all other text (dotted names, quoted keys) must match literally.
   Match results are `(entry, x_expression, index_values)`.
3. 0 matches → `input` error: "no attachment block declares VAR", listing entry
   templates that share the variable's last segment. >1 → `input` error listing them.
4. Assemble (§5), then verify the produced collection has a `docx` format; if the entry
   is a `manual`/`manual code` or non-DOCX entry, `input` error before assembly.

### 4.2 Automatic (`render TEMPLATE` on saved/snapshot sources only)

1. Entries whose static DOCX filename matches `TEMPLATE` (basename or nested relative
   path, as `find_template` accepts). No match → existing direct render path unchanged.
2. Collect candidates: walk the ephemeral namespace for values with
   `info['attachment']` dicts; keep those whose
   `(info['attachment']['name'], info['attachment']['number'])` equals the entry's
   `(question.name, attachment['indexno'])`. Candidate variable =
   `value.instanceName`; dedupe and sort.
3. Decide:

| Situation | Result |
| --- | --- |
| exactly 1 candidate | attachment render of that variable |
| 0 candidates, 1 matching entry, template fully concrete (no `x`, no `[i]`) | attachment render of the template variable |
| 0 candidates, matching entries need a generic/index binding | `input` error naming the entry template and suggesting `--attachment` |
| >1 candidate | `input` error listing every candidate variable |
| ≥1 matching entry, 0 candidates and no concrete entry (e.g. `--no-assemble` on an unfinished session) | `input` error explaining that the session has no assembled instance |

Fixture sources never reach step 1 (fixture renders stay direct; `--fixture` with
`--attachment` is rejected in validation).

## 5. Execution path

### 5.1 Private seam (`execution.py`)

`_with_render_state` / `_with_render_state_impl` currently call `action(namespace)`
while `interview` and `status` are in scope. Change the private callback to receive a
frozen package-private context:

```python
@dataclass(frozen=True)
class _RenderContext:
    namespace: dict[str, Any]
    interview: Any
    status: Any
```

`action(context)`; `render.py` is the only caller. Nothing new is exported; ADR-0002's
contract ("mutable namespaces, compiled interviews, statuses ... never escape to CLI or
persistence code") is unchanged because the callback is package-private. No store write
path is added, so saved sessions and snapshots remain read-only.

### 5.2 Assembly (`_attachments.py`, new package-private module)

```python
def assemble(namespace, target) -> AttachmentAssembly:
    canonical = substitute_vars_from_user_dict(
        intrinsic_name_of(target.entry.template_name, the_user_dict=namespace),
        namespace,
        is_generic=target.entry.question.is_generic,
    )
    undefine(target.variable, canonical)  # docassemble's own undefine
    namespace.update(target.bindings)  # x = eval(root), i/j/... = int
    question = target.entry.question
    prepared = question.prepare_attachment(target.entry.attachment, namespace)
    finalized = question.finalize_attachment(
        target.entry.attachment, prepared, namespace
    )
    collection = eval(canonical, namespace)
    ...
```

- Undefine both the caller's spelling and the canonical name so a double-quoted
  variant cannot leave a stale collection behind.
- Bind `x` with `eval(root_expression, namespace)` and each index as `int(value)`;
  mirror docassemble's `substitute_vars_from_user_dict` semantics (no other names).
- Skip `exec_setup` (it runs `undefine`/`reconsider`/`need`, i.e. asking semantics) —
  documented deviation.
- Do not re-enter `runtime_context`; the action is already inside it.
- Extract `collection.docx.path()`; assert `'pdf' not in finalized['formats_to_use']`
  and that a `docx` file number exists (defensive check for §1.1's guarantee).
- Count paragraphs from the produced `word/document.xml`.
- Copy to `--output` with `atomic_replace(..., lock_destination=True)` (destination
  safety is already validated by execution's `validate_destinations`).
- Publish one `PublishedAttachment` for the produced DOCX (via a new
  `_artifacts.publish_attachment(path, filename)` helper that appends to the active
  `capture_published_attachments()` list, with `validate_docx_structure` diagnostics).
  Publish the copied artifact when `--output` is given, else the numbered file.

### 5.3 Files

| File | Change |
| --- | --- |
| `src/docassemble_simulator/execution.py` | `_RenderContext`; callback signature |
| `src/docassemble_simulator/_attachments.py` (new) | entry enumeration, template matcher, target resolution, candidate discovery, assembly |
| `src/docassemble_simulator/render.py` | request/result fields, mode dispatch, error mapping, expectation aliases, artifact copy helper |
| `src/docassemble_simulator/_artifacts.py` | `docx_paragraph_count(path)`, `publish_attachment(path, filename)` |
| `src/docassemble_simulator/cli.py` | `template` `nargs="?"`, `--attachment`, `_render_request`, `cmd_render`, `_worker_render_fields`, validation messages |
| `README.md` | Rendering section, worker render request, JSON examples, deviations |
| `docs/template-render-design.md` | §9 "future work" now implemented → point at this plan |
| `docs/adr/0019-attachment-aware-render.md` (new) | decision + deviations |
| `CONTEXT.md` | the four terms from §2 |

## 6. Errors and `--expect-missing`

| Failure | Kind | Details |
| --- | --- | --- |
| `UndefinedError` (template, incl. included subdoc) | `render` | `template` (resolved DOCX basename), `error_type`, `paragraph` (when present), `sought_variable` |
| `DAAttributeError` / `DAErrorMissingVariable` (missing attribute) | `render` | `sought_variable` from `extract_missing_name` / `error.variable` |
| `TemplateError` with `filename` | `render` | attributed to the subdoc when it resolves under a `data/templates` directory |
| `PDFConversionUnavailable` | `pdf-unavailable` | `error_type`, target variable |
| `DASourceError` (missing template file, bad option) | `render` | `error_type`, message |
| unknown variable, ambiguity, fixture+attachment, both surfaces | `input` | `candidates`, `template` |

Expectation matching (in attachment mode):

1. pass if the error's message quotes the requested operand (`'S.notary_block_yn' is
   undefined`) — today's `missing_error_matches` behaviour;
2. else resolve the operand's leading binding name through the attachment's prepared
   `field_data` (`field code`/`fields` bindings, `x`/`i`) and pass when the message
   quotes the resolved name (`'documents['petition'].notary_block_yn' is undefined`);
3. else the failure is reported (details carry `expected_missing` and the resolved
   name for the operator).

A satisfied expectation returns `ok: true` with `paragraphs: 0` and no artifact (the
same "assert it stays missing" semantics as direct rendering).

## 7. Invariants

Handoff invariants 1–8 are kept, plus:

- The action never writes a session, snapshot (unless `--save-snapshot`), or template.
- Attachment assembly runs in the prepared context; no nested runtime context.
- Only the targeted entry is assembled; siblings are not finalized by the render.
- `paragraph` may be `null` on the docxtpl path; `template` attribution is the DOCX
  basename, not a YAML location.
- Exactly one numbered file is written per targeted render (siblings hit
  `cache_documents` and are reused, when present).

## 8. Work breakdown

### Phase 0 — Decide and record

1. Create the GitHub issue from this plan (`gh issue create`, heredoc body, labels
   `enhancement` then `ready-for-agent` after triage per
   `docs/agents/issue-tracker.md`).
2. Write `docs/adr/0019-attachment-aware-render.md`; add the §2 terms to `CONTEXT.md`.
3. Update `docs/template-render-design.md` §9.

Done when: ADR accepted in review; issue carries the acceptance criteria below.

### Phase 1 — Private seam + assembly module (no CLI change yet)

- `_RenderContext` in `execution.py`; `render.py` adapted; existing tests pass.
- `_attachments.py` with `AttachmentEntry`, `compile_matcher`, `match_variable`,
  `resolve_targets`, `discover_candidates`, `assemble_attachment`,
  `canonical_variable_name`.
- `_artifacts.py` helpers.
- Unit tests (`tests/test_attachment_render.py`): matcher (generic root, `[i]`…`[n]`,
  quoted keys, nested template paths, code-based templates excluded, ambiguity),
  candidate discovery with fake objects (`elements` dict/list, `vars()`, cycles,
  `attrList` empty), canonical-name substitution with a stub namespace, DOCX paragraph
  counting from a generated zip.

Done when: `uv run pytest tests/test_attachment_render.py tests/test_render.py
tests/test_render_contract.py` passes; no behaviour change for existing renders.

### Phase 2 — Explicit `--attachment` end to end

- `RenderRequest.attachment`, `RenderResult.attachment`, mode dispatch, error mapping,
  expectation aliases, artifact copy + publish.
- CLI `--attachment`, validation rules, `_render_request`; worker `attachment` field;
  parser/help/README text.
- Update `tests/test_cli.py`, `tests/test_worker_render.py` (mapping, absence of
  template, rejection cases, JSON shapes).
- New real-runtime test module (`tests/test_attachment_render_real_runtime.py`) with a
  package fixture modelled on `test_issue88_pdf_unavailable_real_runtime.py`:
  generic attachment pair with `field code S: raw(x)`, `fields`, `redact` twins,
  `valid formats: code: documents.valid_formats`, an `include_docx_template` partial
  and a `current_context().attachment`/`redact()` expression; a code block assembles
  both variants so the saved session holds them.

Done when: `render --attachment ...` with **no `--bind`** produces a DOCX whose text
contains `S`-bound values, the subdoc, and redacted/unredacted text as specified by
`redact:`; the saved session file hash is unchanged; `--expect-missing` works for both
`S.flag` and `documents[...].flag`; `valid formats` containing `pdf` produces DOCX only;
a PDF-only attachment reports `pdf-unavailable`; the worker protocol accepts
`attachment`; `--fixture`+`--attachment` is an `input` error.

### Phase 3 — Automatic resolution

- Entry matching + candidate selection + four-way decision table (§4.2), candidate
  errors.
- Real-runtime tests: single instance auto-resolves with no flags; the dual-variant
  template reports both candidates; a standalone partial still takes the direct path;
  a multi-stage session (`documents[stage_a]`, `documents[stage_b]`) auto-resolves only
  when unambiguous and never binds the wrong stage; `--no-assemble` on an unfinished
  session gives the "no assembled instance" error.

Done when: handoff §"Suggested Verification Plan" cases 1–3 of the end-to-end section
are covered by `family_python`-parameterized tests on both 1.9.x and 1.10.x.

### Phase 4 — Docs and compatibility pass

- README § Rendering: `--attachment`, auto-resolution, error/deviations tables, JSON
  examples, DOCX-only statement, "render writes one numbered file" note.
- Worker README paragraph and `--help` text.
- `docs/adr/0019`, `CONTEXT.md`, `docs/template-render-design.md` §9.
- `docs/runtime-compatibility.md` untouched (no new capability gate).

Done when: `mise run lint` is clean and every new flag/field is documented where the
existing render surface is documented.

### Phase 5 — Verification

```sh
mise run lint
mise run test:fast
mise run test:py310
mise run test:runtime        # both docassemble families + 3.10 lane
mise run test:all-da         # broadest lane
```

Manual, recorded in the issue (not automated here): against
`docassemble-automatedpleading`, from a saved `dept_of_revenue_variance` session —
`render --attachment "documents['dor_variance_petition'].original[0].dor_variance_petition"`
(notary block per `notary_block_yn`, no CRIFS) and the `redacted[0]` twin (CRIFS
included with `Document Filed`, employer details redacted), plus
`render certificate_of_service_standalone.docx` auto-resolving with `S.cos.*`.

## 9. Test matrix (handoff verification plan → tests)

| Requirement | Test |
| --- | --- |
| `field code: {S: raw(x)}` in main + `include_docx_template` subdoc, no `--bind` | real: assertions on `word/document.xml` text |
| multi-stage session binds the right stage, no session mutation | real: `documents[stage_a]`/`stage_b` fixture + session hash |
| dual-variant template reports both candidates | real: `input` error, `details.candidates` |
| explicit original vs redacted honour `redact:` and `fields:` | real: text assertions, redaction marks |
| indexed `[i]` binds `i` for 0 and 1 | real: `x.original[i].*` fixture, two renders |
| `valid formats` containing `pdf` stays DOCX | real: generated file is a DOCX zip, no `.pdf` in the registry |
| PDF-only attachment | real: `pdf-unavailable` error kind |
| `--expect-missing` on `S.flag` / resolved name | unit (alias resolution) + real (both spellings) |
| `--fixture` unchanged | unit (`test_render.py`) + CLI |
| no session mutation | real: hash `.simulator/sessions` before/after |
| stale collection re-render | real: edit template on disk, render twice, bytes differ |
| worker request mapping | unit (`test_worker_render.py`) |
| CLI validation (both surfaces, fixture) | unit (`test_cli.py`) |

## 10. Risks and open questions

- **Sibling fidelity.** A real server assembles every entry in a block. We assemble only
  the target; a sibling's newly edited template is not refreshed by this render. Accepted
  and documented; if it bites, add `--attachment-all` later.
- **Asking semantics.** `exec_setup` is skipped, so a block's `undefine`/`need`/
  `reconsider` directives do not run. `prepare_attachment` still evaluates `code`,
  `fields`, `field code`, and `valid formats`, i.e. authored code runs — the same as a
  server assembly, and only in the ephemeral namespace.
- **Paragraph attribution.** `lineno` is absent on docxtpl template errors through this
  path; the `template` field carries the DOCX basename (including included subdocs when
  docassemble sets `filename`). Keep `paragraph: null` rather than inventing one.
- **Nested attribute errors** (`S.cos.filer`) report the first missing attribute
  (`documents[...].cos`), matching docassemble's `DAStrictUndefined` message; document
  the shape.
- **Numbered-file growth.** Each targeted render saves one file in `.simulator/files`;
  the index already tolerates this. A render-scoped registry is a possible follow-up.
- **Non-static template files** (`docx template file: {code: ...}`) are not matchable by
  filename; auto-resolution skips them, explicit `--attachment` still works.
- **Multi-file attachments** (`docx template file` list) concatenate; the result's
  `template` reports the first filename.
- **`attachment code:`/computed attachments** are not addressable by variable name; out
  of scope, and auto-resolution falls through to direct rendering.
- **1.9 vs 1.10**: `prepare_attachment`/`finalize_attachment`/`all_questions` exist in
  both (probed); the real-runtime lane must still run both.

## 11. Out of scope

- AssemblyLine-only documents that never pass through an `attachments:` block.
- Doc-key/`--dockey` resolution and `render --documents`.
- Pruning or namespacing `.simulator/files`.
- Rendering the whole attachment block (all entries) in one command.
- PDF/`raw`/`md` attachment formats.
