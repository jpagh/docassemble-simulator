---
status: accepted
---

# Substitute no server database for AssemblyLine session persistence

The simulator's generated defaults previously claimed a local SQLite database
(`db.database name`, `db.driver`). Neither is a docassemble configuration key:
docassemble expects `db.prefix`/`db.name` and fills in PostgreSQL defaults when
they are absent, and its database layer supports only
PostgreSQL/MySQL/Oracle -- there is no SQLite path. The broken default went
unnoticed because nothing exercised docassemble's database until
`docassemble.AssemblyLine 4.8.0` added an `initial: True` block in
`al_saved_sessions.yml` that calls `update_session_metadata`, which is
hard-coded PostgreSQL (`pg_advisory_xact_lock`, `jsonb`, `||`,
`CAST(... AS jsonb)`). Every AssemblyLine interview then attempted a
`localhost:5432` connection during `start`.

## Decision

- The simulator substitutes no SQL database. The bogus `db` default is removed;
  the simulator's generated defaults contain no `db` section.
- `assembly line.update session metadata` defaults to `false`, because a local
  run has no server session store. A project can opt back in with the
  docassemble-compatible key when it configures a real database.
- AssemblyLine's saved answer sets, interview list, and session metadata remain
  deployment-only capabilities. The simulator does not rewrite or shim
  installed AssemblyLine functions, and `docassemble.webapp`'s session-vault
  hooks (`create_session`, `set_session_variables`, ...) are not emulated.
- The CLI's own durable state stays `.simulator/sessions/`; compatibility
  checks and main journeys -- compile, start, questions, documents, downloads --
  must not touch a database.
- `info` reports the boundary as a capability.

## Consequences

- Fresh AssemblyLine-backed projects can step through complete interviews with
  no PostgreSQL and no unexpected connection attempts.
- Users who had (or expected) server-side session metadata locally lose it by
  default; the override and its prerequisite real database are documented in
  `docs/runtime-compatibility.md`.
- The simulator carries no persistence adapter, so it cannot drift from
  AssemblyLine's own storage semantics; a future AssemblyLine release that adds
  another unconditional database write will surface as a real database error
  rather than a silent local substitute.
