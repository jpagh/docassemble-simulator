# LSP demo corpus execution audit

## Scope

Audited `/Users/jack/Projects/docassemble-yaml/lsp/tests/fixtures/examples/**/*.yml`
at fixture revision `638fb49f92ae2c11a322e737705cd235a32fb546` using
`openai-codex/gpt-5.6-luna` and `openai-codex-work/gpt-5.6-luna` execution and review agents.
The corpus contains 966 examples. Nine separate LSP regression fixtures are
excluded; the separate three-file demo-package check passed.

Runtime: `.venv-da110/bin/python`, Python 3.14.10, docassemble 1.10.13,
AssemblyLine 4.8.0, ALToolbox 0.19.0. Each case used an isolated process,
network denial, and a 30-second timeout, with four concurrent workers.

## Results

| Phase | Success | Failure | Content drift | Total |
| --- | ---: | ---: | ---: | ---: |
| Compile/check | 964 | 0 | 2 | 966 |
| Start/assemble | 894 | 70 | 2 | 966 |

Successful starts comprised 879 question outcomes and 15 immediate finished
outcomes. **No interactive answer journeys were exercised. These results do
not establish end-to-end completion or support for all examples.**

The drift exclusions are `fields-mc-9.yml` and `progressive-disclosure.yml`.
Their fixture/runtime content differs; they were not executed as substitutes.
All 70 startup failures returned structured errors (61 execution errors and
9 unresolved-variable errors), rather than crashes or timeouts.

## Reviewed findings

- **Harness defect: four `run-python-module*.yml` examples.** Package staging
  changes `docassemble.base.pandoc.__file__` to a workspace path. Upstream
  interpreter discovery consequently tries executing `pandoc.py` itself,
  producing permission errors. See package copying/permissions in
  `src/docassemble_simulator/demo_corpus.py:701-717`. This is not an authored
  interview failure. Interpreter selection needs a focused fix and regression test.
- **Simulator diagnostic gap: three PDF-link examples.** `dafile-url-for.yml`,
  `dafile-url-for-temporary.yml`, and `document-variable-name-link.yml` fail
  seeking `complaint.pdf` or `my_file.pdf`. Simulator PDF-format removal can
  cause URL lookup to enter variable seeking before attachment finalization
  emits the capability error. These must not be dismissed as authored errors.
  See `src/docassemble_simulator/_runtime.py:569-614` and the existing
  `docs/runtime-fidelity-follow-up-plan.md` PDF-lookup follow-up.
- **Eleven PDF-only requests hit a documented limitation.** These request only
  PDF output and fall under ADR-0006, not ADR-0011's DOCX fallback. Examples
  include `attachment.yml`, `formatting.yml`, and `tagged-pdf.yml`.
- **Two converter dependency failures:** the `document-docx-from-rtf` examples
  require missing LibreOffice.
- **Six optional-package failures:** missing `ics`, `gspread`, `matplotlib`, or
  `google_drive_downloader`.
- **Twelve ML/service failures:** unconfigured classification/database services.
- **Fifteen network/auth/database/configuration failures:** service-dependent
  examples cannot operate in this offline, uncredentialed environment.
- **Two manual-PDF failures:** no valid files available to concatenate.
- **Nine runtime/error demonstration or server-context failures:** examples
  include division-by-zero demonstrations, detected infinite loops, and
  missing session/task context. These observations do not establish fidelity
  in a configured server environment.
- **Six remaining unresolved-variable cases remain unproven:** `branch-error`,
  `dual-dict`, `gather`, `gather-fruit-incomplete`, `declarative`, and
  `questionless`. Some may intentionally require seed state, but startup
  errors alone do not prove authored fault. In particular, `branch-error`
  and `dual-dict` warrant focused comparison with upstream execution.

No production fixes or expectation exemptions were applied. Missing service
boundaries prevent testing beyond those points; they are not evidence that
later simulator behavior is correct.

## Reproduction and local evidence

Run from the simulator repository, with an existing valid NLTK data cache:

```sh
export NLTK_DATA=/Users/jack/nltk_data
scripts/test-demo-corpus \
  --fixtures /Users/jack/Projects/docassemble-yaml/lsp/tests/fixtures \
  --python "$PWD/.venv-da110/bin/python" \
  --compile --start --timeout 30 --jobs 4 \
  --output .simulator/demo-audit \
  --provenance-manifest tests/demo_corpus_provenance.toml \
  --expectations tests/demo_corpus_expectations.toml
```

The actual audit ran compile and start separately. Preserved local, git-ignored
artifacts contain per-interview errors and reproduction commands:

- `.simulator/demo-audit-compile/{results.jsonl,summary.json,summary.md}`
- `.simulator/demo-audit-start/{results.jsonl,summary.json,summary.md}`

Each `results.jsonl` contains 966 rows. Add
`--match '^run-python-module\.yml$'` for a focused staging-defect reproduction.

Next steps are focused regression tests/fixes for staging and PDF diagnostics,
upstream comparisons for unresolved cases, and reviewed per-interview answer
journeys. Full interactive coverage remains outstanding.
