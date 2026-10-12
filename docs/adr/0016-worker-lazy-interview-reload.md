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
- Detection covers the interview's own source file and the source files of
  its declared `modules:`/`imports:` helpers. A helper that resolves to a file
  under the workspace source tree is stamped directly; a helper that resolves
  to a package directory is stamped by walking its `*.py` files, so an edited
  or newly added submodule counts. A declared module that does not resolve to a
  workspace file (stdlib, an installed docassemble package, a mis-declared
  name) is skipped and logged at debug; those are not the interview's own
  source. Module paths are resolved by workspace-tree lookup, not
  `importlib.util.find_spec`: find_spec imports a dotted module's parent
  package, which would execute authored package code during compilation and
  could import a heavy installed dependency.
- Configuration is resolved once at worker startup and is not reloaded; a
  configuration change still requires a restart. Templates are read at render
  time (`prepare_docx_template` loads the file per render, with no cache), so a
  template edit is visible to the next render without a reload.
- One-shot commands and any `cache_interview=False` caller keep compiling once
  per operation and are unchanged.

## Consequences

- Unchanged interviews pay one `stat` per operation per watched interview file
  and helper file, plus a `*.py` walk per watched helper package directory, not
  a ~3s compile, so repeated operations stay in the warm range.
- The reload signal is a file timestamp, so tests set it explicitly
  (`os.utime`) rather than depending on clock resolution.
- A helper edit under a declared workspace module now reloads the worker with
  no restart. The remaining boundary is a module that lives outside the
  workspace source tree (a third-party or installed package), which is not
  watched.
