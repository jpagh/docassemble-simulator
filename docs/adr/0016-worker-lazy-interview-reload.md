---
status: accepted
---

# A persistent worker lazily reloads its interview source

## Context

A worker compiles the interview once and keeps it for the process's life so a
journey amortizes startup. During interview development that caching served a
stale program after every edit until the worker restarted (issue #47). The
obvious signal was docassemble's own `interview_cache.cache_valid`, but it is a
redis index:
the simulator's runtime stubs redis with a constant and nothing in the
simulator calls `interview_source.update_index()` (that is the web editor's
job, at `docassemble/webapp/develop/views.py`). Verified against both target
families, `cache_valid` stays `True` and `get_interview` keeps returning the
stale compile after an edit.

## Decision

- `InterviewCatalog._compile` (the worker's only compile seam, with
  `cache_compiled=True`) revalidates lazily at operation boundaries. On a cache
  hit it compares the signature of every watched source file — docassemble's
  `InterviewSourceFile.get_modtime()` for the selected file, and `os.stat`
  mtime plus size for it and the rest — against the signatures captured when it
  compiled. One missing or unreadable source is treated as changed so
  recompilation surfaces the typed error.
- On a change the worker calls `interview_cache.clear_cache(selected)`, evicts
  the recompiled interview's declared `modules:`/`imports:` helpers **that
  resolve under the workspace root** from `sys.modules` (an installed module
  keeps its identity), unlinks the cached bytecode of the changed helper files
  so a same-size edit within the same integer second cannot re-import stale
  `.pyc`, calls `importlib.invalidate_caches()`, drops its own compiled entry,
  and recompiles through `get_interview`. Without the eviction, docassemble
  would keep serving the helper code imported on first assembly; without the
  bytecode removal, CPython's integer-second timestamp validation would accept
  the old cache and re-import stale code.
- A reload changes compilation only. The Saved session is never cleared by a
  reload; `reset` remains the explicit clear. A reload that fails to compile
  reports the typed `compile` error and leaves the session intact.
- Detection covers the selected interview source, the source files of its
  declared `modules:`/`imports:` helpers, and every YAML the compiled interview
  includes. An include is resolved through the same identity→path lookup as the
  selected source for a `package:relative` identity, or against the source's
  directory for a relative sibling. A helper that resolves to a file under the
  workspace source tree is stamped directly; a helper that resolves to a
  package directory is stamped by walking its `*.py` files, so an edited or
  newly added submodule counts. A declared module that does not resolve to a
  workspace file (stdlib, an installed docassemble package, a mis-declared
  name) is skipped and logged at debug; so is an include outside the workspace
  source tree (for example an installed AssemblyLine file). Those are not the
  interview's own source. Module paths are resolved by workspace-tree lookup,
  not `importlib.util.find_spec`: find_spec imports a dotted module's parent
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
- A helper edit under a declared workspace module, or an edit to a workspace
  include, now reloads the worker with no restart. The remaining boundaries are
  modules that are not the interview's own declared workspace helpers. A module
  that lives outside the workspace source tree (a third-party or installed
  package) is not watched. A workspace module the interview does not declare
  in `modules:`/`imports:` — for example a sibling imported by a declared
  helper — is neither watched nor evicted: capturing workspace-owned
  `sys.modules` entries at compile time does not see it, because docassemble
  imports `modules:`/`imports:` only during assembly, after compilation. This
  is a recorded boundary, not a regression.
