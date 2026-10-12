---
status: accepted
---

# A persistent worker lazily reloads its interview source

## Context

A worker compiles the interview once and keeps it for the process's life so a
journey amortizes startup. During interview development that caching served a
stale program after every edit until the worker restarted. The obvious signal
was docassemble's own `interview_cache.cache_valid`, but it is a redis index:
the simulator's runtime stubs redis with a constant and nothing in the
simulator calls `interview_source.update_index()` (that is the web editor's
job, at `docassemble/webapp/develop/views.py`). Verified against both target
families, `cache_valid` stays `True` and `get_interview` keeps returning the
stale compile after an edit.

## Decision

- `InterviewCatalog._compile` (the worker's only compile seam, with
  `cache_compiled=True`) revalidates lazily at operation boundaries. On a cache
  hit it compares the interview source file's signature — docassemble's
  `InterviewSourceFile.get_modtime()` plus the file size, with an `os.stat`
  fallback on the resolved source path — against the signature captured when
  it compiled. One missing or unreadable source is treated as changed so
  recompilation surfaces the typed error.
- On a change the worker calls `interview_cache.clear_cache(selected)`, evicts
  the recompiled interview's declared `modules:`/`imports:` modules from
  `sys.modules`, calls `importlib.invalidate_caches()`, drops its own compiled
  entry, and recompiles through `get_interview`. Without the eviction,
  docassemble would keep serving the helper code imported on first assembly.
- A reload changes compilation only. The Saved session is never cleared by a
  reload; `reset` remains the explicit clear. A reload that fails to compile
  reports the typed `compile` error and leaves the session intact.
- Detection covers the interview's own source file. A helper-module edit is
  picked up when the interview source also changes in the same reload; a
  module-only edit with no interview-source change is not detected and still
  needs a reload trigger (an interview-source edit or a restart). Watcher-free
  module-only reload is a follow-up.
- Configuration is resolved once at worker startup and is not reloaded; a
  configuration change still requires a restart. Templates are read at render
  time (`prepare_docx_template` loads the file per render, with no cache), so a
  template edit is visible to the next render without a reload.
- One-shot commands and any `cache_interview=False` caller keep compiling once
  per operation and are unchanged.

## Consequences

- Unchanged interviews pay one `stat` per operation, not a ~3s compile, so
  repeated operations stay in the warm range.
- The reload signal is a file timestamp, so tests set it explicitly
  (`os.utime`) rather than depending on clock resolution.
- A module edit that is not accompanied by an interview-source edit remains a
  known boundary rather than a silent success.
