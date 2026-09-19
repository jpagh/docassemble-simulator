---
status: accepted
---

# Fail PDF-only attachments explicitly

## Context

ADR-0001 keeps DOCX-to-PDF conversion stubbed and originally decided that
PDF-only attachments "produce no PDF artifact and do not fail the interview",
explicitly rejecting "fail when PDF is requested". That decision was made when
the alternative was failing mixed `pdf` + `docx` interviews, where a hard
failure would block validating the DOCX pipeline.

Issue #1 (story 15) requires PDF conversion to remain *explicitly* unavailable
in the simulator under both runtime families, and the 1.9/1.10 compatibility
work introduced a typed failure vocabulary (`PDFConversionUnavailable`) at the
attachment-finalization seam. Silent omission now has a cost the original ADR
did not weigh: a PDF-only attachment produces an outcome with no usable
artifact at all, with no signal that anything was requested and dropped.

## Decision

- Mixed `pdf` + `docx` attachments are unchanged from ADR-0001: generated PDF
  formats are removed before the conversion branch, DOCX renders locally, and
  the interview does not fail.
- PDF-only attachments — where removing generated PDF formats leaves no
  remaining format — raise `PDFConversionUnavailable` from the attachment
  finalization wrapper. The error names the missing PDF capability so callers
  and agents see an actionable, typed failure instead of an empty artifact set.
- Manually supplied PDF files are still not removed, and the simulator still
  never invokes an external PDF converter.

## Consequences

This partially supersedes ADR-0001: its "PDF-only attachments produce no PDF
artifact and do not fail the interview" clause is replaced by the explicit
failure above. ADR-0001's core decision — always stub the conversion boundary,
never invoke LibreOffice or any external converter — stands.

Implemented in `_runtime.py`'s attachment finalization wrapper
(`finalize_with_filename`), which counts the pre-removal PDF formats and raises
before delegating to the original finalizer when nothing remains.

> **Extended by [ADR-0011](0011-pdf-skip-docx-fallback.md):** at the
> AssemblyLine document/bundle layer, a generated-PDF request for a DOCX-backed
> document returns the DOCX rendering (with a `pdf-skip` diagnostic) instead
> of failing. PDF-only attachments keep the typed failure described here.
