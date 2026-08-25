# Code Review Fix Plan

Review date: 2026-08-23
Scope: whole project (greenfield), commit `ba15510`
Reviewer: code review (1 reviewer agent), verdict **REQUEST_CHANGES**

## Major — must fix before release

### 1. `--stub-defined` flag is a silent no-op

- Where: `src/docassemble_simulator/bootstrap.py:249-261`
- `apply_session_stubs` accepts `stub_define_defined` but never references it. Only
  `set_sessions_data`, `set_sessions_title_stage`, `cleanup_sessions`, and `url_of` are
  stubbed; `define()`/`defined()` are never patched. Both CLI help and README advertise
  the flag for older docassemble setups.
- Fix: implement the stubbing under the flag (patch `docassemble.base.functions.define/defined`
  and util aliases when requested) or remove the flag, its help text, and the README paragraph.

### 2. Per-package config overrides leak into the global home config

- Where: `src/docassemble_simulator/bootstrap.py:128-144`
- `prepare_environment` writes the merged config (`--config` plus discovered TOML)
  into `~/.config/docassemble-simulator/config.yml`, which persists after exit. Package A's
  overrides then silently apply to unrelated package B on later runs.
- Fix: write the effective config to a root-scoped location (e.g.
  `<root>/.simulator/config-effective.yml`, or a temp dir keyed by resolved-root hash)
  and point `DA_CONFIG_FILE` there.

### 3. `SessionError` escapes every command as an unhandled traceback

- Where: `src/docassemble_simulator/cli.py:285-288`
- `load_state_full()` raises when `session.pkl` is missing/stale/unpicklable; no command
  catches it (`cmd_status` imports `SessionError` without using it). Running
  `set/status/get/exec/vars` before `start` dumps a full Python traceback instead of the
  intended one-line message.
- Fix: catch `SessionError` centrally in `main()` around `args.func(...)`, print the message
  to stderr, exit 1.

### 4. Pickled-screen validation ignores `visible` and `required` flags

- Where: `src/docassemble_simulator/session.py:410-416`
- The no-live-question fallback checks every field via `_check_required_field` without
  consulting `f.get('required')` or `f.get('visible')`, unlike the live-field path. Produces
  spurious "required field undefined" warnings (or blocks under `--strict`) for `[optional]`
  and `[hidden]` fields on synthesized screens.
- Fix: skip fields where `f.get('required') is False` or `f.get('visible') is False`.

## Minor

5. **Bare-variable `show if:` form ignored** — `describe.py:60-70`. `field_visible` requires
   both `show_if_var` and `show_if_val`; with only `show_if_var` it falls through to
   "always visible", misreporting hidden required fields as answerable. Evaluate the single-key
   case against target `True` with the same sign handling.
6. **Inconsistent exit codes on error screens** — `cli.py:281-307`. `start`/`set` return 2 on
   `kind == 'error'`; `status` always returns 0 (and `seek` returns 0 when its result describes
   an error screen). Mirror the start/set behavior so error screens are exit-distinguishable
   everywhere.
7. **Non-atomic session save** — `session.py:136-141`. Direct pickle write to `session.pkl`;
   a crash mid-write corrupts state and forces manual deletion. Write to a sibling temp file
   and `os.replace()` onto the final path.
8. **`cmd_vars` re-parses the interview per variable** — `cli.py:490-494`. O(n) re-parse/recompile;
   evaluate against the already-loaded interview object instead of calling
   `session.load_interview()` inside the loop.
9. **Zero tests project-wide** — add unit tests for pure/near-pure logic: `parse_value`'s
   JSON → ast-literal → string fallback chain, `field_visible`'s two show-if encodings and sign
   semantics, `validate_screen`'s error/warning ordering, `_deep_merge`,
   `resolve_interview` ambiguity handling, and `guess_main_interview` precedence.

## Nits

10. `_deep_merge` duplicated identically in `cli.py:852-858` and `bootstrap.py` — keep one,
    import it.
11. Dead `check --all` flag (`cli.py:528-530`) — never read; omitting `--interview` already
    checks all interviews. Remove or wire up.
12. Placeholder-less f-strings at `cli.py:244-246`.
13. `DYLD_FALLBACK_LIBRARY_PATH` hardcoded to `/opt/homebrew/lib` (`cli.py:807-826`) — wrong on
    Intel macs and custom installs. Append candidates or honor an existing user setting.
14. Redundant `not isinstance(val, str)` after `isinstance(val, (dict, list))` at `cli.py:49-51`.
15. `start --set` skips submit-time validation that `set` performs (`cli.py:254-279`) despite
    help claiming the same grammar. Replay validation or document the difference.

## Suggested order of work

1. Items 1–3 (advertised-behavior breaks): smallest blast radius, user-visible correctness.
2. Item 2's fix touches `bootstrap.prepare_environment`; pair with item 3's central catch in `main()`.
3. Items 4–6 (validation/exit-code consistency) together, since they share screen-validation paths.
4. Item 7–8 performance/durability; item 9 tests last so they cover the fixed behavior.
