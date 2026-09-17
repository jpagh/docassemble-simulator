# Domain context

- **Interview definition**: A docassemble YAML program identified by its canonical package path.
- **Interview catalog**: Read-only discovery and compiled metadata for interview definitions; it never creates session state.
- **Execution operation**: One complete request to start, inspect, mutate, seek, evaluate, execute, or prepare render state. It owns lifecycle ordering and transaction policy.
- **Working state**: An operation-local interview namespace and outcome loaded from durable state or created fresh. It never escapes execution.
- **Saved session**: The versioned, per-interview durable record of a working state and its latest outcome, stored per effective configuration so a different config never rehydrates it.
- **Screen outcome**: A typed description of a question, continuation, completion, or reproducible flow error.
- **Active seek**: A sought screen persisted as the session's active target until answered or refreshed.
- **Variable-seek diagnostic**: A non-fatal trace event showing which variable or question docassemble considered while trying to resolve an undefined reference.
- **Unresolved-variable failure**: The terminal result when docassemble exhausts variable seeking without finding a question or code block capable of defining the sought variable.
- **Render source**: Exactly one namespace origin for rendering: saved session, fresh state, snapshot, or fixture.
- **Snapshot**: A versioned, interview-specific namespace capture used as an explicit render source; it is not a saved session.
- **Artifact**: A successfully rendered DOCX written to an explicit target path.
- **Published attachment**: A generated local interview file exposed to the user as a logical download result; intermediate rendering files are not published attachments.
