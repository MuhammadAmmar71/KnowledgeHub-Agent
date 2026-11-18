# How to use this project locally

Set up the project on a new machine and use the chat bubble on the Knowledge Hub
against your local backend (Ubuntu/Debian Linux).

---

## 1. Prerequisites

Install the tools:

```bash
sudo apt update
sudo apt install -y git python3.12 python3.12-venv tesseract-ocr poppler-utils libnss3-tools
# Docker: https://docs.docker.com/engine/install/ubuntu/ then:
sudo usermod -aG docker $USER      # log out and back in afterwards

curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
source ~/.bashrc
nvm install 22
```

Ask the project owner for these secrets (never commit them):
`TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET`, `SHAREPOINT_SITE_ID`, `SHAREPOINT_DRIVE_ID`,
`AZURE_OPENAI_API_KEY`. You also need a Microsoft 365 account with access to
`https://auximotechgmbh.sharepoint.com/sites/AuximoKnowledgeHub`.

---

## 2. Backend setup (one time)

```bash
git clone <repository-url> knowledge-agent
cd knowledge-agent

python3.12 -m venv venv
# CPU-only torch FIRST, otherwise pip downloads several GB of CUDA wheels
venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
venv/bin/pip install -r requirements.txt

cp .env.example .env
```

Edit `.env`:

```ini
TENANT_ID=...
CLIENT_ID=...
CLIENT_SECRET=...
SHAREPOINT_SITE_ID=...
SHAREPOINT_DRIVE_ID=...
AZURE_OPENAI_API_KEY=...

# Leave both EMPTY for local chat-bubble use (dev mode, no auth)
API_KEY=
CHAT_AUTH_AUDIENCE=
```

> ⚠️ Dev mode has no authentication. Never expose it beyond `localhost`.

Start the database and create the tables:

```bash
docker compose up -d db
venv/bin/alembic upgrade head
```

---

## 3. Run the backend

```bash
venv/bin/uvicorn app.main:app --reload --port 8000
```

Keep it running. Check it in a second terminal:

```bash
curl localhost:8000/health/graph
```

---

## 4. Load the SharePoint content (first time only)

```bash
curl -X POST localhost:8000/sync          # document library (full walk on first run)
curl -X POST localhost:8000/sync/pages    # Site Pages
curl localhost:8000/status                # repeat until the runs show "success"
```

Embeddings are created automatically after each sync. The models download on
first use (~130 MB).

**Faster alternative:** copy the data from a machine that already has it.
Run `scripts/backup_db.sh` there, copy the `.dump` file over, stop uvicorn, then
run `scripts/restore_db.sh backups/<file>.dump`.

---

## 5. Run the chat bubble

One time:

```bash
cd sharepoint-chat
nvm use
npm install
```

Each time (with the backend running):

```bash
cd sharepoint-chat
nvm use
npm start
```

1. Open the **debug URL** that `npm start` prints.
2. Sign in to SharePoint if asked, then click **Load debug scripts**.
3. Click the 💬 bubble in the bottom-right corner and ask a question.

**First time only:** the browser doesn't trust the dev certificate on Linux.
Open `https://localhost:4321/temp/build/manifests.js` and click **Advanced → Proceed**,
or trust it permanently and restart the browser:

```bash
certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n rushstack-spfx -i ~/.rushstack/rushstack-ca.pem
```

If Chrome asks to access devices on your local network, click **Allow**.

---

## 6. Daily routine

```bash
docker compose up -d db
venv/bin/uvicorn app.main:app --reload --port 8000     # terminal 1
curl -X POST localhost:8000/sync                       # terminal 2: pull SharePoint changes
cd sharepoint-chat && nvm use && npm start             # terminal 3: open the debug URL
```

---

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `nvm: command not found` | Add `export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"` to `~/.bashrc`, then `source ~/.bashrc` |
| `connection refused` on port 5432 | `docker compose up -d db` |
| `/health/graph` fails | Check the IDs and secret in `.env` |
| Chat returns no useful answers | The database is empty. Run step 4 |
| Chat error 401 | `API_KEY` or `CHAT_AUTH_AUDIENCE` is set. Empty both and restart uvicorn |
| Chat error 503 | `AZURE_OPENAI_API_KEY` is missing |
| `Error loading debug manifests` | Trust the dev certificate (step 5) |
| "Couldn't reach the chat service" | The backend isn't running on port 8000, or the local-network prompt was blocked |
| Bubble doesn't appear | Use the exact URL from `npm start` and click **Load debug scripts** |
