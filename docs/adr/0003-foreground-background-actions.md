# ADR-0003: Foreground background actions are a local default

## Status

Accepted

## Decision

When running locally, the simulator replaces docassemble's Celery dispatch for
supported `background_action()` events with an immediate task value. The event
runs in the current interview namespace and thread context, and its task is
persisted as part of ordinary session state. A response action is followed in
the same context. The explicit `simulator.background_actions = "disabled"`
mode retains a pending/stub task for diagnosis.

## Boundary

This policy does not emulate queue latency, worker isolation, retries, timeouts,
process failures, or external service behavior. Unsupported actions fail with a
clear simulator limitation rather than being reported as successful. Real
Celery behavior remains a deployment/integration-test responsibility.
