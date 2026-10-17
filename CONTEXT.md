# Domain Context

This project reviews QQ group join requests using Bilibili account checks and
per-group review pipelines. A supported deployment runs one Bot process.

## Terms

- **Join request**: one OneBot group-add event, identified for action deduplication
  by Bot ID, group ID, subtype, and a nonempty request flag.
- **Check result**: evidence obtained from a Bilibili or QQ query. `passed`,
  `failed`, `inaccessible`, and `not_required` describe the check, not the final
  admission decision. An incomplete or unavailable query cannot establish a miss.
- **Condition**: a configured rule evaluated as T (`true`), F (`false`), or
  I (`unknown`). Missing evidence and failed identity lookups remain unknown.
- **Review stage**: a named group of conditions, aggregation mode, and one branch
  for each T/F/I result. A branch has one flow action and optional side effects.
- **Flow action**: `next`, `approve`, `reject`, or `ignore`. `next` can select a
  configured target stage. A reached cycle or missing target is an error.
- **Side effect**: an ordered `notify_admin` or `log_warn` action emitted by a
  stage. The stage trace is available before its side effects run.
- **Review decision**: the pipeline's intended admission result and reasons.
  This is separate from the outcome of a OneBot action.
- **Action status**: `pending` is durable intent whose result is unconfirmed;
  `applied` is confirmed execution; `failed` is an explicit action rejection;
  `not_required` means the audit does not require an external approval action.
- **Binding**: a recorded QQ/Bilibili identity relationship used by `no_conflict`
  and `no_identity_change`. It does not prove initial account control.
- **Identity decision lock**: process-local serialization of the QQ and Bilibili
  UID from binding lookup through decision finalization. Manual resolution and
  binding removal share the same lock manager.

## Implementation boundaries

`src/gatekeeper/` owns framework-independent models, condition calculation,
pipeline traversal, formatting, input parsing, and durable decision orchestration.
Its imports do not register handlers or create runtime files.

`src/plugins/group_request_manager/` owns NoneBot registration, OneBot event and
exception translation, Bilibili HTTP, SQLite, configuration I/O, WebUI, and workers.
The plugin composes the core with external adapters; the core never imports the
plugin. See [ADR 0002](docs/adr/0002-framework-independent-core.md).
