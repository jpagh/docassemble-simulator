---
status: accepted
---

# Treat resolved variable seeking as diagnostics

Docassemble uses `NameError` and Jinja `UndefinedError` as control signals while recursively seeking variables, so the simulator reports the resulting ordered variable/question trace as non-fatal diagnostics rather than raw errors. Only docassemble's terminal missing-variable result becomes an `unresolved-variable` failure; it retains the sought variable and trace so users can understand how resolution was attempted.
