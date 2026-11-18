#!/usr/bin/env bash
# Dump the knowledge_agent database from the compose "db" service.
#
#   scripts/backup_db.sh [backup_dir] [retention_days]
#
# Writes <backup_dir>/knowledge_agent_<UTC timestamp>.dump (pg_dump custom
# format, compressed) and deletes dumps older than retention_days (default 14).
# Restore with scripts/restore_db.sh. Schedule it with cron, e.g.:
#   30 3 * * * cd /path/to/knowledge-agent && scripts/backup_db.sh >> backups/backup.log 2>&1
set -euo pipefail

BACKUP_DIR="${1:-backups}"
RETENTION_DAYS="${2:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="${BACKUP_DIR}/knowledge_agent_${STAMP}.dump"

mkdir -p "${BACKUP_DIR}"
umask 077  # dumps contain document text

docker compose exec -T db pg_dump -U knowledge_agent -d knowledge_agent --format=custom > "${TARGET}.partial"
# Only a complete dump gets the real name, so retention never keeps a truncated file in place of a good one.
mv "${TARGET}.partial" "${TARGET}"
echo "backup written: ${TARGET} ($(du -h "${TARGET}" | cut -f1))"

find "${BACKUP_DIR}" -maxdepth 1 -name 'knowledge_agent_*.dump' -mtime "+${RETENTION_DAYS}" -print -delete
