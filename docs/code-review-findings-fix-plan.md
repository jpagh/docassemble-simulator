# Code-review findings fix plan

Review scope: `main...HEAD` (4 commits on `support-da19x`), non-empty diff.
Spec source: GitHub issue #1 (support end-to-end simulator execution on docassemble 1.9.x).
Validation at review time: `ruff check` + `ruff format --check` passed; 151 fast tests passed; real-runtime suite reached the demo-corpus test then timed out.

## Findings being fixed

1. Standards, judgement call: duplicated `_install_relationship_methods()` call in both branches of `register_hooks()`.
2. Spec High: legacy foreground background actions are not wired.
3. Spec Medium: incomplete-runtime failures leak raw imports instead of actionable errors.
4. Spec Medium: legacy real-runtime coverage is partial.

## 1. Hoist `_install_relationship_methods()`

File: `src/docassemble_simulator/_runtime.py:920-931` (`register_hooks`).

Current shape:

```python
if not _modern_hooks_available():
    _ensure_runtime_present()
    _install_relationship_methods()
    _register_legacy_runtime_bindings(bindings)
else:
    _install_relationship_methods()
    _register_pluggy_runtime_bindings(bindings)
```

Fix:

```python
def register_hooks() -> None:
    bindings = _SimulatorRuntimeBindings()
    _install_relationship_methods()
    if not _modern_hooks_available():
        _ensure_runtime_present()
        _register_legacy_runtime_bindings(bindings)
    else:
        _register_pluggy_runtime_bindings(bindings)
```

Notes:

* No behavior change; relationship-method installation is family-independent.
* Covered by existing `TestRuntimeBindings`.
* Run `ruff check` and `ruff format --check`.

## 2. Wire legacy `server.bg_action` (High)

### Problem

`_install_background_action_fallback()` (`src/docassemble_simulator/_runtime.py:1081-1096`) does:

```python
from docassemble.base import background, functions, util
except ImportError:
    return
```

Docassemble 1.9 has no `docassemble.base.background` module, so the whole fallback returns early on legacy runtimes. `_register_legacy_runtime_bindings()` (`_runtime.py:825-905`) also never installs `server.bg_action`, while 1.9 `background_action()` delegates through `functions.server.bg_action`.

This violates issue #1 story 16: foreground background actions must retain local semantics under 1.9.x.

### Fix

* Confirm the 1.9 dispatch seam from the 1.9.8 source: `functions.background_action -> server.bg_action(action, ui_notification, **kwargs)`.
* Split the fallback into modern and legacy paths sharing `_foreground_background_action`:
  * Modern: keep patching `functions.bg_action`, `functions.background_action`, `util.background_action`, and `background.bg_action`.
  * Legacy: patch `functions.server.bg_action`, plus `functions.background_action` / `functions.bg_action` where those attributes exist.
* Preserve ADR-0003: run supported actions in the current working state, return pending `SimulatorTask` in `disabled` mode, never emulate Celery queue latency/retries/isolation.
* Preserve idempotence across repeated `bootstrap()` / `register_hooks()` calls via `_BACKGROUND_INSTALLED`.

### Tests (`tests/test_bootstrap.py`)

* Legacy fallback with stubbed `functions` exposing `server = SimpleNamespace()` and no `background` module: assert `server.bg_action is _foreground_background_action` after install.
* Legacy `disabled` mode: call through the installed seam returns `not ready()` and `not failed()`.
* Re-install preserves behavior and object identity where appropriate.
* If a minimal background-action fixture is available, run it through the legacy lane; otherwise stub-level coverage is sufficient for this fix.

## 3. Make incomplete runtimes fail actionably (Medium)

### Problem

`register_hooks()` treats “no `plugin_manager`” as “legacy”, then `_install_relationship_methods()` imports `docassemble.base.util` before legacy capability validation. A runtime missing `util`, `functions.server`, or required thread helpers can therefore leak raw `ModuleNotFoundError` / `AttributeError`.

Issue #1 requires distinguishing supported 1.9.x, supported 1.10+, installed-but-incomplete, and wrong-interpreter cases with actionable messages.

### Fix

In `register_hooks()` and the legacy construction path in `runtime_context()`:

1. Call `_ensure_runtime_present()` first.
2. Import required legacy modules (`functions`, `util`, `config`) inside `try/except ModuleNotFoundError/ImportError`; on failure raise `RuntimeCompatibilityError` naming the missing module and the expected families (`1.9.x or 1.10+`) plus the target-interpreter hint used by the existing message.
3. Then validate required attributes (`server`, `this_thread`, `populate_this_thread_defaults`, `backup_thread_variables`, `restore_thread_variables`, `Individual` for relationship methods) before mutating shared state.
4. Confirm CLI mapping: `RuntimeCompatibilityError(ValueError)` currently flows through the `ValueError` handler in `cli.py` (exit 1). Keep that or map it explicitly to the structured `compile`/`fault` envelope; do not let it become an unhandled traceback.

### Tests

* Missing `docassemble.base.util` raises `RuntimeCompatibilityError` mentioning `util`.
* `functions` without `server` keeps the existing actionable message.
* Missing `backup_thread_variables` case already covered; keep it.
* `plugin_manager` import failure where `error.name != "docassemble.base.plugin_manager"` is re-raised unchanged, not masked as legacy.

## 4. Extend legacy real-runtime coverage (Medium)

### Problem

Only `test_minimal_start_answer_contract_across_families` (`tests/test_real_runtime.py:224`) is parameterized over `family_python` (modern + legacy). These remain modern-only via `real_python`:

* `test_real_runtime_rehydrates_helpers_for_every_render_source`
* `test_generated_attachment_has_durable_local_uri_and_manifest`
* `test_real_date_answer_formats_and_rejections_roll_back`

Issue #1 asks for render-source, artifact/file-lookup, and PDF-boundary compatibility under 1.9.x as well.

### Fix

Parameterize in this order:

1. Render-source rehydration (`fresh` / `fixture` / `snapshot` / `saved`).
2. Attachment durable URI + manifest.
3. Date answer formatting and rollback.

Implementation notes:

* Reuse existing `family_python` + `real_workspace` fixtures; include `label` (`1.10+` / `1.9.x`) in asserts for attribution.
* Preserve skip semantics: legacy param skips when `DASIMULATOR_REAL_PYTHON_19` is unset; modern lane always runs.
* Keep `test_demo_corpus_runner_canary` modern-only unless a provisioned 1.9 corpus run is explicitly requested. The review run timed out in that test, so do not gate legacy coverage on it: split the slow corpus test from the contract tests, raise the timeout, or document it as a slow lane.
* `scripts/test-real-runtime` already accepts an optional second 1.9 interpreter and exports `DASIMULATOR_REAL_PYTHON_19`; verify that path during validation.

### Acceptance

* Modern interpreter only: legacy params skip, all modern tests pass.
* Both interpreters provisioned: render / attachment / date pass on both families.
* No changes to saved-session, snapshot, or render-source schemas.

## Validation gates

* `ruff check .`
* `ruff format --check .`
* `pytest -q tests/test_bootstrap.py`
* Fast suite excluding `tests/test_real_runtime.py`
* `scripts/test-real-runtime <1.10-python> [<1.9-python>]`, or equivalent `DASIMULATOR_REAL_PYTHON` + `DASIMULATOR_REAL_PYTHON_19` run
* `git diff --check`

## Suggested order

1. Section 1 (trivial, isolated).
2. Section 3 (error-handling foundation for legacy paths).
3. Section 2 (highest spec risk).
4. Section 4 (needs provisioned 1.9 interpreter; depends on section 2 for background-action interviews).

## Out of scope

* Changing the public CLI vocabulary, JSON envelope, operation types, saved-session schema, snapshot schema, or render-source model.
* Supporting docassemble versions outside the declared 1.9.x floor and current 1.10+ family.
* Adding Redis, Celery, Flask, database, or PDF-converter infrastructure.
* Rewriting preflight acquisition or the `jda` launcher model.
