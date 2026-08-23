# docassemble-simulator

Run a docassemble interview locally, without a server. Loads the interview
through the real docassemble compiler and drives the real seek/assemble
machinery — the same code paths the webapp uses on every request — so
variable-resolution failures, broken conditions, and bad screens reproduce
exactly.

Designed to be driven by an AI agent: small stateless commands, JSON output,
a persistent session file, and no server or browser needed.

## Install

The simulator must run inside an interpreter that can import both the target
package and `docassemble.base` — i.e. the package's own virtualenv:

```sh
uv pip install --python /path/to/docassemble-package/.venv/bin/python docassemble-simulator
# then either
/path/to/docassemble-package/.venv/bin/docassemble-simulator --help
# or, from anywhere with uv:
uv run --python /path/to/docassemble-package/.venv/bin/python docassemble-simulator --help
```

## Run

From inside a docassemble package directory (one containing `docassemble/<pkg>/`):

```sh
docassemble-simulator info                 # what did we find here?
docassemble-simulator check                # compile-parse every interview; report errors
docassemble-simulator questions            # compiled blocks/screens in order (--var to filter)
docassemble-simulator index --var M.family # variable -> defining screen mapping
```

## The agent loop

```sh
docassemble-simulator start                 # fresh session; runs mandatory chain; shows first screen
docassemble-simulator set M.parties[0].name.first=Alice
docassemble-simulator set continue=true     # yes/no screens
docassemble-simulator status                # current screen again, any time
docassemble-simulator get 'M.parties[0].name.first'
docassemble-simulator vars                  # dump session variables
```

Every `set` applies values in the interview namespace, marks the current
screen's question answered (as the server does after a POST), re-runs the
mandatory chain, prints the next screen, and saves state to
`.dasimulator/session.pkl`. Add `--json` to any command for machine-readable
output.

### Value parsing

`set VAR=VALUE` parses VALUE as JSON first (`true`, `false`, `null`, `42`,
`3.5`, `"text"`, `[1,2]`), then as a Python literal (so `['a','b']` with
single quotes works), then falls back to a literal string. Use `--code` to
treat the value as a Python expression instead:

```sh
docassemble-simulator set M.parties.there_are_any=true
docassemble-simulator set --code "M.parties.append_object('Individual')"
```

For anything the `set` grammar can't express (list appends, object setup),
use `exec`:

```sh
docassemble-simulator exec "M.parties.append_object('Individual')" --show
```

## Stubbing server-only dependencies: the prelude

Interviews that read the firm DB, PMS OAuth state, or other server-only
services can be unblocked with `.dasimulator/prelude.py`. It runs in the
session namespace after util imports but before every mandatory-chain pass,
so it can patch module attributes or seed variables:

```python
# .dasimulator/prelude.py
import docassemble.automatedpleading.dw_pms as _pms
_pms.DWClioAuth.get_credentials = lambda self: type("C", (), {"apply": lambda s, h: None})()
```

The prelude re-executes on every `start`/`set`/`status`/`seek --continue`,
because each CLI invocation is a fresh process.


## Debugging variable-resolution failures

```sh
docassemble-simulator seek M.family.parenting_plan.custody_legal_type --trace
```

`seek` drives `interview.askfor()` directly from a fresh (or, with
`--continue`, the saved) session and reports which screen the variable
resolves to, or the exact failure ("could not be looked up in the question
file"), plus the full seek chain of variables/questions considered.
`index --var NAME` tells you whether any screen defines the variable at all.

## What's stubbed vs real

Real: YAML compilation, mandatory code blocks, objects/data blocks, question
selection and ordering, condition evaluation, multiple-choice following,
show-if/hide-if field visibility (both `code:` and `variable`/`is` forms),
submit-time `validation code` replay, and the required-field structure.

Stubbed: redis (`FakeRedis` module swap), pluggy webapp hooks (minimal local
implementations, including button-class and URL generation so UI fragments
built during assembly don't crash), DB session bookkeeping
(`set_sessions_data`, `cleanup_sessions`, ...). Screens cannot render HTML
and signature/file widgets have no upload path — you set their variables
directly.

Some older setups also need `define()`/`defined()` stubbed (the original
harness did); pass `--stub-defined`. On recent docassemble versions this
breaks `defined()` branching, so it's off by default.

### Submit-time validation replay

Every `set` replays what the server does after a POST:

1. values are written into the namespace;
2. the current screen's `validation code` is executed — a raised
   `DAValidationError` (or any crash) rejects the submission: the command
   reports `kind: invalid`, **discards the answers** (server-faithful), and
   leaves you on the same screen;
3. required fields left undefined are reported as warnings (the browser
   enforces these before POST); `--strict` promotes them to blocking errors;
   `--no-validate` skips the replay entirely.

Field descriptions also show computed `[hidden]` markers for fields whose
`show if` currently evaluates false.

Note: checkbox groups (`datatype: checkboxes`) are stored as `DADict`
(choice -> bool), not lists — the validation replay will tell you when a
value has the wrong shape:

```sh
docassemble-simulator set --code "documents.selected_documents = DADict('documents.selected_documents', elements={'family': True}, auto_gather=False, gathered=True)"
```
