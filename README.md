# Auximo Knowledge Agent — Phase 1 (Foundation)

This is the **first slice only**, per the implementation spec's Phase 1 /
"Immediate Codex Task": project structure, config, MSAL app-only auth, a
reusable Graph client with pagination + retry, site/drive health checks,
recursive drive discovery, and unit tests.

**Not yet implemented (by design):** delta sync, change detection, file
processors, chunking, embeddings, pgvector, RAG, and the scheduler. Those
are Phases 2–9 in the spec and should be built on top of this once Phase 1
is verified against your real tenant.

## What's here

```
app/
  config.py              typed settings (pydantic-settings)
  auth/graph_auth.py      MSAL confidential-client app-only auth
  sharepoint/client.py    Graph HTTP client: pagination, retry/backoff, 429 handling
  sharepoint/drives.py    site/drive health checks + iterative recursive discovery
  main.py                 FastAPI app: /health, /health/graph, /discovery/preview
tests/
  unit/test_config.py      settings validation
  unit/test_graph_client.py  pagination, retry/backoff, absolute-URL handling
  unit/test_discovery.py     nested folders, pagination, unsupported-file skipping
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in TENANT_ID / CLIENT_ID / CLIENT_SECRET in .env
```

`SHAREPOINT_SITE_ID` and `SHAREPOINT_DRIVE_ID` in `.env.example` are
already pre-filled from the spec's known environment (section 3) — you
shouldn't need to look those up again, only the Entra credentials.

## Run the tests

```bash
pytest -v
```

All tests run against a mocked Graph transport (`httpx.MockTransport`) —
**no network calls are made**, so this proves the pagination, retry, and
discovery *logic* is correct without needing live credentials. I was not
able to run this suite myself in the environment I built it in (no
outbound network access to install fastapi/httpx/msal/pytest), so please
run it on your end as the actual verification step — every file did pass
a plain `python -m py_compile` syntax check, but that's not a substitute
for the real test run.

Expected: all tests in `test_config.py`, `test_graph_client.py`, and
`test_discovery.py` pass.

## Run the app and hit it against your real tenant

```bash
uvicorn app.main:app --reload --port 8000
```

Then:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/health/graph
curl http://localhost:8000/discovery/preview
```

- `/health` — liveness only, no network.
- `/health/graph` — acquires a real app-only token via MSAL, then calls
  `GET /sites/{site-id}` and `GET /drives/{drive-id}`. This is the first
  real proof that `Sites.Selected` has actually been granted **and**
  explicitly assigned at the site level (spec section 6 — registering the
  permission is not the same as granting site access).
- `/discovery/preview` — runs the full iterative recursive walk of the
  document library and returns counts only (folders, files, supported vs.
  unsupported), matching the acceptance shape in spec section 69. It does
  not print file names, paths, or any content, so it's safe to run before
  you've decided what should be indexed.

## Known permission risk (spec section 7)

Site Pages (`/sites/{id}/pages/microsoft.graph.sitePage`) may require
`Sites.Read.All` per current Graph docs, which is broader than
`Sites.Selected`. **This Phase 1 slice does not touch pages at all** —
that ambiguity is deliberately deferred to Phase 4, where a capability
test should run before any page-sync code is written, so a permission
gap there can't silently block the rest of the system.

## Assumptions made

- `SHAREPOINT_SITE_ID` and `SHAREPOINT_DRIVE_ID` are treated as stable
  configuration (per spec section 54), not something the app should
  ever resolve dynamically from the site path — they're taken as given
  from spec section 3.
- Recursive discovery is breadth-first via an explicit queue, not
  Python function recursion, so folder depth can't exhaust the call
  stack (spec section 16).
- Retry/backoff defaults (5 retries, 1s base, exponential) are
  conservative starting points per spec section 44 ("start
  conservatively and measure") — not tuned against real Graph
  throttling behavior yet.
- `GraphAuth` relies on MSAL's own in-memory token cache
  (`acquire_token_silent` then `acquire_token_for_client`) rather than
  a custom cache layer, since MSAL already handles token expiry
  correctly.

## Next step

Once `/health/graph` and `/discovery/preview` succeed against your real
tenant, Phase 2 (persistent document state + initial full sync) can be
built on top of `walk_drive()` without changing this foundation.
