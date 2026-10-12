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
  recompilation surfaces the typed error. A caller that compiles *outside* an
  operation — the worker's read-only `check`/`questions`/`index` requests, and
  a `trace replay`'s short-lived execution — runs the same check through
  `InterviewCatalog.ensure_current(identity)` first, because docassemble's
  compiled-interview cache is process-global and those callers read it through
  their own catalog instance. A watched-but-changed or never-watched identity is
  invalidated and recompiled there, so the check costs one reload per edit and
  the next request is still warm. A never-watched identity whose compile
  docassemble already holds is evicted from that cached interview's declared
  workspace helpers, bytecode included, before the entry is dropped, because
  this catalog has no signatures of its own to compare. A refresh that cannot
  compile returns its `CompileFailure` instead of swallowing it; the caller
  hands it to the catalog or execution that would have compiled again, which
  re-raises it, so a failing request costs one compile and still reports the
  typed error its own path produces (a `check` row, a catalog failure, or a
  replay divergence).
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
- Detection covers two sets, both under the resolved workspace root. The
  exact set is question provenance — each compiled question's
  `from_source.filepath` — so an abbreviated colon identity
  (`docassemble.other:screens.yml` resolving under
  `docassemble/other/data/questions/`) and a nested relative include resolved
  against its own owning source's directory are watched at the exact file
  docassemble read. The safety net is every `*.yml`/`*.yaml` under the
  workspace's `docassemble/*/data/questions` trees, because docassemble
  excludes include-only blocks and features-only documents from
  `questions_list`, so a routing YAML that changes its `include:` target has no
  compiled question to watch. Walking the questions trees over-includes: an
  unrelated YAML edit in the same workspace merely causes a reload, which is
  preferred to under-including a source the interview could read. A helper that
  resolves to a file under the workspace source tree is stamped directly; a
  helper that resolves to a package directory is stamped by walking its `*.py`
  files, so an edited or newly added submodule counts. A declared module that
  does not resolve to a workspace file (stdlib, an installed docassemble
  package, a mis-declared name) is skipped and logged at debug. Module paths
  are resolved by workspace-tree lookup, not `importlib.util.find_spec`:
  find_spec imports a dotted module's parent package, which would execute
  authored package code during compilation and could import a heavy installed
  dependency.
- Watching, eviction, and bytecode invalidation all resolve candidates with
  `Path.resolve()` and require containment under the resolved workspace root.
  A workspace name that resolves through a symlink to a file outside the tree
  is therefore neither watched, evicted, nor unlinked, and a YAML or include
  whose real path is outside the root is not watched. Bytecode invalidation
  resolves the derived cache path too, so a contained helper whose
  `__pycache__` directory is a symlink outside the tree does not have its
  external `.pyc` deleted. A file that was recorded at compile time and then
  deleted or made unreadable still counts as changed, so recompilation surfaces
  the typed error.
- Configuration is resolved once at worker startup and is not reloaded; a
  configuration change still requires a restart. Templates are read at render
  time (`prepare_docx_template` loads the file per render, with no cache), so a
  template edit is visible to the next render without a reload.
- One-shot commands and any `cache_interview=False` caller keep compiling once
  per operation and are unchanged.

## Consequences

- Unchanged interviews pay one `stat` per operation per watched YAML under the
  workspace questions trees and per helper file, plus a `*.py` walk per watched
  helper package directory, not a ~3s compile, so repeated operations stay in
  the warm range. The questions-tree scan is captured once at compile, not per
  operation.
- The reload signal is a file timestamp, so tests set it explicitly
  (`os.utime`) rather than depending on clock resolution.
- A helper edit under a declared workspace module, or an edit to a workspace
  YAML under the questions trees, now reloads the worker with no restart. The
  remaining boundaries are: a source or module whose real path is outside the
  resolved workspace root is not watched (an installed AssemblyLine include, an
  absolute include outside the tree, a workspace symlink that escapes the
  tree); a workspace module the interview does not declare in
  `modules:`/`imports:` — for example a sibling imported by a declared helper —
  is neither watched nor evicted, because capturing workspace-owned
  `sys.modules` entries at compile time does not see it (docassemble imports
  `modules:`/`imports:` only during assembly, after compilation). These are
  recorded boundaries, not regressions.
