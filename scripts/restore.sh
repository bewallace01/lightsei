#!/usr/bin/env bash
# Restore an encrypted Lightsei DB backup into a Postgres target.
#
# Usage:
#   BACKUP_PASSPHRASE='...' scripts/restore.sh <backup.sql.gz.enc> <target-db-url>
#
# Example (local docker):
#   docker run -d --name pg-restore -e POSTGRES_PASSWORD=x -p 5433:5432 postgres:18-alpine
#   BACKUP_PASSPHRASE=$(cat ~/lightsei-backup-passphrase.txt) \
#     scripts/restore.sh ./lightsei-20260426_090000.sql.gz.enc \
#       postgresql://postgres:x@localhost:5433/postgres
#
# The script does NOT touch the prod DB. You point it at a scratch Postgres,
# verify the data, then if you're rebuilding production you'd swap.

set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <backup.sql.gz.enc> <target-db-url>" >&2
  exit 2
fi

INPUT="$1"
TARGET_URL="$2"

if [ -z "${BACKUP_PASSPHRASE:-}" ]; then
  echo "set BACKUP_PASSPHRASE in env" >&2
  exit 2
fi

if [ ! -f "$INPUT" ]; then
  echo "no such file: $INPUT" >&2
  exit 2
fi

echo "decrypting + restoring $INPUT into $TARGET_URL..."
openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_PASSPHRASE -in "$INPUT" \
  | gunzip \
  | psql "$TARGET_URL"

echo
echo "restore complete. Smoke checks:"
psql "$TARGET_URL" -c "SELECT version_num FROM alembic_version" || true
psql "$TARGET_URL" -c "SELECT count(*) AS workspaces FROM workspaces" || true
psql "$TARGET_URL" -c "SELECT count(*) AS users FROM users" || true
psql "$TARGET_URL" -c "SELECT count(*) AS runs FROM runs" || true
psql "$TARGET_URL" -c "SELECT count(*) AS messages FROM thread_messages" || true
