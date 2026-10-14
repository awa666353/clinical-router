#!/usr/bin/env bash
#
# backup_postgres.sh -- weekly encrypted logical backup of the pilot schema (W10.1)
#
# Pipeline:
#   1. pg_dump only the `pilot` schema into a private mktemp directory.
#   2. Encrypt that dump with age (preferred) or gpg.
#   3. Upload <timestamp>.dump.age|gpg to
#      ${SUPABASE_URL}/storage/v1/object/${STORAGE_BUCKET:-pilot-private}/backups/
#      using the server-only secret key.
#   4. Keep the newest 8 (configurable) backups; delete older ones.
#   5. Always wipe the local plaintext/encrypted files on exit via trap.
#
# Required environment (never commit these, never echo them):
#   BACKUP_DATABASE_URL    libpq URL for pg_dump (plain postgresql://, no
#                          SQLAlchemy "+psycopg" driver suffix); the password is
#                          a secret.  Can be the session-pooler connection of a
#                          role that owns/reads the pilot schema.
#   SUPABASE_URL           https://<project_ref>.supabase.co
#   SUPABASE_SECRET_KEY    server-only secret / service_role key
#                          (SUPABASE_SERVICE_ROLE_KEY is an accepted fallback)
#   BACKUP_AGE_RECIPIENT   age public key, used when the age binary is present
#   BACKUP_GPG_RECIPIENT   gpg recipient key id / fingerprint, used otherwise
#
# Optional:
#   STORAGE_BUCKET         default: pilot-private
#   BACKUP_RETENTION       default: 8
#
# Dependencies: pg_dump, curl, and age or gpg.  An unencrypted dump is never
# uploaded or written outside the trap-cleaned mktemp directory.
#
# Cron example -- weekly, Sunday 03:17, environment from a root-owned 600 file:
#   # /etc/laya/backup.env contains the variables above
#   17 3 * * 0 set -a; . /etc/laya/backup.env; set +a; /opt/laya-web-collector/scripts/backup_postgres.sh >>/var/log/laya/backup.log 2>&1
#
# Restore pointer: download a *.dump.age|gpg object, decrypt it, then
#   pg_restore --no-owner --no-privileges -d "$RESTORE_DATABASE_URL" pilot.dump
# (guide W10.3 describes the temporary-project restore drill).

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: backup_postgres.sh [--help]

Creates one encrypted logical backup of the `pilot` schema and uploads it to
Supabase Storage under backups/, then deletes all but the newest 8 backups.

Required environment:
  BACKUP_DATABASE_URL    libpq URL passed to pg_dump (no +psycopg suffix)
  SUPABASE_URL           https://<project_ref>.supabase.co
  SUPABASE_SECRET_KEY    server-only secret key
                         (or SUPABASE_SERVICE_ROLE_KEY as fallback)
  BACKUP_AGE_RECIPIENT   age recipient, used if age is installed
  BACKUP_GPG_RECIPIENT   gpg recipient, used if age is not configured

Optional environment:
  STORAGE_BUCKET         default pilot-private
  BACKUP_RETENTION       default 8

This script never prints BACKUP_DATABASE_URL or the storage secret.
EOF
}

die() {
    printf 'backup_postgres.sh: %s\n' "$*" >&2
    exit 1
}

case "${1:-}" in
    -h|--help)
        usage
        exit 0
        ;;
    "")
        ;;
    *)
        usage >&2
        die "unknown argument: $1"
        ;;
esac

if [[ -z "${BACKUP_DATABASE_URL:-}" ]]; then
    die "BACKUP_DATABASE_URL is not set (provide a libpq URL without the +psycopg driver suffix)"
fi
if [[ -z "${SUPABASE_URL:-}" ]]; then
    die "SUPABASE_URL is not set"
fi
supabase_url="${SUPABASE_URL%/}"
storage_bucket="${STORAGE_BUCKET:-pilot-private}"
retention="${BACKUP_RETENTION:-8}"
if ! [[ "$retention" =~ ^[0-9]+$ ]]; then
    die "BACKUP_RETENTION must be a positive integer, got: ${retention}"
fi
retention=$((10#$retention))
if (( retention < 1 )); then
    die "BACKUP_RETENTION must be a positive integer, got: ${retention}"
fi

storage_secret="${SUPABASE_SECRET_KEY:-${SUPABASE_SERVICE_ROLE_KEY:-}}"
if [[ -z "$storage_secret" ]]; then
    die "SUPABASE_SECRET_KEY (or SUPABASE_SERVICE_ROLE_KEY) must be set for Storage upload"
fi

command -v pg_dump >/dev/null 2>&1 || die "pg_dump is required but was not found on PATH"
command -v curl >/dev/null 2>&1 || die "curl is required but was not found on PATH"

encrypt_tool=""
if command -v age >/dev/null 2>&1 && [[ -n "${BACKUP_AGE_RECIPIENT:-}" ]]; then
    encrypt_tool="age"
elif command -v gpg >/dev/null 2>&1 && [[ -n "${BACKUP_GPG_RECIPIENT:-}" ]]; then
    encrypt_tool="gpg"
else
    die "no usable encryption backend: install age and set BACKUP_AGE_RECIPIENT, or install gpg and set BACKUP_GPG_RECIPIENT; refusing to handle an unencrypted dump"
fi

umask 077
workdir="$(mktemp -d "${TMPDIR:-/tmp}/laya-pg-backup.XXXXXX")"
dump_file="${workdir}/pilot.dump"
cleanup() {
    rm -rf -- "$workdir"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "backup_postgres.sh: dumping schema 'pilot' ..."
# --no-password keeps cron from ever blocking on an interactive prompt.
pg_dump \
    --no-password \
    --schema=pilot \
    --format=custom \
    --no-owner \
    --no-privileges \
    -f "$dump_file" \
    "$BACKUP_DATABASE_URL"

if [[ "$encrypt_tool" == "age" ]]; then
    encrypted_file="${workdir}/pilot.dump.age"
    encrypted_ext="age"
    age -r "$BACKUP_AGE_RECIPIENT" -o "$encrypted_file" "$dump_file"
else
    encrypted_file="${workdir}/pilot.dump.gpg"
    encrypted_ext="gpg"
    # --trust-model always avoids a WoT prompt under cron; verify the
    # recipient key out-of-band before adding it here.
    gpg --batch --no-tty --yes --trust-model always \
        --encrypt --recipient "$BACKUP_GPG_RECIPIENT" \
        --output "$encrypted_file" "$dump_file"
fi

[[ -s "$encrypted_file" ]] || die "encryption produced an empty file; aborting before upload"
rm -f -- "$dump_file"  # plaintext does not outlive local encryption

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
remote_name="backups/${timestamp}.dump.${encrypted_ext}"
upload_url="${supabase_url}/storage/v1/object/${storage_bucket}/${remote_name}"

# The secret key is fed to curl through --config - (stdin) instead of argv, so
# it never shows up in `ps`.  Supabase keys are URL/JWT-safe and contain no
# characters that need escaping inside the quoted config values.
curl_api() {
    local method="$1"
    shift
    printf 'header = "Authorization: Bearer %s"\nheader = "apikey: %s"\n' \
        "$storage_secret" "$storage_secret" \
        | curl --config - --silent --show-error --fail-with-body --request "$method" "$@"
}

echo "backup_postgres.sh: uploading ${remote_name} ..."
curl_api POST \
    --header "Content-Type: application/octet-stream" \
    --header "x-upsert: false" \
    --data-binary "@${encrypted_file}" \
    "$upload_url" >/dev/null

# --- Retention: keep the newest N backups --------------------------------
list_response="$(curl_api POST \
    --header "Content-Type: application/json" \
    --data '{"prefix":"backups","limit":1000}' \
    "${supabase_url}/storage/v1/object/list/${storage_bucket}")"

# The list API may return names with or without the "backups/" prefix; the
# normalization below (plus the final suffix filter) makes deletion safe to
# run against either shape.  Our timestamps sort lexicographically.
mapfile -t backup_names < <(
    printf '%s' "$list_response" \
        | grep -oE '"name"[[:space:]]*:[[:space:]]*"[^"]+"' \
        | sed -E 's/.*"name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/' \
        | while IFS= read -r name; do
              case "$name" in
                  backups/*) printf '%s\n' "$name" ;;
                  */*) : ;;  # ignore unexpected nested keys
                  *) printf 'backups/%s\n' "$name" ;;
              esac
          done \
        | grep -E '\.dump\.(age|gpg)$' \
        | sort -r
)

if (( ${#backup_names[@]} > retention )); then
    for name in "${backup_names[@]:retention}"; do
        echo "backup_postgres.sh: deleting old backup ${name}"
        curl_api DELETE "${supabase_url}/storage/v1/object/${storage_bucket}/${name}" >/dev/null
    done
fi

echo "backup_postgres.sh: done; uploaded ${remote_name}, kept newest ${retention} encrypted backup(s)"
