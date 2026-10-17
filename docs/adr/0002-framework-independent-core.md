# ADR 0002: Extract a framework-independent review and decision core

## Status

Accepted

## Context

Review models, three-valued condition evaluation, pipeline traversal, OneBot
approval recovery, commands, and background tasks lived in one plugin module.
Importing a pure rule helper also imported the plugin registration entry point,
required an initialized NoneBot driver, and created the runtime config directory.

The V3 architecture needs a reusable business core before adding an independent
OneBot entry point or expanding the management interface. The existing approval
and identity race regressions establish behavior that extraction must preserve.

## Decision

- Add `src/gatekeeper/` as an independent package. It owns shared configuration
  schemas, check and review models, parsing, conditions, pipeline traversal,
  formatting, decision locks, and `DecisionService`.
- Core imports never depend on NoneBot or `src.plugins`, initialize storage,
  create runtime directories, or open connections.
- Keep OneBot events and `Bot` objects in the plugin. Convert approval requests
  to plain `DecisionRequest` data and translate only explicit `ActionFailed`
  responses into the neutral `DefiniteActionFailure` exception.
- Supply asynchronous persistence, external action, and notification ports to
  `DecisionService`. `SqliteDecisionStorage` delegates complete atomic operations
  to the existing record manager on its storage executor.
- Forward only the core decision logger through NoneBot's `LoguruHandler` at
  plugin composition. This retains daily file diagnostics without changing the
  process-wide logging configuration or importing the framework into the core.
- Let the pure pipeline yield one step at a time. The plugin appends its trace,
  executes ordered effects, and then resumes traversal with the actual dispatch
  result. A later routing error cannot erase earlier traces or move effects.
- Keep existing plugin model and formatter import paths as compatibility exports
  while callers migrate to the core.

## Behavior contracts

- T/F/I priority, configured reasons, trace text, routing, and the final
  true-to-next fallback retain their existing semantics. Synthetic fallback
  approval emits its trace without dispatching a second action handler.
- Request-key locks serialize duplicate external decisions. Identity locks span
  binding reads, evaluation, external decisions, and audit finalization.
- Persist `pending` before an external action. Transport errors and cancellation
  leave the action unconfirmed, pause new automatic decisions, and require
  operator resolution. An explicit action failure can finalize it as failed.
- Successful finalization updates action status, statistics, and bindings in one
  existing SQLite transaction. Manual recovery uses the same identity locks.
- Restore the pause state from pending records during startup. An unrelated
  action already in flight may finish after another action causes a pause.
- Configuration formats and precedence, SQLite schema, and public commands keep
  their current behavior for this extraction. ADR 0001 continues to govern the
  storage boundary.

## Validation

Run core tests directly without framework initialization. A separate isolated
Python process rejects all NoneBot and plugin imports and verifies that importing
every core module leaves an empty temporary working directory untouched.

Retain real SQLite handler regressions for competing identities, duplicate
requests, lost responses, cancellation, binding removal, and operator recovery.
Adapter tests verify trace-before-effect ordering and failure timing.

## Consequences and follow-up decisions

The current NoneBot entry point composes reusable business logic. A future
independent OneBot entry point can use the same models and services.

Scoped pending isolation needs durable reservations for related request, QQ, and
Bilibili identities. Membership observations alone do not prove which approval
action succeeded, and uncertain actions cannot be retried without a reliable
request-level outcome or idempotency contract. This requires a separate ADR.

Moving business configuration into versioned SQLite storage would replace ADR
0001's configuration decision and requires its own migration and rollback plan.
Configuration snapshots, request rate control, UID lookup, expression syntax,
ownership challenges, and member patrol are subsequent work with separate
behavior and compatibility checks.
