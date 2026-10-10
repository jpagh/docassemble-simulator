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
