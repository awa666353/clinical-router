# Hosted deployment runbook (Supabase + one resident container host)

Companion to [`SUPABASE_MIGRATION_GUIDE.md`](SUPABASE_MIGRATION_GUIDE.md) for
work packages W9/W10. This is a runbook only: it does not claim that any cloud
resource or DNS record exists.

> **Confirmation required (guide §0.3).** Creating paid Supabase projects,
> running migrations on the production project, importing existing data,
> switching DNS, and decommissioning/deleting the old RDS/ECS resources all
> require **explicit user confirmation before execution**. This document only
> describes the steps.

## 1. Prerequisites (guide §7)

- Supabase **staging** project (Free) and **production** project (Pro), region
  per decision D2 (`ap-southeast-1` or Tokyo unless the user chooses otherwise).
  Generate the database password with `openssl rand -base64 32` and hand it to
  the user's password manager; never store it in Git.
- Supabase Auth: disable "Allow new users to sign up", set Site URL to
  `https://collector.<domain>`, enable password-strength requirements.
- Supabase Data API: confirm the `pilot` schema is **not** in Exposed schemas
  (an empty Exposed-schema list is fine; the browser never talks to Supabase).
- Supabase Storage: create the private bucket `pilot-private`
  (`exports/`, `doctor_rounds/`, `backups/` prefixes). No Storage policies are
  created; only the server-side secret key is used.
- A container host with Docker + Docker Compose, a public IPv4 address, ports
  80/443 open, a single resident instance (no scale-to-zero), and outbound TLS
  to Supabase and the LLM endpoint. Platform choice is decision D3 (HK/SG host
  with Caddy, or Render/Fly.io/Railway with platform TLS).
- Migrations applied and verified before first application start: follow
  `supabase/README.md` (created by W2/W3). If that file is not present in the
  working tree yet, use §5 W2/W3 of the migration guide as the source of truth.

## 2. Environment file

- Location on the host: **`/etc/laya/collector.env`**.
- Create it from `deploy/env.supabase.example`, fill every placeholder, then:
  `chown root:root /etc/laya/collector.env && chmod 600 /etc/laya/collector.env`.
- This file is read only by the `api` and `worker` services and is never
  committed. `SUPABASE_SECRET_KEY` is server-only; Caddy never receives this
  file. The host's cron-only backup credentials live in a separate root-owned
  `/etc/laya/backup.env` (see §5).

## 3. Deploy api + worker + Caddy

Run from the `web_collector` directory:

```bash
sudo cp deploy/env.supabase.example /etc/laya/collector.env  # edit, then chown root:root + chmod 600
cp deploy/Caddyfile.example deploy/Caddyfile              # replace collector.<domain>
docker compose -f deploy/compose.supabase.yml up -d --build
docker compose -f deploy/compose.supabase.yml ps
docker compose -f deploy/compose.supabase.yml logs -f api worker
```

- `api` listens on the compose network only (`expose: 8080`, no host port).
  Never publish 8080 to the internet; Caddy is the only public entry point.
- **Exactly one `worker` instance** (guide W6.6). Do not add replicas.
- First start requires the domain's DNS A/AAAA record to resolve to this host
  so Caddy can finish its automatic TLS certificate order. Verify
  `https://collector.<domain>/api/health` returns `{"status":"ok"}` and that
  the logs contain no patient tokens or secrets.
- Alternative (no Caddy): deploy the same image twice on Render/Fly.io/Railway
  (web service + one worker), use platform TLS and platform secrets, and keep
  scale-to-zero disabled. See the bottom of `deploy/Caddyfile.example`.

## 4. DNS switch and rollback (guide §10)

1. **24 hours before the switch**, lower the production DNS TTL to `300` s.
2. Freeze writes on the old deployment (stop old api/worker or show a
   read-only maintenance page); record the freeze time.
3. Migrate/verify per guide §8 (empty-DB start needs no migration). All
   row-count and hash checks must pass and the restore drill (§5) must have
   completed before switching.
4. Point DNS at the new host. This is a user-confirmed change.
5. For one hour after the switch: run one end-to-end pilot flow, check
   `question_jobs` is not accumulating, and confirm `audit_events` keeps
   receiving writes.
6. **Rollback** if verification fails, authz tests fail, or writes fail within
   that hour: point DNS back to the old deployment and unfreeze the old
   database. Writes made on the new database after the switch are **not**
   automatically back-filled; assess them manually. Keep the old RDS/ECS for
   at least 14 days plus a final snapshot; disabling or deleting it requires
   explicit user confirmation.

## 5. Weekly encrypted backup (W10)

- `scripts/backup_postgres.sh` runs `pg_dump --schema=pilot --format=custom`,
  encrypts with `age` (preferred) or `gpg`, uploads to
  `pilot-private/backups/`, and keeps the newest 8 objects.
- Put `BACKUP_DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, and
  `BACKUP_AGE_RECIPIENT`/`BACKUP_GPG_RECIPIENT` in a root-owned
  `/etc/laya/backup.env` (`chmod 600`). Never echo or commit these values.
- Recommended cron entry (weekly, Sunday 03:17):

```cron
17 3 * * 0 set -a; . /etc/laya/backup.env; set +a; /opt/laya-web-collector/scripts/backup_postgres.sh >>/var/log/laya/backup.log 2>&1
```

- Before go-live, perform the guide W10.3 restore drill into a temporary
  project/local Postgres and compare row counts and hashes. Deleting the
  temporary project requires user confirmation.
