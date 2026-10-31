# Reviewed demo corpus audit

Status: reviewed successor to the preliminary audit.

Issue: #91 (documentation).

Supersedes: `docs/demo-corpus-audit.md` (the preliminary audit). That file is
preserved unchanged for history and must not be used as the baseline for future
sweeps. This document is the baseline.

Audit date: 2026-10-31 (America/Chicago, UTC-05:00).

Scope: the start/assemble phase of the 966-example LSP demo corpus. This is a
startup audit, not end-to-end interview validation. No interactive answer
journeys were exercised.

## What this review changes

The preliminary audit was a first pass. It correctly identified that the corpus
needed triage, but four of its conclusions are superseded:

1. The four `run-python-module*.yml` failures were not a package-copying or
   file-permission defect. They were an interpreter-derivation defect in
   upstream `run_python_module` over the intentionally staged package tree.
   Issue #87 now binds the subprocess runner to the active interpreter.
2. The PDF story is one capability finding, not three separate ones. The three
   stripped `.pdf` link examples, the eleven PDF-only examples, and the two
   manual-concatenation examples all now report the typed `pdf-unavailable`
   capability. The manual-concatenation pair is folded into the PDF finding.
3. The six unresolved-variable cases now have per-fixture verdicts. Four are
   authored/incomplete fixtures; `branch-error` and `dual-dict` close to a
   time-boxed upstream-seeking verdict. None is an open question.
4. The one remaining startup failure that needs an asset absent from the
   corpus, `objects-from-file-template.yml`, is stated as such.

The fresh sweep is the saved `.simulator/demo-audit-issue89-90-start` run. Its
observed rows were not rerun for this review. A tightened postprocessing pass
recomputed blocker attribution from those identical rows into
`.simulator/demo-audit-issue89-90-start-blockers-v2`; that pass is not an
execution and did not change any observed failure. The only changed row field
in `results.jsonl` is `redis.yml`'s blocker (`service` to none), because its
saved diagnostic has no service context. Section [Executed sweep versus postprocessing](#executed-sweep-versus-postprocessing)
makes that distinction explicit.

No runtime or corpus code, expectation entry, or production behavior was
changed for this documentation issue. The fixes and expectations described here
were implemented by issues #87, #88, #89, #90, and #92.

## Provenance

### Fixture and runtime identity

| Item | Value |
| --- | --- |
| Fixture root | `/Users/jack/Projects/docassemble-yaml/lsp/tests/fixtures` |
| Fixture revision | `638fb49f92ae2c11a322e737705cd235a32fb546` (2026-10-25 17:21:34 -0500) |
| Fixture checkout state | clean; no modified or untracked files in the fixture repository |
| Corpus | 966 `examples/*.yml` fixtures |
| Excluded regression fixtures | nine files under `lsp/tests/fixtures/regressions/`; not part of the 966 |
| Demo package check | `demo_package` three-file compile check passed |
| Interpreter | `/Users/jack/Projects/docassemble-simulator/.venv-da110/bin/python` |
| Python | 3.14.10 (Clang 23.1.3) |
| docassemble base / demo / webapp | 1.10.13 / 1.10.13 / 1.10.13 (from saved summary metadata) |
| AssemblyLine | 4.8.0 (from installed distribution metadata, not `summary.json`) |
| ALToolbox | 0.19.0 (from installed distribution metadata, not `summary.json`) |
| Provenance manifest | `tests/demo_corpus_provenance.toml`, SHA-256 `57340b57a91fb57470e6a9f45a436d8746cfda83ea3b19833d5b0359ce0db07f` |
| Expectations file (before commit-hook formatting) | `tests/demo_corpus_expectations.toml`, SHA-256 `710755ab01628705e78ab67a44c11c396a6b3c75d0dd5b7c7dd76966a5cd08be` |
| Simulator base commit | `8577911415604c7c59406319e56800bcb31859ee`; the audit ran in a dirty worktree with the #87/#88 runtime fixes and the #89/#90/#92 corpus work in progress |
| Reviewed `_runtime.py` SHA-256 (before commit-hook formatting) | `fd14a2d580beaaab3910de0d50cbd6357582b7433ddf5a6569754e59b05b841c` (reference only; the runner does not embed a simulator source hash) |
| Current `demo_corpus.py` SHA-256 | `e40fb09fa006f1cf9ba1738decdf5b1a92dcc8e2aca80582084b25e20bded94c` (reference only) |

The runner records the fixture revision, interpreter, Python version, and the
docassemble base/demo/webapp versions in `summary.json`. It does not record
AssemblyLine or ALToolbox versions, and it does not record a simulator source
hash. The AssemblyLine and ALToolbox values above were read from the installed
distributions at audit time via `importlib.metadata`. The fresh run therefore
has exact recorded fixture/runtime provenance and a commit-plus-worktree source
identity, not a clean commit hash.

The fresh sweep ran at 17:37:28. The final runtime and corpus edits are
timestamped 18:02:08 (`_runtime.py`), 18:04:30 (`execution.py`), and 18:11:10
(`demo_corpus.py`). The current `_runtime.py` and `demo_corpus.py` hashes above
are therefore reference values for the later, pre-commit-formatting worktree, not the exact source of
the saved run. Because the artifact embeds no source hash, treat the run source
as commit `8577911` plus the dirty worktree state as of 17:37, with the fixture
revision, runtime versions, and command recorded here.

### Exact fresh-sweep command

The completed fresh start sweep was executed once, from the simulator
repository, as:

```sh
cd /Users/jack/Projects/docassemble-simulator
scripts/test-demo-corpus \
  --fixtures /Users/jack/Projects/docassemble-yaml/lsp/tests/fixtures \
  --python "$PWD/.venv-da110/bin/python" \
  --start --timeout 30 --jobs 4 \
  --prepare-runtime-data \
  --nltk-cache-dir "$PWD/.simulator/nltk-cache" \
  --output .simulator/demo-audit-issue89-90-start \
  > /tmp/demo-audit-issue89-90-start.log 2>&1
```

The command used the default provenance manifest and default reviewed
expectations file. It did not pass `--compile`; the saved artifact contains only
start-phase rows. A preserved compile-phase artifact from the same recorded
fixture revision and runtime versions is listed below. It is historical
corroboration only: it was produced before the fresh sweep and before the final
runtime edits, and it cannot establish that the current tree compiles the full
966-example corpus.

Because the saved run has 45 `UNEXPECTED` and 2 `DRIFT` rows, the runner's
contract returns 1. That is the expected result of this triage run, not a pass
signal.

Every saved row carries a per-fixture reproduction command of the form:

```sh
scripts/test-demo-corpus --fixtures /Users/jack/Projects/docassemble-yaml/lsp/tests/fixtures \
  --python /Users/jack/Projects/docassemble-simulator/.venv-da110/bin/python \
  --match '^<fixture>$' --start --timeout 30 \
  --provenance-manifest /Users/jack/Projects/docassemble-simulator/tests/demo_corpus_provenance.toml \
  --prepare-runtime-data
```

The runner used isolated subprocesses, the network-denial sandbox, a 30-second
timeout, and four concurrent workers. The log contains 45 `UNEXPECTED` lines and
2 `DRIFT` lines, matching the saved counts. A later rerun attempt wrote only an
empty `/tmp/demo-audit-issue89-90-start-final.log`; it produced no artifact and
is not part of this evidence.

### Artifacts, timestamps, and hashes

| Artifact | Timestamp (UTC-05:00) | SHA-256 |
| --- | --- | --- |
| Fresh start `results.jsonl` (`.simulator/demo-audit-issue89-90-start/results.jsonl`) | 2026-10-31T17:37:28 | `9da2e6d8f08552e856b32f4ce4dceb394b3c47189a6fd18304fe19c72af91893` |
| Fresh start `summary.json` | 2026-10-31T17:37:28 | `23583534d89b28cf99c057b389134a9b7d30a6d1f1665dec014a90f9369e522b` |
| Fresh start `summary.md` | 2026-10-31T17:37:28 | `8c9d2fe019e083e840d43e5085807cda97d8a363163208982c5432b5e752ece1` |
| Fresh start log (`/tmp/demo-audit-issue89-90-start.log`) | 2026-10-31T17:37:28 | `6b42cf1f9e3f12c6a80adb533e8f52767b073dd2b6df0b108b2a715aab7cb82a` |
| Postprocessed v2 `results.jsonl` (`.simulator/demo-audit-issue89-90-start-blockers-v2/results.jsonl`) | 2026-10-31T18:11:14 | `90630274c05e0a6ef2e0d0f547163ef59239f7d2f4f1af65e49c43c69b712e80` |
| Postprocessed v2 `summary.json` | 2026-10-31T18:11:14 | `e705ce0e3180bfeb0274b2d88f0aca886a412a1adac12ca9536dfc8d657a9b82` |
| Postprocessed v2 `summary.md` | 2026-10-31T18:11:14 | `92bd5f7e49299104eb86e25f97deac0313af99325ab6055f3d3ba2ec12296e06` |
| Preliminary start `results.jsonl` (`.simulator/demo-audit-start/results.jsonl`) | 2026-10-31T15:09 | `15334cb17bda0d818232aa7ee99194e4d05d4228884f18aed50ec0b5ac4f87af` |
| Preserved compile `results.jsonl` (`.simulator/demo-audit-compile/results.jsonl`) | 2026-10-31T14:54 | `e029c0f937f583e011323c95571b88e46d9633da21d612e0e201a825451b54e2` |

The preliminary 70-failure baseline comes from the preliminary start artifact,
not from the fresh sweep.

### Executed sweep versus postprocessing

| Step | What happened | Evidence |
| --- | --- | --- |
| Fresh execution | The completed start phase ran 966 cases at 17:37:28. It produced 898 successes, 66 failures, and 2 runtime drifts. This is the only new corpus execution used by this review. | `.simulator/demo-audit-issue89-90-start/results.jsonl`, `/tmp/demo-audit-issue89-90-start.log` |
| Postprocessing | At 18:11:14 the same 966 saved rows were reconstructed and `write_reports`/`derive_blocker` recomputed blocker fields with the tightened #92 rules. No interview subprocess ran. `summary.json` gained the `expected` field and blocker-recomputation provenance. | `.simulator/demo-audit-issue89-90-start-blockers-v2/`; source hash recorded in its `metadata.blocker_recomputation` |
| Targeted investigation | Read-only `why` probes and upstream class probes for `branch-error` and `dual-dict`; no corpus run. | Inline evidence in [Six unresolved-variable cases](#six-unresolved-variable-cases) |

The postprocessed `results.jsonl` is identical to the fresh artifact in every
field except `blocker`: all 966 rows match after ignoring that field, and the
only changed row is `redis.yml` (`service` to none). The original fresh artifact
was not overwritten; its hash is unchanged.

The fresh `summary.json` was written before the `expected` summary field existed,
so it reports `unexpected: 45` but has no `expected` key. The v2 summary adds
`expected: 21`; recomputing the saved rows produces the same 21 expected and 45
unexpected rows with zero mismatch.

## Results

Fresh start-phase counts:

| Classification | Count |
| --- | ---: |
| Success | 898 |
| Failure | 66 |
| Runtime drift | 2 |
| **Total** | **966** |

Successful starts comprised 883 question outcomes and 15 immediate finished
outcomes. The two runtime-drift exclusions are `fields-mc-9.yml` and
`progressive-disclosure.yml`; their fixture content differs from the installed
runtime, and they were not executed as substitutes.

The preserved compile-phase artifact at the same recorded fixture revision and
runtime versions is historical corroboration only: 964 successes, 0 failures,
and 2 runtime drifts, with the two drift rows not compiled. It was produced at
14:54, before the fresh start sweep, and was not rerun after the final runtime
edits. It therefore cannot establish current full-corpus compilation; a
full-corpus compile remains un-rerun. A passing real-runtime gate is not a
substitute for that compile.

Failure kinds:

| Error kind | Count |
| --- | ---: |
| execution | 44 |
| pdf-unavailable | 16 |
| unresolved-variable | 6 |
| runtime-drift (separate classification) | 2 |

Of the 66 failures, 21 are reviewed expectations (18 capability boundaries and
3 deliberate errors) and 45 are unexpected. The 45 unexpected split into 32
automatically attributed boundaries (10 environment, 1 network, 21 service), 6
manually attributed boundaries, and 7 authored/incomplete fixtures. Section
[Complete verdict register](#complete-verdict-register-for-the-70-prior-failures)
lists every prior failure.

### Reconciliation from the preliminary 70 failures

The preliminary audit reported 70 start failures: 61 execution and 9
unresolved-variable. The fresh sweep reports 66: 44 execution, 6
unresolved-variable, and 16 `pdf-unavailable`.

| Change | Execution | Unresolved | PDF | Total |
| --- | ---: | ---: | ---: | ---: |
| Preliminary failures | 61 | 9 | 0 | 70 |
| Fixed by #87 (`run-python-module*.yml`) | -4 | 0 | 0 | -4 |
| #88 typed PDF boundary | -13 | -3 | +16 | 0 |
| **Fresh failures** | **44** | **6** | **16** | **66** |

The 16 typed PDF failures are 11 PDF-only attachments, 3 stripped `.pdf` link
requests, and 2 manual-concatenation requests. The 13 execution failures that
became PDF failures are those same 11 PDF-only and 2 manual cases; the 3
unresolved-variable cases that became PDF failures are the stripped `.pdf` link
cases.

Successes rose from 894 to 898 (+4 fixed module-runner cases). Runtime drift
remained 2. No new failure appeared.

Every one of the 70 prior failures has exactly one verdict:

| Verdict | Count |
| --- | ---: |
| Fixed | 4 |
| Reviewed expectation | 21 |
| Declared boundary (32 automatic + 6 manual) | 38 |
| Authored/incomplete (4 missing state + 1 absent asset + 2 upstream-seeking) | 7 |
| **Total** | **70** |

Equivalently, the 45 fresh unexpected rows are 32 automatic boundaries, 6
manual boundaries, and 7 authored/incomplete fixtures. The 21 fresh expectations
are not part of the unexpected set.

### This is not end-to-end validation

The 883 question outcomes are first-screen assemblies. The 15 finished outcomes
are immediate completions. No screen was answered, no required-field or
validation gate was exercised, no automatic seek/replay was run, no action or
row action was pressed, and no DOCX/PDF artifact journey was exercised. One
start result (`document-links.yml`) published an RTF attachment; that is not an
action or download journey and does not make the sweep end-to-end. A successful
start means only that the interview reached its first question or finished
without a startup error.

The 21 expectations are startup-phase expectations. They assert the typed
capability or deliberate error at start; they do not assert that later screens,
validation, documents, or downloads behave correctly. The 45 unexpected
failures stop at their startup point. None of these results is evidence that the
simulator supports the full interview, and none should be quoted as end-to-end
completion.

## Corrected module-runner root cause

The preliminary audit called the four `run-python-module*.yml` failures a
package-copying/file-permission defect and pointed at the staging code. That
explanation is superseded.

The actual upstream behavior is visible in docassemble 1.10.13's
`docassemble/base/util.py`:

```python
commands = [
    re.sub(r"/lib/python.*", "/bin/python3", docassemble.base.pandoc.__file__),
    "-m",
    module,
]
```

The interpreter is derived from the installed Pandoc module path. In the
installed environment, that path contains `/lib/python3.14/`, so the
substitution yields `.venv-da110/bin/python3`. In the corpus runner's staged
workspace, `docassemble.base.pandoc.__file__` is a path such as
`.../workspace/docassemble/base/pandoc.py`. That path has no `/lib/python`
segment, so the regular expression performs no substitution and the command
becomes:

```text
.../workspace/docassemble/base/pandoc.py -m docassemble.demo.hello
```

The runner then tries to execute a Python source file that has no executable
bit. The observed `[Errno 13] Permission denied:
.../workspace/docassemble/base/pandoc.py` is the symptom of executing the
source file itself, not evidence that package copying removed permissions or
that the workspaces are misconfigured. Staging is intentional and is what makes
the upstream path assumption fail.

Issue #87 replaces the seam in
`src/docassemble_simulator/_runtime.py:924-966` with a wrapper that keeps
upstream's module-name normalization, argument validation, combined output, and
return-code contract, but runs:

```python
commands = [sys.executable, "-m", module]
subprocess.check_output(commands, stderr=subprocess.STDOUT, cwd=_ACTIVE_ROOT.get())
```

`sys.executable` is the active target interpreter and `_ACTIVE_ROOT` is the
active staged package tree, so `python -m` can import the package module. The
wrapper is installed by `install_python_module_runner()` at
`src/docassemble_simulator/_runtime.py:969-998`. Regression coverage is in
`tests/test_runtime_python_module_runner.py` and
`tests/test_issue87_python_module.py`.

All four fixtures now reach a question outcome and are absent from the fresh
failure list:

- `run-python-module.yml` (filename form)
- `run-python-module-2.yml` (dotted module form)
- `run-python-module-3.yml` (relative module form)
- `run-python-module-tests.yml` (filename plus argument list)

This is the corrected root cause; the preliminary audit's package
copying/permissions explanation should not be repeated.

## Reviewed expectations (21)

The expectations file pins 21 narrow start-phase expectations: 18 capability
boundaries and 3 deliberate errors. Each expectation is keyed by interview,
phase, classification, return code, error kind, and a stable message fragment.
The expectation is not a general whitelist for other failures.

| Type | Count | Fixtures |
| --- | ---: | --- |
| PDF-only attachment (ADR-0006) | 11 | `allow-emailing-false-pdf.yml`, `attachment-notice.yml`, `attachment.yml`, `document-links-limited.yml`, `formatting.yml`, `immediate_file.yml`, `manual-attachment-list.yml`, `pdf-a.yml`, `tagged-pdf.yml`, `valid-formats-code.yml`, `valid-formats.yml` |
| Stripped `.pdf` link (ADR-0006) | 3 | `dafile-url-for.yml`, `dafile-url-for-temporary.yml`, `document-variable-name-link.yml` |
| Manual concatenation of stripped PDFs (ADR-0006) | 2 | `document-manual.yml`, `document-manual-code.yml` |
| RTF-to-DOCX converter capability (ADR-0001) | 2 | `document-docx-from-rtf.yml`, `document-docx-from-rtf-standalone.yml` |
| Deliberate error demonstration | 3 | `error-help.yml`, `error-help-language.yml`, `infinite-loop-2.yml` |
| **Total** | **21** | |

### The PDF finding includes the manual-concatenation pair

The preliminary audit listed two "manual-PDF failures" as a separate cause: the
two fixtures failed with `pdf_concatenate: no valid files to concatenate`.
Those fixtures concatenate only generated PDF attachments, which the simulator
strips before conversion. They are not a separate manual-copy defect, and they
are not a new failure class. They are part of the same PDF capability boundary
and now report the typed `pdf-unavailable` message like the other PDF cases.
The preliminary audit's separate manual-PDF bullet is therefore folded into
this PDF finding.

The same correction applies to the three stripped `.pdf` link examples. The
preliminary audit called them a simulator diagnostic gap and insisted they must
not be dismissed as authored errors. That diagnosis was right about the cause
and is now resolved: they are reviewed capability boundaries with the typed
`pdf-unavailable` error, not unresolved-variable findings. The simulator does
not fabricate a PDF and does not provide or bundle an external PDF converter.
The two RTF-to-DOCX examples still attempt the absent `libreoffice` executable
and fail with `ENOENT`; that absent converter is the ADR-0001 capability
boundary, not a successful conversion. The simulator only falls back to DOCX
where ADR-0011 applies. PDF-only and manual-PDF cases keep the ADR-0006 typed
failure.

## Declared boundaries

The remaining 38 prior failures are declared boundaries. They are not reviewed
expectations and are not whitelisted. The runner's evidence-only blocker rules
attribute a boundary only when the saved diagnostic contains the relevant
context; the 32 automatic labels below come from those rules. Six more are
declared manually because the saved message does not carry enough context for
automatic attribution. All 38 stop at startup and none establishes later
behavior.

### Automatically attributed boundaries (32)

Environment (10):

| Fixture | Evidence |
| --- | --- |
| `calendar.yml` | `namespace rehydration failed: ModuleNotFoundError: No module named 'ics'` |
| `chatbot.yml` | Authentication method / API key is not configured |
| `google-drive.yml` | Google Cloud credentials are not configured |
| `google-sheet-2.yml` | `namespace rehydration failed: ModuleNotFoundError: No module named 'gspread'` |
| `google-sheet-3.yml` | `namespace rehydration failed: ModuleNotFoundError: No module named 'gspread'` |
| `google-sheet.yml` | `namespace rehydration failed: ModuleNotFoundError: No module named 'gspread'` |
| `graph.yml` | `namespace rehydration failed: ModuleNotFoundError: No module named 'matplotlib'` |
| `oauth-test.yml` | `The application mygoogle is not configured in the Configuration` |
| `realtimegd.yml` | `namespace rehydration failed: ModuleNotFoundError: No module named 'google_drive_downloader'` |
| `realtimegd2.yml` | Google Cloud credentials are not configured |

Network (1):

| Fixture | Evidence |
| --- | --- |
| `get-time.yml` | `HTTPConnectionPool(host='worldclockapi.com', port=80)` with name-resolution failure |

Service (21):

| Fixture | Evidence |
| --- | --- |
| `classify.yml` | ML service not configured; runtime `DummyObject` lacks `one_unclassified_entry` |
| `companies.yml` | No server database; `namespace rehydration failed: AttributeError: 'NoneType' object has no attribute 'Model'` |
| `indexdemoquery.yml` | No server database; `NoneType` has no attribute `cursor` at `conn.cursor()` |
| `interview-list.yml` | Server session/secret not configured (`'secret'`) |
| `ml-ajax-classify.yml` | ML service not configured; `DummyObject` lacks `one_unclassified_entry` |
| `ml-ajax.yml` | ML service not configured; `DummyObject` lacks `save_for_classification` |
| `ml-classify.yml` | ML service not configured; `DummyObject` lacks `one_unclassified_entry` |
| `ml-export-playground.yml` | ML service not configured; `DummyObject` lacks `export_training_set` |
| `ml-export-yaml.yml` | ML service not configured; `DummyObject` lacks `export_training_set` |
| `ml-export.yml` | ML service not configured; `DummyObject` lacks `export_training_set` |
| `ml-predict-probabilities.yml` | ML service not configured; `DummyObject` lacks `predict` |
| `ml-predict.yml` | ML service not configured; `DummyObject` lacks `predict` |
| `oauth-test-3.yml` | `FakeRedis` lacks `setnx`; server Redis substitute limitation |
| `ocr-classify.yml` | ML service not configured; `DummyObject` lacks `one_unclassified_entry` |
| `persistent-task.yml` | Server task store missing; `task_performed` receives `None` |
| `random-forest.yml` | ML service not configured; `DummyObject` lacks `is_empty` |
| `save-and-predict.yml` | ML service not configured; `DummyObject` lacks `new_entry` |
| `session-interview-redirect.yml` | Server session secret not configured (`'secret'`) |
| `session-interview.yml` | Server session secret not configured (`'secret'`) |
| `testdatabase.yml` | No server database; `namespace rehydration failed: AttributeError: 'NoneType' object has no attribute 'Model'` |
| `testdatabase2.yml` | No server database; `namespace rehydration failed: AttributeError: 'NoneType' object has no attribute 'Model'` |

The service label is the runner's evidence bucket, not a claim that every
failure is an external outage. In particular, `oauth-test-3.yml` is the
simulator's `FakeRedis` substitute missing `setnx`, and the `companies` /
`testdatabase` cases are the ADR-0009 no-server-database boundary. They are
still boundaries for this audit because the capability is not provided locally;
they are not reviewed expectations.

### Manually attributed boundaries (6)

These failures do not contain the context required by the automatic blocker
rules, so their saved `blocker` is `null`. They are declared manually here from
the fixture source and the observed startup failure. They are still not
whitelisted and are not reviewed expectations.

| Fixture | Why it is a boundary |
| --- | --- |
| `chat-partners-available.yml` | Calls the server live-help API and dereferences its result; no live-chat service is configured. The message is only `'NoneType' object has no attribute 'help'`, so the automatic service rule does not fire. |
| `daweb.yml` | Calls the external SpaceX API through `DAWeb`; the network-denied sandbox returns `None`, then the template subscripts it. The saved message has no HTTP/DNS transport text, so the network rule does not fire. |
| `list-interview.yml` | Calls server `interview_menu()`; no server dispatch is available, so it returns `None` and the template calls `len(None)`. The saved message has no service context. |
| `redis.yml` | Uses `DARedis` against the simulator `FakeRedis`; that substitute's `get` always returns `None` and `set` does not store. The saved failure is only `NoneType` has no attribute `decode`, so the automatic service rule correctly leaves it unattributed. This is a server-store substitute limitation, not an external Redis outage. |
| `save-url-to-file.yml` | Passes an `https://` URL to `path_and_mimetype` and then `copy_into`; the simulator resolves the URL as a metadata path and the local copy opens the URL string as a filesystem path (`[Errno 2] ... 'https://docassemble.org/img/docassemble-logo.jpg'`). This is a simulator URL-file retrieval gap, not an environment outage and not an authored error. It has no transport diagnostic. |
| `wc_side_of_bed.yml` | Includes `wc_common.yml`, which uses server records/login/session APIs. `read_records` returns `None`, producing `'NoneType' object is not iterable`; the saved message has no service context. |

The six manual boundaries are deliberately not promoted to reviewed
expectations. They remain visible as unexpected results so future work does not
mistake an unexercised server or URL capability for a fixed one.

### Boundaries are not end-to-end validation

All 38 boundary cases stopped at startup. A configured server, database, ML
service, live-help service, Redis store, URL fetcher, or real docassemble
deployment could expose different behavior later. Nothing in this audit claims
that the simulator's later behavior is correct for those cases. The boundary is
a statement about what this simulator/environment did not provide, not a
statement that the interview is unsupported in a real deployment.

## Six unresolved-variable cases

The six unresolved-variable failures are listed with their fixture hashes,
sought variable, evidence, and verdict. The hashes come from the saved sweep
rows and match the provenance manifest.

| Fixture | Fixture SHA-256 | Sought variable | Evidence | Verdict |
| --- | --- | --- | --- | --- |
| `branch-error.yml` | `7b74d336dfcba0d3ecd038ea9a051fabc47140f65eeb555ab84aee2c2a759642` | `long_branch.length` | The only field is `tree.branch.length` (fixture line 23). The object alias keeps `long_branch` as the instance name, so the runtime seeks `long_branch.length`; a read-only `why` probe returns no candidates. The upstream demo package ships the same shape as `testdaobjectfail.yml`; `branch-no-error.yml` shows the valid form (`tree.branch: DAObject`). | Authored/incomplete, upstream-seeking consequence; not a simulator regression |
| `dual-dict.yml` | `c5c23eb91ac6d0d96815b14c567fe4e4ce2e2f72d5c764303aede22be0c80522` | `veggies['potato'].there_are_any` | No question or code block is indexed as a provider for that variable; a read-only `why` probe returns no candidates. The later non-mandatory code block (lines 34-43) sets `there_are_any` while iterating `veggies`, but the compiled candidate index does not offer it as a provider. The fixture also references undefined `foo` at line 29. | Authored/incomplete, upstream-seeking consequence; not a simulator regression |
| `gather.yml` | `29e49206ca215cb41ed52b531b6f7e5ebf993f6f9c5348f5727c13d658f62c41` | `witness.there_are_any` | `witness.gather()` runs at line 16, but the `there_are_any` question is commented out at lines 29-32. The fixture deliberately supplies the `there_is_another` question but not the initial "any witnesses?" question. | Authored/incomplete |
| `gather-fruit-incomplete.yml` | `e67fb5ff62001d08d11e0020a942107e3679403027f1a464b8167b4bc92688f9` | `fruit.there_are_any` | The mandatory summary question at lines 11-16 calls `fruit.number_as_word()` at line 13, which triggers list gathering. The fixture has no gathering questions (no `there_are_any` or `there_is_another`) and no code that marks the empty list gathered. The name states it is incomplete. | Authored/incomplete |
| `declarative.yml` | `c2bd88c6cf2cf2f0f68f40055b10c15e538f02cd5a3510c1bd03bbf12425e279` | `case_type` | No question or code block defines `case_type`, `injury_date`, `amount_in_dispute`, or `defendant`; the mandatory question at lines 12-20 depends on `has_a_case`, whose code chain reaches `case_type`. This is a declarative example that expects pre-seeded/API state. | Authored/incomplete |
| `questionless.yml` | `0f92bca2bd41bb1d29ae002442273fe098d0fd85ae7e884203b4963926cf346c` | `favorite_number` | The fixture has no question blocks; its two code blocks expect API request variables `favorite_number` and `user_agrees_to_waive_penalties`. Starting without those inputs seeks `favorite_number`. | Authored/incomplete |

### `branch-error`: upstream-seeking verdict

`branch-error.yml` declares `tree` and `long_branch` as `DAObject`s (lines
7-9), assigns `tree.branch = long_branch` (line 17), and defines a field for
`tree.branch.length` (line 23). Upstream's `DAObject.__setattr__` only renames
an assigned object when it does not already have a non-random instance name
(`docassemble/base/util.py:1786-1790`). The declared `long_branch` already has
one, so the assignment leaves `long_branch.instanceName == 'long_branch'` and
the template's `tree.branch.length` raises for `long_branch.length`, exactly as
observed.

A targeted read-only probe confirms the class behavior:

```text
long_branch.instanceName = long_branch
has_nonrandom_instance_name = True
tree.branch is long_branch = True
tree.branch.length raises: DAAttributeError name 'long_branch.length' is not defined
```

A second probe against the compiled interview shows the field question is
indexed under its written name, not the alias:

```sh
.venv-da110/bin/python -m docassemble_simulator \
  --root .simulator/issue91-why-probe-stage \
  --interview docassemble.base:data/questions/examples/branch-error.yml \
  --json --offline why tree.branch.length
```

```json
{"candidates": [{"block_type": "fields", "source_file": "docassemble.base:data/questions/examples/branch-error.yml"}], "conflict": false}
```

*(Abridged to the decision fields; the full result also carries the interview
identity and candidate ordering.)*

The probe for the actually sought variable returns no candidates:

```sh
.venv-da110/bin/python -m docassemble_simulator \
  --root .simulator/issue91-why-probe-stage \
  --interview docassemble.base:data/questions/examples/branch-error.yml \
  --json --offline why long_branch.length
```

```json
{"variable": "long_branch.length", "candidates": [], "conflict": false}
```

*(Abridged to the decision fields.)*

The upstream demo package contains an equivalent fixture named
`testdaobjectfail.yml`, and the valid counterpart `branch-no-error.yml` declares
`tree.branch` directly. The time-boxed verdict is therefore: this is an authored
upstream-seeking failure, not a simulator defect. The fixture is named
`branch-error`, so the failure is plausibly intentional; either way it is not
evidence of missing simulator capability and no expectation is added here.

### `dual-dict`: upstream-seeking verdict

`dual-dict.yml` creates `veggies` as a `DADict` whose values are `DAList`
objects (lines 34-43), then iterates the dictionary in the mandatory summary
question (lines 58-70). Accessing `veggies['potato'].there_are_any` triggers
the list-gathering path, but no question or code block is indexed as a provider
for that variable. The later non-mandatory `code` block that sets
`veggies[item].there_are_any = True` does so while iterating `veggies`; the
compiled candidate index does not offer it as a provider for the missing list
attribute, and the `why` probe confirms there is no candidate. The fixture also
has an unrelated undefined `foo` at line 29, which is another sign that it is
not a self-contained success case.

The read-only candidate probe returns no candidates:

```sh
.venv-da110/bin/python -m docassemble_simulator \
  --root .simulator/issue91-why-probe-stage \
  --interview docassemble.base:data/questions/examples/dual-dict.yml \
  --json --offline why "veggies['potato'].there_are_any"
```

```json
{"variable": "veggies['potato'].there_are_any", "candidates": [], "conflict": false}
```

*(Abridged to the decision fields.)*

The upstream class probe confirms the undefined attribute is the expected
`DAAttributeError`:

```text
veggies potato: DAList veggies['potato']
potato there_are_any: DAAttributeError name 'veggies['potato'].there_are_any' is not defined
```

Docassemble's seeking loop converts that undefined attribute into
`DAErrorMissingVariable` when no question or code block can resolve it
(`docassemble/base/parse.py:9609`). The time-boxed verdict is therefore: the
fixture is incomplete/authored, and upstream's seeking rules produce the same
missing-variable failure. It is not a simulator regression. No expectation is
added here.

### The four authored/incomplete fixtures

The remaining four are straightforward authored/incomplete cases:

- `gather.yml` requires `witness.there_are_any`, but the question that would
  define it is commented out. The fixture still defines `there_is_another`, so
  it is a partial gathering example, not a complete interview.
- `gather-fruit-incomplete.yml` names itself incomplete and has no gathering
  questions (no `there_are_any` or `there_is_another`; only the mandatory
  summary question at lines 11-16). That summary calls `fruit.number_as_word()`,
  which gathers the empty list and seeks `fruit.there_are_any`.
- `declarative.yml` is a declarative-style fragment: it defines code branches
  for jurisdiction and venue, but no question or seed supplies `case_type`,
  `injury_date`, `amount_in_dispute`, or `defendant`. Starting it without API
  or seed state cannot reach `case_type`.
- `questionless.yml` is a questionless API example: its code expects request
  variables `favorite_number` and `user_agrees_to_waive_penalties`, and it has
  no questions to ask for them.

These are authored/incomplete fixtures. They are not simulator faults and are
not reviewed expectations. A future audit may add seeded-state journeys if
those fixtures are intended to be runnable; this audit does not fabricate that
state.

## Absent asset fixture

`objects-from-file-template.yml` needs an asset that is absent from the corpus.

The fixture calls `objects_from_file("claims.yml")` at line 18 and then expects
`info['discovery']['correspondence']` at line 29. The 966-example corpus provides
only `data/questions/examples/*.yml`; it does not provide a `claims.yml` source
asset. The fixture repository also contains `demo_package`, `reference_package`,
and `regressions` YAML, but those are outside the 966-example corpus. The
installed `docassemble.demo` 1.10.13 package does contain
`data/sources/claims.yml`, but that file's top-level key is `defenses`, not
`discovery`; the observed start failure is exactly `KeyError: 'discovery'` while
rendering the template. The included `discovery.yml` fixture exists, but it
does not provide the missing `claims.yml` payload.

Verdict: authored/incomplete fixture with an absent corpus asset. This is not a
simulator startup defect and no expectation is added.

## Complete verdict register for the 70 prior failures

The four tables in this section cover every one of the 70 failures in the
preliminary start artifact. Each fixture appears exactly once.

### Fixed (4)

| Fixture | Prior failure | Verdict and basis |
| --- | --- | --- |
| `run-python-module.yml` | execution | Fixed by #87; old EACCES executing staged `pandoc.py`; now success |
| `run-python-module-2.yml` | execution | Fixed by #87; old EACCES executing staged `pandoc.py`; now success |
| `run-python-module-3.yml` | execution | Fixed by #87; old EACCES executing staged `pandoc.py`; now success |
| `run-python-module-tests.yml` | execution | Fixed by #87; old EACCES executing staged `pandoc.py`; now success |

### Reviewed expectations (21)

| Fixture | Prior failure | Verdict and basis |
| --- | --- | --- |
| `allow-emailing-false-pdf.yml` | execution | Reviewed expectation: PDF-only attachment; typed `pdf-unavailable` (ADR-0006) |
| `attachment-notice.yml` | execution | Reviewed expectation: PDF-only attachment; typed `pdf-unavailable` (ADR-0006) |
| `attachment.yml` | execution | Reviewed expectation: PDF-only attachment; typed `pdf-unavailable` (ADR-0006) |
| `dafile-url-for-temporary.yml` | unresolved-variable | Reviewed expectation: stripped `.pdf` link; typed `pdf-unavailable` (ADR-0006) |
| `dafile-url-for.yml` | unresolved-variable | Reviewed expectation: stripped `.pdf` link; typed `pdf-unavailable` (ADR-0006) |
| `document-docx-from-rtf-standalone.yml` | execution | Reviewed expectation: RTF-to-DOCX converter capability; LibreOffice absent (ADR-0001) |
| `document-docx-from-rtf.yml` | execution | Reviewed expectation: RTF-to-DOCX converter capability; LibreOffice absent (ADR-0001) |
| `document-links-limited.yml` | execution | Reviewed expectation: PDF-only attachment; typed `pdf-unavailable` (ADR-0006) |
| `document-manual-code.yml` | execution | Reviewed expectation: manual concatenation of stripped PDFs; typed `pdf-unavailable` (ADR-0006) |
| `document-manual.yml` | execution | Reviewed expectation: manual concatenation of stripped PDFs; typed `pdf-unavailable` (ADR-0006) |
| `document-variable-name-link.yml` | unresolved-variable | Reviewed expectation: stripped `.pdf` link; typed `pdf-unavailable` (ADR-0006) |
| `error-help-language.yml` | execution | Reviewed expectation: deliberate division-by-zero demonstration |
| `error-help.yml` | execution | Reviewed expectation: deliberate division-by-zero demonstration |
| `formatting.yml` | execution | Reviewed expectation: PDF-only attachment; typed `pdf-unavailable` (ADR-0006) |
| `immediate_file.yml` | execution | Reviewed expectation: PDF-only attachment; typed `pdf-unavailable` (ADR-0006) |
| `infinite-loop-2.yml` | execution | Reviewed expectation: deliberate cyclic-dependency demonstration |
| `manual-attachment-list.yml` | execution | Reviewed expectation: PDF-only attachment; typed `pdf-unavailable` (ADR-0006) |
| `pdf-a.yml` | execution | Reviewed expectation: PDF-only attachment; typed `pdf-unavailable` (ADR-0006) |
| `tagged-pdf.yml` | execution | Reviewed expectation: PDF-only attachment; typed `pdf-unavailable` (ADR-0006) |
| `valid-formats-code.yml` | execution | Reviewed expectation: PDF-only attachment; typed `pdf-unavailable` (ADR-0006) |
| `valid-formats.yml` | execution | Reviewed expectation: PDF-only attachment; typed `pdf-unavailable` (ADR-0006) |

### Declared boundaries (38)

| Fixture | Prior failure | Verdict and basis |
| --- | --- | --- |
| `calendar.yml` | execution | Declared boundary: environment; missing optional `ics` |
| `chat-partners-available.yml` | execution | Declared boundary (manual): server live-help API returns `None`; no qualifying diagnostic text |
| `chatbot.yml` | execution | Declared boundary: environment; authentication/API key not configured |
| `classify.yml` | execution | Declared boundary: service; no ML service, `DummyObject` lacks method |
| `companies.yml` | execution | Declared boundary: service; no server database, `NoneType.Model` |
| `daweb.yml` | execution | Declared boundary (manual): external API/network denied; no transport text |
| `get-time.yml` | execution | Declared boundary: network; DNS/HTTP failure to `worldclockapi.com` |
| `google-drive.yml` | execution | Declared boundary: environment; Google Cloud credentials absent |
| `google-sheet-2.yml` | execution | Declared boundary: environment; missing `gspread` |
| `google-sheet-3.yml` | execution | Declared boundary: environment; missing `gspread` |
| `google-sheet.yml` | execution | Declared boundary: environment; missing `gspread` |
| `graph.yml` | execution | Declared boundary: environment; missing `matplotlib` |
| `indexdemoquery.yml` | execution | Declared boundary: service; no server database, `NoneType.cursor` |
| `interview-list.yml` | execution | Declared boundary: service; server session/secret not configured |
| `list-interview.yml` | execution | Declared boundary (manual): server `interview_menu()` returns `None`; no qualifying diagnostic text |
| `ml-ajax-classify.yml` | execution | Declared boundary: service; no ML service, `DummyObject` lacks method |
| `ml-ajax.yml` | execution | Declared boundary: service; no ML service, `DummyObject` lacks method |
| `ml-classify.yml` | execution | Declared boundary: service; no ML service, `DummyObject` lacks method |
| `ml-export-playground.yml` | execution | Declared boundary: service; no ML service, `DummyObject` lacks method |
| `ml-export-yaml.yml` | execution | Declared boundary: service; no ML service, `DummyObject` lacks method |
| `ml-export.yml` | execution | Declared boundary: service; no ML service, `DummyObject` lacks method |
| `ml-predict-probabilities.yml` | execution | Declared boundary: service; no ML service, `DummyObject` lacks method |
| `ml-predict.yml` | execution | Declared boundary: service; no ML service, `DummyObject` lacks method |
| `oauth-test-3.yml` | execution | Declared boundary: service; `FakeRedis` lacks `setnx` |
| `oauth-test.yml` | execution | Declared boundary: environment; `mygoogle` application not configured |
| `ocr-classify.yml` | execution | Declared boundary: service; no ML service, `DummyObject` lacks method |
| `persistent-task.yml` | execution | Declared boundary: service; server task store missing |
| `random-forest.yml` | execution | Declared boundary: service; no ML service, `DummyObject` lacks method |
| `realtimegd.yml` | execution | Declared boundary: environment; missing `google_drive_downloader` |
| `realtimegd2.yml` | execution | Declared boundary: environment; Google Cloud credentials absent |
| `redis.yml` | execution | Declared boundary (manual): `FakeRedis` store limitation; no service context |
| `save-and-predict.yml` | execution | Declared boundary: service; no ML service, `DummyObject` lacks method |
| `save-url-to-file.yml` | execution | Declared boundary (manual): simulator does not fetch remote URLs; no transport text |
| `session-interview-redirect.yml` | execution | Declared boundary: service; server session secret not configured |
| `session-interview.yml` | execution | Declared boundary: service; server session secret not configured |
| `testdatabase.yml` | execution | Declared boundary: service; no server database, `NoneType.Model` |
| `testdatabase2.yml` | execution | Declared boundary: service; no server database, `NoneType.Model` |
| `wc_side_of_bed.yml` | execution | Declared boundary (manual): server records/login path returns `None`; no qualifying text |

The automatic/manual counts are 32 automatic (10 environment, 1 network, 21
service) and 6 manual. The fresh v2 summary reports exactly those automatic
counts; the manual six are intentionally `blocker: null` and are not counted in
the summary's blockers. They are still 45 unexpected minus 32 blocked minus 7
authored = 6.

### Authored/incomplete (7)

| Fixture | Prior failure | Verdict and basis |
| --- | --- | --- |
| `branch-error.yml` | unresolved-variable | Authored/incomplete, upstream-seeking: alias seeks `long_branch.length`; no candidate; upstream-equivalent failing demo |
| `declarative.yml` | unresolved-variable | Authored/incomplete: no questions or code define `case_type`, `injury_date`, `amount_in_dispute` |
| `dual-dict.yml` | unresolved-variable | Authored/incomplete, upstream-seeking: no candidate for `veggies['potato'].there_are_any`; setting block is not a provider |
| `gather-fruit-incomplete.yml` | unresolved-variable | Authored/incomplete: no gathering questions, only the mandatory summary question; `fruit.number_as_word()` seeks `fruit.there_are_any` |
| `gather.yml` | unresolved-variable | Authored/incomplete: `there_are_any` question commented out; `witness.gather()` seeks it |
| `objects-from-file-template.yml` | execution | Authored/incomplete: absent corpus `claims.yml` with expected `discovery` key; observed `KeyError: 'discovery'` |
| `questionless.yml` | unresolved-variable | Authored/incomplete: no questions; API inputs `favorite_number` and waiver flag absent |

## Outstanding coverage gap and next audit

The startup baseline is now reconciled. The outstanding gap is not another
startup category: it is that **no interactive answer journeys were exercised**.

The corpus produced 883 question outcomes, but the sweep never submitted an
answer, never crossed a validation or requiredness gate, never exercised
automatic seek/replay, and never pressed an action or row action. The 15
finished outcomes are immediate completions, not answer-driven completions.
The 21 expectations are startup-phase expectations. The 38 boundaries stop
before any later behavior can be observed. These results do not establish
end-to-end support for any example.

The next audit should be an interactive-answer journey audit over the 883
question outcomes, not more startup triage. It should sample the screen shapes
the corpus actually produced (fields, yes/no, choices, object groups, list and
dictionary gathering, code and event screens, document and attachment
requests), submit representative answers, and compare the resulting screen
outcomes and traces. It should also cover the 15 immediate-finished examples as
journeys where they can be reached without external services. Startup-only
sweeps should not be repeated as a substitute for this coverage.

## Reproduction appendix

### Saved evidence

The fresh sweep and postprocessed artifact are under `.simulator/` and are
git-ignored. They are corroborating evidence, not the durable report; the
counts, hashes, commands, and per-fixture verdicts above are self-contained.
The source sweep log is `/tmp/demo-audit-issue89-90-start.log`.

### Targeted `why` probes

The `why` probes used a temporary staged corpus root containing only
`branch-error.yml` and `dual-dict.yml`. The root was produced at
`.simulator/issue91-why-probe-stage` with
`docassemble_simulator.demo_corpus.stage_corpus(..., cases=(branch_case, dual_case))`
and removed after the probes. The simulator CLI then ran with
`--json --offline why <variable>`. The probes are read-only and created no
session state. The key results are reproduced in
[Six unresolved-variable cases](#six-unresolved-variable-cases):

- `why long_branch.length` → `candidates: []`
- `why tree.branch.length` → one `fields` candidate
- `why "veggies['potato'].there_are_any"` → `candidates: []`

The upstream class probes initialized docassemble's `thread_context` globals and
used docassemble 1.10.13's `DAObject`, `DAList`, and `DADict` directly; their
outputs are also reproduced above.

### Upstream source references

- OS interpreter derivation: `.venv-da110/lib/python3.14/site-packages/docassemble/base/util.py:9974`
- `DAObject` alias behavior: `.venv-da110/lib/python3.14/site-packages/docassemble/base/util.py:1786-1790`
- undefined attribute: `.venv-da110/lib/python3.14/site-packages/docassemble/base/util.py:1211-1220`
- missing-variable conversion: `.venv-da110/lib/python3.14/site-packages/docassemble/base/parse.py:9609`
- upstream equivalent failing branch demo:
  `.venv-da110/lib/python3.14/site-packages/docassemble/demo/data/questions/testdaobjectfail.yml`
- upstream correct branch form:
  `.venv-da110/lib/python3.14/site-packages/docassemble/base/data/questions/examples/branch-no-error.yml`
- simulator runner replacement: `src/docassemble_simulator/_runtime.py:924-998`
- simulator PDF boundary: `src/docassemble_simulator/_runtime.py:574-768` and
  `src/docassemble_simulator/execution.py:2511-2555`
- blocker evidence rules: `src/docassemble_simulator/demo_corpus.py:275-407`
- reviewed expectations: `tests/demo_corpus_expectations.toml`

### Related repository gates (not corpus evidence)

The final repository gates reported for the same working tree were:

- `mise run test:fast` — 834 passed.
- `mise run lint` — passed.
- `mise run test:runtime` — CPython 3.10 lane 834 passed; runtime selection 446
  passed, 6 skipped (219 modern, 213 legacy, plus lane-neutral tests).

These gates exercise the package and runtime-compatibility suites. They are not
a rerun of the demo corpus, are not evidence for the 966-example start results,
and do not establish interactive journeys. They also do not replace the
un-rerun full-corpus compile.

### Documentation checks

This file was checked directly for trailing whitespace and local Markdown
anchors; the worktree diff also passes `git diff --check`. The preliminary
`docs/demo-corpus-audit.md` was not modified.
