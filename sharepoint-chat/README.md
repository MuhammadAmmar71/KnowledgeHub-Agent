# Knowledge Hub Assistant (SharePoint chat bubble)

SPFx 1.23 Application Customizer that adds a floating 💬 chat bubble to every page
of the Auximo Knowledge Hub. It calls the Knowledge Agent backend's `POST /chat`
(see the main README, "Chat API") and shows answers with linked sources.

It is UI only: no secrets, no agent logic. All intelligence and all credentials
live in the backend.

## Requirements

- Node 22 (`nvm use`; see `.nvmrc`)
- `npm install`

```bash
npm run build      # lint + tests + package -> sharepoint/solution/knowledge-agent-sp-chat.sppkg
npm run release    # same, but FAILS while deployment placeholders are still set
npm start          # local dev server (https://localhost:4321) + debug URL
```

## Configuration (component properties)

| Property | Example | Purpose |
|---|---|---|
| `apiUrl` | `https://<backend-host>/chat` | Backend chat endpoint |
| `apiResource` | `api://<client-id>` | Entra resource of the backend. When set, requests carry the signed-in user's token (`AadHttpClient`). Leave **unset only for local debugging**. |
| `title` | `Knowledge Hub Assistant` | Panel header |

They're set in three places:
- `config/serve.json`: local debugging. Points to `http://localhost:8000/chat` with no `apiResource`.
- `sharepoint/assets/elements.xml` and `ClientSideInstance.xml`: the deployed package (HTML-escaped JSON).

## Local debugging (before anything is deployed)

1. Run the backend in **dev mode** on port 8000: `CHAT_AUTH_AUDIENCE` and `API_KEY` both
   unset, so it accepts requests without a token. Never expose a dev-mode backend
   beyond your machine.
   ```bash
   cd .. && venv/bin/uvicorn app.main:app --port 8000
   ```
2. `npm start`, then open the debug URL it prints (the Knowledge Hub site with
   `?loadSPFX=true&debugManifestsFile=…&customActions=…`) and choose **Load debug scripts**.
3. If the bubble doesn't appear or the browser blocks the request, see "Local
   development gotchas" in `../SHAREPOINT_AGENT_INTEGRATION.md` (dev certificate trust,
   Chrome's local-network prompt).

The backend's CORS already allows `https://auximotechgmbh.sharepoint.com`, and
private-network access for `localhost`.

## Deploying to the Knowledge Hub

Before the first deployment:

1. **Entra app registration → Expose an API:** Application ID URI `api://<client-id>`,
   scope `user_impersonation`. On the backend, set `CHAT_AUTH_AUDIENCE=api://<client-id>`.
   No app role is needed: every user of the tenant may chat (main README, "Chat API").
2. **Replace the placeholders** (`npm run release` refuses to build until you do):
   - `config/package-solution.json` → `webApiPermissionRequests[0].resource`: the app
     registration's **display name** (exactly as shown in Entra).
   - `sharepoint/assets/elements.xml` and `ClientSideInstance.xml` → `apiUrl`: the
     backend's public **HTTPS** URL (after the Azure deployment).
3. Nothing else needs changing for CORS: the browser calls the backend directly, and the
   backend's `CORS_ALLOWED_ORIGINS` already lists the SharePoint origin.

Then:

1. `npm run release`.
2. Upload `sharepoint/solution/knowledge-agent-sp-chat.sppkg` to the **App Catalog**.
3. **SharePoint admin center → Advanced → API access:** approve the pending request for
   `<app registration> / user_impersonation`. Without this, `AadHttpClient` can't get
   tokens and the bubble shows a sign-in error.
4. On the Knowledge Hub site: **Settings → Add an app →** Knowledge Hub Assistant.

For each redeploy, bump `solution.version` and the feature `version` in
`config/package-solution.json`.

## Behaviour

- Conversations continue across page navigation and reloads within the tab
  (`sessionStorage`). **↺** starts a new chat. If the backend no longer knows a
  conversation, the bubble silently starts a new one with the same question.
- Replies are rendered from a small Markdown subset by building DOM nodes. `innerHTML`
  is never used for reply text, and only `https:` links are created, so a reply can't
  inject markup or script links (`src/extensions/chatBot/markdown.ts`, with tests).
- `[n]` markers link to the cited source, and the cited sources are listed under each answer.
- Errors are mapped to plain messages (expired sign-in, service unavailable, and so on);
  details go to the browser console only.
