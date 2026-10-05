# Operate PIG storage

Use a dedicated PostgreSQL 15–18 database and an S3 bucket, Azure container, or
GCS bucket. Keep database backups and object recovery enabled. PIG retains all
trace data, source offsets, analysis history, and object versions by default.
Size and monitor both stores as ingestion grows.

## Database credentials

The default installation uses one database owner credential. To give serving
pods fewer privileges, supply separate application and migration credentials:

```yaml
storage:
  postgres:
    dsnSecretRef: {name: pig-credentials, key: postgres-dsn}
    migrationDsnSecretRef: {name: pig-migration, key: postgres-dsn}
```

Both DSNs must name the same database endpoint and use `sslmode=verify-full`.
Use `caConfigMapRef` for a private certificate authority. Preflight and migration
Jobs receive the migration credential; serving, verification, and acceptance
pods receive only the application credential. The migration role must own the
worker tables and have `CREATE` permission on the `public` schema. For an
existing installation, its current owner can serve as the migration role.

For a fresh database named `pig`, a database administrator can create the roles
and grants below. Configure login credentials through your secret-management
process, then connect to `pig` before running the schema grants:

```sql
CREATE ROLE pig_migrator LOGIN;
CREATE ROLE pig_app LOGIN;
GRANT CONNECT ON DATABASE pig TO pig_migrator;
GRANT CONNECT, TEMPORARY ON DATABASE pig TO pig_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO pig_migrator;
GRANT USAGE ON SCHEMA public TO pig_app;
```

Migrations grant the application role data access to worker tables and access
to the deployment ledger. They do not grant schema ownership or create roles.
The application uses temporary tables while updating trace lineage. A database
administrator must retain its `TEMPORARY` permission if public grants are revoked.
Keep the migration role out of the application role's memberships.

To replace an existing installation's database login with a new application role,
create the role and grants above before changing the serving Secret. In the current
worker image, set `INSTRUCTION_HUB_CUSTOMER_POSTGRES_DSN` to the new application
DSN and `INSTRUCTION_HUB_MIGRATION_POSTGRES_DSN` to the existing owner DSN. Run
`pig-trace-analyzer migrate`, then `pig-trace-analyzer db-status`. At the current
schema revision, migration applies the grants without changing the schema. After
both commands succeed, update the deployment's credential references and Secret.
Configuration and credential rotations do not rerun release migrations.

## Install and update

The supervisor checks permissions, PostgreSQL version, schema layout, TLS, and
object read/write access before stopping the analyzer. It then waits for analyzer
pods to exit, runs migrations, and starts the target image. A single transaction
and advisory lock serialize each migration chain. Failed migrations roll back;
rerunning a completed migration is safe. Unknown revisions, changed historical
checksums, and incomplete schemas block installation without resetting data.

The candidate targets schema revision 4. Alembic records it in
`pig_alembic_version`; `pig_schema_migrations` retains the historical checksums.
Use a forward repair after a schema transition. An older image must not serve a
newer schema, and the worker does not run migrations at HTTP startup.

`/readyz` withdraws traffic when storage checks fail or expire. `/healthz` checks
background task liveness, so a dependency outage does not itself cause repeated
pod restarts. Readiness checks run every 15 seconds. Canonical-object repairs run
without new host uploads and retry failed rows with delays capped at five minutes.

For manual Helm installations, stop the analyzer and wait for its pods to exit
before upgrading. Set `secrets.migrationPostgresDsnKey` to an existing Secret key
for separate migration credentials. The chart does not stop existing pods itself.

## Diagnose a blocked worker

Run these commands in the target worker image with database credentials supplied
through the environment. They need no hosted token, model key, or bucket access:

```sh
pig-trace-analyzer db-status
pig-trace-analyzer db-check
```

`db-status` reports schema compatibility, pending objects, queued analyses,
expired claims, dirty projections, and the oldest pending work. `db-check` also
checks the migration role and schema layout. Commands return JSON and a nonzero
exit status on failure, without credentials or database error payloads. Readiness
changes appear in worker logs with a `storage_reason` field.

Check pool exhaustion, database connections and disk usage, object-store errors,
and backlog age when work stops progressing. Each process uses at most eight
pooled database connections. Connections, lock waits, SQL statements, and SDK
network requests have timeouts. In-flight storage work drains during shutdown;
unfinished analyses become claimable after their leases expire.

## Recover from a backup

Choose a database recovery point and object recovery state that includes every
raw and context object referenced by that database. A quiesced backup of both
stores provides that boundary. Keep the matching worker image and configuration
with the recovery record. Database recovery can lose acknowledged uploads after
the chosen point; canonical reconstruction cannot recover those missing rows.

1. Suspend GitOps reconciliation and stop the supervisor and analyzer. Wait for
   analyzer and maintenance Job pods to exit before restoring either store.
2. Restore PostgreSQL, including source offsets, analysis claims, and migration
   and deployment ledgers. Restore the referenced raw and context objects.
3. Configure the matching image against the restored stores. Run `db-status`
   and `db-check`. If upgrading that recovery point, run `migrate` while the
   analyzer remains stopped.
4. Run `pig-trace-analyzer rebuild-canonical-objects`. This queues reconstruction
   from the database's canonical snapshots in repeatable batches. It preserves
   upload offsets and analysis history and never deletes bucket objects.
5. Start the analyzer. Wait for readiness and for `db-status` to report zero
   pending objects. Verify a restored trace's raw and context objects, then
   upload a new trace and confirm analysis and Dashboard acknowledgement.
6. Resume the supervisor and GitOps after validation. Record the recovery points,
   image digest, and validation evidence.

New canonical objects use content-specific keys. A delayed write cannot replace
a newer version, and restoration can retain extra objects safely. Restoring an
older database can leave hosted projections or host upload offsets ahead of it;
coordinate their reconciliation with support before resuming affected hosts.
Do not reset source offsets to hide a gap or claim that a local restore rewound
hosted findings.

Do not expire current raw, context, or canonical keys with a blanket bucket
lifecycle rule. Retention must account for database references and recovery
points. PIG does not yet provide a coordinated data-deletion command. Run the
restore exercise in an isolated environment before relying on a recovery policy.
