# AnyAiCam AWS-readiness guide

No AWS resources are created by this project. The following describes a later deployment path only.

## Current local mode

Keep `ANYAICAM_ENV=development`, SQLite, local storage, preview email, and HTTP/Tailscale. Docker continues mounting `/app/recordings`, which contains the database, media, email previews, and storage objects.

## Staging sequence

1. Create a private PostgreSQL database and apply the built-in migrations by starting one portal instance.
2. Configure an S3-compatible private bucket with blocked public access, encryption, lifecycle policies, and narrowly scoped application credentials.
3. Configure HTTPS at the load balancer or reverse proxy and preserve `X-Forwarded-Proto`.
4. Set staging URLs, rotated application secrets, secure cookies, and CSRF protection.
5. Keep email in preview mode until invitation and reset templates are approved.
6. Run the complete test suite and an appliance activation/check-in test against staging.

## Production preparation

- Store database, SMTP, storage, and application secrets in a managed secret service rather than source files.
- Use at least two application secrets during rotation: current first, previous second. Remove the previous value after all sessions expire.
- Use a private database network, encrypted connections, backups, point-in-time recovery, and restricted security groups.
- Keep storage private and use short-lived presigned URLs.
- Send structured logs to the selected log service and create alerts for authentication lockouts, appliance revocations, repeated failures, and low storage.
- Run migrations as a controlled deployment step before increasing application replicas.
- Configure retention policies and export audit logs before deleting expired records.

## Database compatibility

`ANYAICAM_DATABASE_BACKEND=sqlite` remains the local default. PostgreSQL uses `psycopg` and the same migration versions with SQL placeholder and conflict translation. Test migrations against a disposable staging database before production.

## Media migration

The storage abstraction covers snapshots, thumbnails, clips, documents, and partner materials. Existing local recordings are not automatically copied or deleted. A later migration job should copy, checksum, verify, and only then retire local objects.

## Public website and Partner Portal session

The public website and Partner Portal can share a signed-in session when they use the same parent domain. Set `ANYAICAM_COOKIE_DOMAIN` to that parent domain (for example `.example.com`), keep both services on HTTPS, and route the partner session, login, logout, and password-recovery endpoints to the portal application. Leave the cookie domain blank in local development.

`partner.html` is the public entry point. It exposes application and sign-in forms only. Wholesale pricing, commissions, customer records, credentials, and portal data remain behind role-protected endpoints.
## Production and staging domains

The intended production hosts are `https://anyaicam.com` and `https://portal.anyaicam.com`. Staging uses `https://staging.anyaicam.com` and `https://portal-staging.anyaicam.com`. Every public URL remains configurable in the environment files. Never reuse staging databases, buckets, cookie secrets, SMTP credentials, or application secrets in production.

The supplied `Caddyfile` terminates TLS automatically, redirects HTTP to HTTPS, removes the Server header, forwards proxy and WebSocket headers, limits request bodies to 250 MB, and checks `/health/live`. Set DNS before starting it. No AWS resource is created by this configuration.

Run the deployment verification from the application container:

```text
python /app/deployment_verify.py
```

It checks configuration validation, database connectivity, the selected storage backend, email mode, and public URLs without sending email or contacting cloud storage.

## Backup and restore preparation

SQLite development backup and restore:

```text
python /app/backup_portal.py backup /app/recordings/backups/portal.db
python /app/backup_portal.py restore /app/recordings/backups/portal.db
```

Stop portal writes before a restore. The restore command verifies the copied SQLite file and replaces the destination atomically.

PostgreSQL placeholders (supply credentials through the environment, never the command history):

```text
pg_dump --format=custom --file=anyaicam.dump "$ANYAICAM_DATABASE_URL"
pg_restore --clean --if-exists --dbname="$ANYAICAM_DATABASE_URL" anyaicam.dump
```
