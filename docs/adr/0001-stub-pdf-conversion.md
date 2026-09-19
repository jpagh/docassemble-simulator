# Keep PDF conversion stubbed in the simulator

Status: accepted

Date: 2026-08-25

The simulator is a DOCX interview runner, not a PDF-conversion environment. External DOCX-to-PDF converters such as LibreOffice introduce a server/runtime dependency that is unrelated to interview assembly and make otherwise valid document generation fail. The simulator should therefore always stub the DOCX-to-PDF conversion boundary: render and persist DOCX outputs, skip PDF conversion without invoking an external process, and clearly omit or mark PDF outputs as unavailable rather than advertising broken links.

## Decision

The simulator removes generated `pdf` formats from docassemble attachment
finalization before the conversion branch runs. DOCX formats remain unchanged
and use simulator-local file storage. PDF-only attachments produce no PDF
artifact and do not fail the interview; manually supplied PDF files are not
removed. The simulator does not invoke LibreOffice or any other external PDF
converter.

> **Partially superseded by [ADR-0006](0006-pdf-only-attachments-fail.md):**
> PDF-only attachments now raise `PDFConversionUnavailable` instead of failing
> silently. The mixed-format and no-external-converter behavior above stands.
>
> **Extended by [ADR-0011](0011-pdf-skip-docx-fallback.md):** AssemblyLine
> document and bundle `as_pdf()` requests on DOCX-backed documents return the
> DOCX rendering instead of seeking a missing generated `pdf` attribute.

## Plan

- Centralize the converter stub in the bootstrap layer so every attachment-generation path gets the same behavior.
- Remove `pdf` from simulator attachment formats before docassemble enters the conversion path; leave explicitly rendered DOCX output unchanged.
- Keep simulator-local DOCX storage and expose its artifact path through the CLI/final result instead of fabricating a PDF URL.
- Add tests proving the converter process is never invoked, DOCX attachments still validate, and final output does not claim that a skipped PDF exists.
- Update README and fidelity documentation to state that PDF generation is intentionally out of scope; server-side PDF conversion remains the responsibility of docassemble/the deployment environment.

## Implementation

Implemented in `bootstrap.py`:

- `_without_pdf_conversion()` removes generated PDF formats before
  `Question.finalize_attachment()` dispatches to `word_to_pdf` or another
  converter.
- The simulator's attachment hooks persist DOCX outputs locally and expose
  file metadata without requiring server storage. Published DOCX downloads use
  durable `file://` URIs; these are local artifact references, not fabricated
  server or PDF URLs.
- Nameless attachment filenames receive a safe fallback so a skipped PDF does
  not fail after DOCX rendering completes.

Tests cover PDF-only omission and DOCX preservation. The README and render
fidelity documentation describe PDF conversion as deployment/server scope.

## Considered options

- **Install or invoke LibreOffice:** rejected because it makes the simulator environment heavyweight and reproduces a server concern.
- **Create a fake PDF:** rejected because it would look like a usable artifact while containing no converted document.
- **Fail when PDF is requested:** rejected because PDF availability should not prevent validating the DOCX interview and template pipeline.
