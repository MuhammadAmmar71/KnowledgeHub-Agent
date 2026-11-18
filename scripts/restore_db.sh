#!/usr/bin/env bash
# Restore a dump made by scripts/backup_db.sh into the compose "db" service.
#
#   scripts/restore_db.sh backups/knowledge_agent_<stamp>.dump
#
# DESTRUCTIVE: replaces the current contents of the knowledge_agent
# database. Stop the app first (docker compose stop app) so no sync writes
# during the restore.
set -euo pipefail

DUMP="${1:?usage: scripts/restore_db.sh <dump file>}"
[ -f "${DUMP}" ] || { echo "no such file: ${DUMP}" >&2; exit 1; }

read -r -p "Replace the knowledge_agent database with ${DUMP}? [y/N] " answer
[ "${answer}" = "y" ] || { echo "aborted"; exit 1; }

docker compose exec -T db pg_restore -U knowledge_agent -d knowledge_agent \
    --clean --if-exists --no-owner --exit-on-error < "${DUMP}"
echo "restored ${DUMP}"
