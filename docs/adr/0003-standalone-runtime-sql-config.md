# ADR 0003: Standalone runtime, SQL configuration and scoped decisions

## Status

Implemented in the V3 work; integration and deployment acceptance are tracked separately. Supersedes ADR 0001's external business-configuration boundary and ADR 0002's framework entry point/global pending pause.

## Decision

- Compose one FastAPI lifecycle from `app/`, connecting directly to OneBot v11 with forward or reverse WebSocket and echo correlation. Default modules never import NoneBot or the legacy plugin package.
- Preserve `src/gatekeeper/` as a pure core; add bounded AST evaluation with Kleene T/F/I and raw numeric observations. Continue to accept v2 `conditions + mode`.
- Upgrade the existing database to schema4 without rewriting old business rows. Compile effective legacy configuration once, preserving precedence, paths, raw sources and private backups; thereafter SQL revisions are authoritative.
- Use append-only revisions with compare-and-swap versions. Restore appends a new revision. Credentials are atomically paired with generation checks; public snapshots and ordinary YAML exports are redacted.
- Persist action intent before OneBot. Unknown outcomes reserve only related QQ/UID/request identities. Reconciliation requires exact request evidence; membership alone is insufficient. Without reliable evidence, keep the record for operator confirmation.
- Keep ownership and patrol opt-in. A claimed UID or client-submitted success is not proof. Privacy/API errors remain Unknown and never justify automatic member removal.
- Support one application process. SQL transaction safeguards do not establish multi-worker deployment support.

## Consequences

Old YAML/JSON/environment group settings stop overriding SQL after migration. Operators must use versioned configuration editing/import, preserve full infrastructure and database backups, and distinguish configuration rollback from binary/schema downgrade.

Fixtures and import tests prove local contracts, not current Bilibili API availability or real QQ actions. Production acceptance must state exactly which readonly queries and controlled actions were executed.
