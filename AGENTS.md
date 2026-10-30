# AGENTS.md

## Agent skills

### Issue tracker

Issues are tracked in this repo's GitHub Issues via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### Release tooling

Releases are cut locally with `mise run release` (see `scripts/release`) and
require the GitHub CLI, `gh`, on `PATH`. `gh` is intentionally **not** declared
as a mise tool: Homebrew provides it, and Homebrew's formula ships the zsh
completion that mise's package lacks. Install it with Homebrew (`brew install
gh`). The script checks for `gh` up front and aborts before any bump or push if
it is missing, so a missing CLI never leaves a half-published release.

The GitHub release created by `mise run release` triggers
`.github/workflows/python-publish.yml`, which builds the wheel and sdist and
publishes them to PyPI using trusted publishing. The PyPI pending trusted
publisher must name owner `jpagh`, repository `docassemble-simulator`, workflow
`python-publish.yml`, and environment `pypi`. The release gate
(`mise run test:runtime`) includes `mise run test:py310`, the CPython 3.10 fast
lane for legacy docassemble 1.9.x targets.

`mise run test:py310` provisions `.venv-310`, so `.venv-*/` is ignored rather
than only the named runtime lane environments.
