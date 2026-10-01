# ADR 0001: Use SQLite for mutable business records

## Status

Accepted

## Context

Join request audits, QQ/Bilibili bindings, and leave records were stored in JSON files.
The application loaded complete JSON documents into memory and rewrote an entire file for
each update. This was simple at small scale, but write cost and linear scans grew with the
audit history. The process-local lock also could not coordinate a second process.

Configuration remains human-maintained and has different requirements from mutable records.

## Decision

Store mutable business records in `data/records/gatekeeper.sqlite3` using Python's standard
library `sqlite3` module.

- Enable WAL mode, transactions, a busy timeout, and indexes for common lookups.
- Keep YAML, `.env.prod`, and runtime configuration files outside SQLite.
- On first database initialization, import legacy join-request and leave-record JSON files in
  one transaction and write a migration marker.
- Never modify or delete legacy JSON files during automatic migration.
- Preserve the existing manager APIs so command, review, and WebUI code do not depend on SQL.
- Track schema changes with `PRAGMA user_version` and apply upgrades sequentially.
- Record external approval decisions as `pending` before calling OneBot, then finalize them as
  `applied` on success or `failed` on an explicit OneBot failure response. Timeouts, disconnections,
  and cancelled calls remain `pending` and pause automatic decisions until an operator confirms
  their actual outcome; a transport error does not prove that QQ left the request untouched.
- Mark audits that require no OneBot action as `not_required`, and persist a unique request key so
  redelivered join-request events cannot execute the same approval action twice.
- In the supported single-Bot deployment, hold process-local locks for the applicant QQ and Bilibili
  UID from binding lookup through approval finalization. Unrelated identities remain concurrent;
  manual binding removal and pending-decision confirmation use the same identity locks.
- Serialize concurrent copies of the same request through its finalization. A duplicate that is
  still running in this process is not treated as an orphaned `pending` decision.

## Consequences

- Writes update only affected rows and related records are committed atomically.
- QQ, Bilibili UID, group, and recent-audit queries use indexes instead of full JSON scans.
- Blocking SQLite calls run in worker threads so lock waits do not stall NoneBot's event loop.
- Operators must account for `-wal` and `-shm` files when backing up a running database;
  stopping the Bot before copying `data/` remains the simplest safe procedure.
- Restoring a pre-migration release requires restoring the retained JSON files or exporting
  SQLite data back to the legacy format.
