# Skip generated-PDF requests with the DOCX rendering

Status: accepted

Date: 2026-09-18

## Context

ADR-0001 and ADR-0006 keep generated-PDF conversion out of the simulator:
attachment finalization removes generated `pdf` formats, DOCX renders locally,
and a PDF-only attachment raises the typed `PDFConversionUnavailable`. That
boundary covers the docassemble attachment path, but not AssemblyLine's
document API.

AssemblyLine's `ALDocument.as_pdf()` and `ALDocumentBundle.as_pdf()` read a
generated `pdf` attribute from the attachment's `DAFileCollection`. Because the
simulator removes that attribute, `as_pdf()` enters docassemble's
variable-seeking machinery, looks for `....pdf`, and finally raises
`DAErrorMissingVariable`. An interview's document-bundle background event
(`create_downloads`) therefore fails even though every requested document has a
successfully rendered DOCX intermediate. The failure is a simulator PDF
boundary, not an authored template failure.

Two supporting gaps surface once bundle assembly gets that far: the 1.9
runtime's `get_new_file_number`, `SavedFile`, and `secure_filename*` server
seams are unbound, so docassemble's `docx_concatenate()` and `zip_file()`
cannot create generated local files even when all inputs are DOCX.

## Decision

- Wrap `ALDocument`, `ALStaticDocument`, and `ALDocumentBundle.as_pdf` so a
  generated-PDF request on a DOCX-backed document returns that document's
  `as_docx()` rendering — the same bytes the simulator already produced —
  instead of seeking a missing `pdf` attribute.
- Record one `pdf-skip` diagnostic per substitution. The diagnostic names the
  document and the DOCX format used.
- PDF-only documents are unchanged: they still raise
  `PDFConversionUnavailable` at finalization. A bundle with no DOCX rendering
  keeps its original failure.
- The simulator does not fabricate a PDF and never invokes an external
  converter. Returned artifacts keep DOCX bytes, extension, and MIME type;
  ZIP archives and merged "full PDF" downloads contain real DOCX output.
- Preserve the active interview's generic-object placeholders (`x`, `i`,
  …) across a foreground background action. A nested event evaluates
  attachment questions that rebind `x`; restoring the placeholders keeps the
  caller's follow-up assignment (`x.generate_downloads_task = ...`) on the
  bundle object rather than the last attachment object.
- Back the generated-file seams with the local artifact registry:
  `get_new_file_number()` reserves a numbered path, the local `SavedFile`
  adapter materializes that path, and `secure_filename*` uses docassemble's
  own sanitizers. `docx_concatenate()` and `zip_file()` then create durable
  local files without a server database or remote file store.

## Consequences

- AssemblyLine bundle workflows complete under both runtime families:
  individual DOCX downloads, a ZIP of the DOCX documents, and a merged DOCX
  are published as local `file://` artifacts. `create_downloads` caches a
  complete download list instead of failing the task.
- No `.pdf` artifact is created. The "download as one PDF" label may point at
  the merged DOCX; the `pdf-skip` diagnostic is the authoritative record that
  PDF assembly was skipped, and real PDF validation remains a deployment
  concern.
- ADR-0001's core decision — always stub the conversion boundary and never
  invoke LibreOffice or any external converter — stands. ADR-0006's PDF-only
  failure is untouched.
- Docassemble code that creates new `DAFile`s (merge, ZIP, generated files)
  now works locally when a simulator file registry is active.

Implemented in `_runtime.py`
(`install_pdf_skip_fallback()`, `_SimulatorSavedFile`,
`_SimulatorRuntimeBindings.get_new_file_number`,
`_foreground_background_action()` placeholder restoration) and `_artifacts.py`
(`LocalFileRegistry.reserve()` / `remove()`).
