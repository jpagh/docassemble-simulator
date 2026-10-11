# ADR 0015: Persistent worker and standard project runner

- Status: accepted
- Date: 2026-10-11

## Context

A one-shot simulator process pays Python, docassemble, preload, and interview
startup costs for every operation in a journey. Project tests also repeatedly
reimplemented process management, JSON transport, timeouts, and temporary
session directories.

## Decision

The simulator provides a newline-delimited JSON worker through `sim run` (with
`sim worker` as an equivalent name). One worker is scoped to one project root,
interview selector, effective configuration, and session directory. It
serializes requests and returns one response containing the request `id`,
`ok`, the existing `result` or `error` payload, the scope identity, and
optional diagnostics, attachments, and timing data. `start`, `answer`, `seek`,
`refresh`, `eval`, `exec`, `vars`, and `status` use the same typed operations as
the one-shot CLI. `reset` and `shutdown` are explicit lifecycle operations.

`docassemble_simulator.SimulatorRunner` is the standard Python client. It
launches that same worker protocol, applies per-operation timeouts, and cleans
up the child process and temporary workspace in a context manager. Its default
source policy copies the project and gives the journey a temporary session
root; `source_policy="symlink"` and `isolate=False` are explicit alternatives.
A session root can be supplied when a journey must be resumed by another
worker. The command-line worker accepts the same explicit `--session-root` and
`--timeout` controls and otherwise isolates its temporary session.

`seed` / `state-seed` is an explicit local test operation for executing code in
the saved interview namespace. It is not a browser submission and is included
in responses as `kind: state-seeded`. Dynamic `fields: code` screens report
`dynamic_fields: true` and `field_source: code` without fabricating browser
controls.

## Consequences

- Runtime bootstrap, installed-module preload, and docassemble's process-level
  imports are amortized across a journey. The saved-session pickle remains the
  continuity and isolation boundary; worker memory is not cross-session reuse.
- A worker does not support concurrent mutations. Parallel journeys use
  separate workers and session roots.
- A worker timeout returns a typed `timeout` error when the process can recover;
  the Python client terminates the child if no response arrives. Logs remain on
  stderr so stdout stays machine-readable.
- Source copying is safer but can cost setup time. Projects with a deliberate
  shared or symlinked source policy must opt into it explicitly.
- Existing one-shot commands are unchanged and remain the compatibility path
  for simple shell invocations.
