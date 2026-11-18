# Auximo Knowledge Agent

Syncs the Auximo KnowledgeHub SharePoint site (document library + Site Pages)
through Microsoft Graph into PostgreSQL, extracts and chunks the content,
embeds it into pgvector, and answers questions over it with GPT-4o, citing
SharePoint sources.

```
SharePoint ─Graph─▶ sync (initial / delta / reconciliation / pages)
                     ─▶ extraction ─▶ chunking ─▶ embeddings (pgvector)
                                                  ─▶ hybrid search ─▶ reranking ─▶ RAG answers
```

SharePoint is the source of truth. Everything in Postgres can be rebuilt from it.
The design spec is `Auximo_Knowledge_Agent_Implementation_Spec.md`.

## Quick start (local development)

Requirements: Python 3.12, Docker, `tesseract` and `poppler` (OCR for
scanned PDFs and PNGs).

```bash
python3 -m venv venv
venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu   # first, or pip pulls CUDA wheels
venv/bin/pip install -r requirements.txt
cp .env.example .env              # fill in TENANT_ID, CLIENT_ID, CLIENT_SECRET, SHAREPOINT_SITE_ID, SHAREPOINT_DRIVE_ID
docker compose up -d db           # Postgres 16 + pgvector on 127.0.0.1:5432
venv/bin/alembic upgrade head
venv/bin/uvicorn app.main:app --reload --port 8000
```

Then:

```bash
curl localhost:8000/health/graph               # auth + site + drive reachable?
curl -X POST localhost:8000/sync               # first run: full walk, then a delta cursor
curl -X POST localhost:8000/sync/pages         # Site Pages (needs page-listing permission; see below)
curl localhost:8000/status                     # what happened
venv/bin/python -m app.cli search "release branches"
venv/bin/python -m app.cli ask "How do I deploy the Pulse dashboard?"   # needs AZURE_OPENAI_API_KEY
```

The embedding model (`BAAI/bge-small-en-v1.5`, about 130 MB) downloads on first
use and is loaded from the local HuggingFace cache after that.

## Deployment (Docker)

```bash
cp .env.example .env     # set real values, including API_KEY and POSTGRES_PASSWORD
docker compose up -d --build
docker compose logs -f app
```

- The `app` container runs `alembic upgrade head` and then starts uvicorn with one
  worker on `127.0.0.1:8000`. Put a TLS-terminating reverse proxy in front of it
  to expose it beyond the host.
- Postgres listens on loopback only. Its data lives in the `knowledge_agent_pgdata`
  volume, and the model cache lives in `knowledge_agent_hf_cache`.
- Running more than one instance is safe, because job locks are Postgres
  advisory locks. Each instance does load its own copy of the embedding model, though.

## API

Every endpoint except `GET /health` and `POST /chat` requires `X-API-Key: <API_KEY>`
(or `Authorization: Bearer <API_KEY>`) when `API_KEY` is set. `POST /chat` uses
Entra ID user tokens instead (see below).

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness check. Touches nothing. |
| `POST /chat` | Answers a question for the SharePoint chat bubble. See [Chat API](#chat-api). |
| `GET /health/graph` | Checks app-only auth and that the site and drive are reachable. |
| `GET /health/pages` | Checks whether the app is permitted to list Site Pages. |
| `POST /sync[?mode=auto\|full]` | Document-library sync. `auto` runs a delta pass, or a full walk if no cursor exists yet. `full` runs a reconciliation. Returns `409` if a library sync is already running. |
| `POST /sync/reconcile` | Full reconciliation, same as `mode=full`. |
| `POST /sync/pages` | Site Pages sync (full listing; Graph has no page delta). |
| `GET /sync/latest`, `GET /sync/{id}` | Status of a sync run. |
| `POST /embeddings/run[?reembed=all]` | Embedding stage (normally runs automatically after every sync). |
| `GET /embeddings/status` | Documents by status, plus chunks by embedding model. |
| `GET /status` | Operational overview: last successful sync per source, running syncs, cursor state, document/chunk counts, failed documents, last error. |
| `GET /metrics` | Prometheus metrics. |
| `GET /documents`, `GET /documents/{id}` | Tracked documents. |
| `/debug/*`, `/discovery/preview` | Extraction and chunking previews. Return `404` unless `ENABLE_DEBUG_ENDPOINTS=true`. |

Search and answers are also available from the CLI: `python -m app.cli search|ask|reprocess --help`.

### Chat API

`POST /chat` is the backend for the SharePoint chat bubble in `sharepoint-chat/`
(build, debug and deployment steps: `sharepoint-chat/README.md`).

```http
POST /chat
Authorization: Bearer <Entra user token for api://<CLIENT_ID>>
Content-Type: application/json

{"message": "What permissions do I need for ACR?", "conversation_id": "<optional uuid>"}
```

```json
{
  "reply": "You need the following permissions:\n- Contributor\n- Container Registry Repository Contributor [1]",
  "conversation_id": "6b7b9e88-…",
  "sources": [{"number": 1, "title": "Pushing changes on Azure.md", "url": "https://…",
               "sections": ["Required Permissions"], "source_type": "file", "cited": true}],
  "project_numbers": [],
  "project_scope_fell_back": false
}
```

- **Authentication.** With `CHAT_AUTH_AUDIENCE=api://<CLIENT_ID>` set, a valid Entra ID
  *user* token from your tenant with the `user_impersonation` scope is required (signature,
  issuer, audience, expiry and scope are all checked). The user's identity is taken from
  the token; a `user` field in the body is ignored. With it unset (development only),
  `/chat` falls back to `API_KEY` and all callers share one identity.
- **Who may chat.** Any user of the company's Microsoft 365 tenant. Everyone at Auximo may
  use the Knowledge Hub and no guest users are invited, so no further restriction is needed.
- **Optional: restrict to an app role.** If that ever changes, set `CHAT_REQUIRED_ROLE`
  (e.g. `KnowledgeHub.Chat`). Tokens without the role then get `403`, so assign the role
  *before* setting this. To set it up in Entra ID:
  1. **App registrations → the app → App roles → Create app role:** display name
     `Knowledge Hub Chat User`, allowed member types **Users/Groups**, value
     `KnowledgeHub.Chat`, enabled.
  2. **Enterprise applications → the same app → Users and groups → Add user/group:**
     choose the group that gives people access to the Knowledge Hub (for a group-connected
     site, its Microsoft 365 group), with role *Knowledge Hub Chat User*.

  Assigning a *group* needs Entra ID P1 or P2. Without it, assign users individually.
  Nested groups aren't expanded, so assign the group that directly contains the users.
  If site access is ever granted outside that group (for example by sharing with
  individuals), those people need the role too. The backend can't see SharePoint site
  permissions, only the role in the token.
- **Follow-up questions.** Pass back `conversation_id` to continue a conversation. A
  follow-up is rewritten into a standalone question from the recent turns
  (`CHAT_HISTORY_TURNS`), then answered only from freshly retrieved SharePoint excerpts.
  Earlier answers are never used as a source. Conversations belong to the user who started
  them; anyone else gets `404`.
- **Sources.** `[n]` markers in `reply` refer to `sources[].number`. Cited sources come first.
  Render `url` as links. `reply` may contain Markdown, so sanitize it before rendering as HTML.
- **CORS.** Only `CORS_ALLOWED_ORIGINS` (default `https://auximotechgmbh.sharepoint.com`) may
  call it from a browser.
- **Errors.** `401` bad or missing token, `404` unknown conversation, `422` empty or
  over-long message (`CHAT_MAX_MESSAGE_CHARS`), `502` LLM unavailable (retry), `503` chat
  not configured (`AZURE_OPENAI_API_KEY` missing).
- **Storage.** User questions and answers are stored in `conversations`/`chat_messages` to
  resolve follow-ups. Question and answer text is never written to logs.
- **Performance.** The embedding and reranker models load once at startup in the background
  (`CHAT_WARMUP_MODELS`), so answers take about 5–8 seconds, most of it spent in GPT-4o.

Retrieval runs in two stages. Hybrid search (vector similarity + Postgres
full-text, merged by rank) fetches `RERANK_CANDIDATES` chunks; a local
cross-encoder (`RERANKER_MODEL_NAME`, default `cross-encoder/ms-marco-MiniLM-L-6-v2`) then reads the question
together with each candidate and reorders them by relevance (0–1). Pass
`--no-rerank` to skip the second stage. For `ask`, chunks below
`RERANK_MIN_RELEVANCE` are dropped before the LLM sees them, and if none are
left the CLI answers "not found" without calling the LLM at all.

**Project numbers.** Any 8-digit number (e.g. `10000001`) in a chunk's text, its
heading, or its document's name or folder path is stored as a project tag. If a
question mentions a project number, search is restricted to content tagged with
it, so answers about one project never mix in another's. If nothing is tagged
with that number at all, search falls back to all content and says so. Use
`--project 10000001` (repeatable) to restrict explicitly; that never falls back.

## How syncing works

| Run type | What it does | When |
|---|---|---|
| `initial` | Recursive walk of the whole drive, then captures a delta cursor. | First sync, or when no cursor exists. |
| `delta` | Processes only Graph's change feed since the cursor. The cursor advances only after a pass with zero failures; otherwise the same batch is retried next time. | Normal daily run. |
| `reconciliation` | Same walk as `initial`, run against existing state. Repairs anything delta missed and marks tracked files that SharePoint no longer has (or that were renamed to an unsupported type) as deleted. | Every `RECONCILIATION_INTERVAL_DAYS` (default 7) on the schedule, or `POST /sync/reconcile`. |
| `pages` | Full Site Pages listing, diffed against tracked pages. | Daily on the schedule (`SYNC_PAGES_ON_SCHEDULE`), or `POST /sync/pages`. |

Unchanged content is detected by SHA-256 and never reprocessed, so reruns
are cheap and never duplicate chunks. Each item commits on its own, so one bad
file is recorded as failed (`DOWNLOAD_FAILED`, `EXTRACTION_FAILED`, and so on)
without affecting the others, and it is retried on the next run.

**Scheduler.** The API process runs one job a day at `SYNC_SCHEDULE_TIME`
(default `02:00`, in `SYNC_SCHEDULE_TIMEZONE` or server local time). The job
runs a library sync (delta or reconciliation), then pages, then embeddings. Set
`SYNC_SCHEDULE_ENABLED=false` to drive syncs externally instead. If the service
is down at the scheduled time, that run is not caught up; the next run picks up
all changes anyway.

**Locks.** At most one library sync and one page sync run at a time, across all
instances (Postgres advisory locks). The lock is released automatically if a
process dies. On startup, sync runs left `running` by a dead process are marked
`failed` ("interrupted").

**Site Pages permission.** Graph documents `Sites.Read.All` as the
least-privileged permission for listing pages, which is broader than
`Sites.Selected`. Page sync runs a capability check first and fails the run
with a clear reason rather than escalating. Check with `GET /health/pages`.

## Monitoring

`GET /status` answers the operational questions directly. `GET /metrics`
(Prometheus) exposes:

- `knowledge_agent_sync_runs_total{sync_type,status}`, `knowledge_agent_sync_duration_seconds`, `knowledge_agent_sync_items_total{outcome}`
- `knowledge_agent_graph_requests_total{status}`, `knowledge_agent_graph_throttled_total`, `knowledge_agent_graph_retries_total{reason}`
- `knowledge_agent_extraction_failures_total{extension}`, `knowledge_agent_embedding_batch_seconds`, `knowledge_agent_embedding_documents_total{outcome}`
- `knowledge_agent_retrieval_seconds`, `knowledge_agent_llm_request_seconds` (in-process only, so populated by API-side use)
- These are read from the database at scrape time: `knowledge_agent_documents{status}`, `knowledge_agent_chunks{embedded}`, `knowledge_agent_last_successful_sync_timestamp_seconds{source}`, `knowledge_agent_sync_running{source}`

Suggested alerts:

- `time() - knowledge_agent_last_successful_sync_timestamp_seconds{source="document_library"} > 36*3600`
- any `knowledge_agent_documents{status=~".*_FAILED"} > 0` lasting a day
- `increase(knowledge_agent_sync_runs_total{status="failed"}[1d]) > 0`

Prometheus scrape config with a key:
`authorization: { type: Bearer, credentials: <API_KEY> }`.

**Logs.** Logs use `key=value` text by default, or one JSON object per line
with `LOG_FORMAT=json`. Every line written during a sync run carries its
`sync_id`. A final redaction filter scrubs bearer tokens, JWTs, URL query
strings (Graph download URLs carry credentials there) and `secret=`-style
values, as a backstop; the code itself never logs them.

## Backups and recovery

```bash
scripts/backup_db.sh                 # -> backups/knowledge_agent_<UTC>.dump, deletes dumps older than 14 days
scripts/backup_db.sh /mnt/backups 30
scripts/restore_db.sh backups/knowledge_agent_<stamp>.dump   # destructive; stop the app first
```

Schedule the backup with cron (an example is in the script header) and copy the
dumps off the host. A backup saves re-download and re-embedding time; it is not
the only copy of anything, because SharePoint is canonical. Without a backup, a
full rebuild from an empty database works too:

```bash
venv/bin/alembic upgrade head
curl -X POST localhost:8000/sync          # walk, extract, chunk, embed, cursor
curl -X POST localhost:8000/sync/pages
```

Other recovery paths:

- **Extraction or chunking logic changed:** run `python -m app.cli reprocess`, then `POST /sync?mode=full` and `POST /sync/pages`.
- **Embedding model changed (same dimension):** the next embedding run re-embeds automatically. To force it, use `POST /embeddings/run?reembed=all`.
- **Embedding dimension changed:** this needs a migration for `vector(N)` (`EMBEDDING_DIMENSIONS` in `app/db/models.py`).
- **Suspected drift between SharePoint and the database:** `POST /sync/reconcile`.

## Security

- App-only Graph auth with `Sites.Selected`. The app never escalates permissions on its own.
- Secrets come only from the environment or `.env` (git-ignored; see `.env.example`). `CLIENT_SECRET` and `AZURE_OPENAI_API_KEY` are `SecretStr`, so they don't show up in reprs.
- Access tokens and temporary download URLs are never stored or logged. httpx request logging is turned off, and transport errors are reported by type only, never with their URL.
- Public endpoints return generic messages for Graph failures. Raw Graph error bodies appear only on the opt-in debug endpoints.
- The API is protected with `API_KEY` (compared in constant time). A warning is logged at startup when no key is set.
- Postgres and the app bind to loopback in compose. The container runs as a non-root user.

## Configuration

Everything is in `.env.example`, with comments. Required: `TENANT_ID`, `CLIENT_ID`,
`CLIENT_SECRET`, `SHAREPOINT_SITE_ID`, `SHAREPOINT_DRIVE_ID`. Invalid values (a bad
schedule time, an unknown timezone, a blank secret) fail at startup with a clear error.

## Tests

```bash
venv/bin/python -m pytest                 # everything
venv/bin/python -m pytest tests/unit      # no database or network needed
```

`tests/integration` covers locks, the sync run lifecycle, reconciliation and
status against real Postgres. It uses a separate `knowledge_agent_test`
database (`TEST_DATABASE_URL` to override), which it creates and migrates on
first use, and it skips itself if Postgres isn't reachable.
