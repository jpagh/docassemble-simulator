---
status: accepted
---

# Assemble single attachment entries for attachment-aware DOCX rendering

## Context

`sim render TEMPLATE.docx` previously rendered only raw `.docx` templates directly through `docxtpl` + `custom_jinja_env()` against the prepared namespace. Interviews that declare `attachments:` blocks commonly supply template context via attachment-scoped `field code:`, `fields:`, `redact:`, generic `x` objects, index variables (`i`, `j`, …), and `current_context().attachment`. Direct rendering cannot see those bindings, forcing operators to reconstruct them manually with `--bind` or fixtures, and cannot verify `redact:` twins or attachment-scoped `include_docx_template` subdocuments.

Live probes across both runtime compatibility lanes (`da19` for docassemble 1.9.x and `da110` for 1.10.x) established four runtime constraints:

1. Driving assembly via `interview.askfor` finalizes every sibling attachment in the declaring block (so an unrelated sibling can abort the render), converts template `UndefinedError` exceptions into re-seeks and `DAErrorMissingVariable` (losing filename attribution), and can seek/execute other blocks or surface a question.
2. `interview.cache_documents` defaults to `True`, so an already-assembled `DAFileCollection` is returned unchanged across template edits unless the target variable is undefined first.
3. `DAObject.attrList` is empty for objects populated in code blocks; assembled `DAFileCollection` candidates must be discovered by walking `elements` and `vars()` with cycle and depth guards.
4. Re-entering `runtime_context()` inside the render callback clears `this_thread.current_package`, breaking `include_docx_template` and `url_for`.

## Decision

- Extend the package-private render callback seam in `execution.py` (`_with_render_state` / `_with_render_state_impl`) to pass a frozen package-private `_RenderContext(namespace, interview, status)` rather than only `namespace`. Nothing new is exported from `execution.py`, preserving ADR-0002's guarantee that mutable namespaces, compiled interviews, and statuses never escape to CLI or persistence code.
- Implement attachment entry enumeration, variable-name matching, candidate discovery, and single-entry assembly in a package-private `_attachments.py` module.
- Assemble **one** matched Attachment target directly with `Question.prepare_attachment()` and `Question.finalize_attachment()` inside the already-entered runtime context:
  - undefine both the caller's spelling and the canonical variable name (`substitute_vars_from_user_dict(intrinsic_name_of(...))`) before assembly so cached collections never mask template edits;
  - bind generic `x` and index variables (`i`…`n`) in the ephemeral namespace only;
  - rely on `_runtime.install_attachment_filename_fallback()` for DOCX-only enforcement (ADR-0001 / ADR-0006 / ADR-0011) and map `PDFConversionUnavailable` to `pdf-unavailable`;
  - copy the produced DOCX to `--output` with atomic replacement and publish one `PublishedAttachment` entry with `validate_docx_structure` diagnostics.
- Expose attachment rendering through two surfaces on `RenderRequest`, `sim render`, and the persistent worker's `render` request:
  - explicit `--attachment VAR` (or worker `attachment`), mutually exclusive with positional `TEMPLATE` and rejected with `--fixture`;
  - automatic resolution when `render TEMPLATE` targets a saved session or snapshot: match static `docx template file` entries, discover assembled Attachment candidates in the ephemeral namespace, auto-render when unambiguous (or when a single concrete entry needs no generic/index binding), and return an `input` error listing `details.candidates` when ambiguous or requiring `--attachment`. Unmatched templates and fixture sources continue using the direct render path unchanged.
- Support `--expect-missing` on attachment renders by matching both the literal operand and the resolved alias derived from the attachment's prepared `field code`/`fields` bindings. Report `paragraph: null` when docxtpl does not provide a line number, and attribute `template` to the DOCX basename (including subdocs when docassemble sets `filename`).

### Accepted deviations from server assembly

- **Single-entry assembly only.** Sibling attachments in the same declaring block are not finalized by a targeted render; siblings reuse any cached collections already in the session or remain unassembled.
- **No asking semantics.** `Question.exec_setup` (`undefine`, `need`, `reconsider`) is skipped because attachment rendering does not ask questions or seek new screens. `prepare_attachment` still executes authored `code`, `fields`, `field code`, and `valid formats` expressions inside the ephemeral namespace.

## Consequences

- `sim render` and the persistent worker can render attachment-backed DOCX templates (including generic `x`, indexed `[i]`, `field code:`, `fields:`, `redact:` twins, `include_docx_template`, and `current_context().attachment`) directly from saved sessions and snapshots without `--bind` reconstruction.
- Saved sessions and snapshots remain strictly read-only; no store write path is added.
- Each targeted attachment render writes one numbered file in `.simulator/files/`.
- `RenderResult` gains an optional `attachment` field (omitted from JSON when `None`) identifying the canonical variable assembled.
- Non-static `docx template file` (`code:`) and computed `attachment code:` entries cannot be auto-matched by filename; explicit `--attachment` still works for `docx template file: {code: ...}` entries, while `attachment code:` remains out of scope and falls through to direct rendering.
