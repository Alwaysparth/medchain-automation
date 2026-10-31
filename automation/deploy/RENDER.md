# Deploy to Render (free plan, demo) — Member 2

```
Internet ─HTTPS─> medchain-n8n ──HTTPS + token──> medchain-ai-worker ──> MongoDB Atlas (GridFS: files)
                       │                                                     ▲
                       └──────────HTTPS + token──> medchain-mock-api ────────┘ (mock state)
                       └── medchain-n8n-db (Render Postgres: n8n's workflows, users, executions)
```

Render builds everything from **`render.yaml`** in the root of your GitHub repo. Time: ~30 min (first builds are slow).

**Free plan limits (fine for a hackathon demo):** services sleep after ~15 min idle (first request then takes ~1 min) · 512 MB RAM each · free Postgres is small and expires after a while (n8n data only; workflows are also in Git) · `/tmp` files (AI traces until Step 6) are lost when a service restarts.

---

## 1. Push the code to your GitHub repo

From the repo root (the folder that contains `contracts/`, `automation/`, `render.yaml`):
```powershell
git init
git add .
git status                 # CHECK: no ".env" in the list (only .env.example). Stop if .env appears.
git commit -m "MedChain automation: pipeline + Render blueprint"
```
On github.com: **New repository** → name `medchain-automation` → **Private** → Create (no README). Then:
```powershell
git branch -M main
git remote add origin https://github.com/<your-username>/medchain-automation.git
git push -u origin main
```

## 2. MongoDB Atlas (file storage)

In your Atlas project (cloud.mongodb.com):
1. **Database Access → Add New Database User**: username `medchain-files`, **Autogenerate** a password (copy it), role **Read and write to any database** (or specifically `medchain_files`).
2. **Network Access → Add IP Address → Allow access from anywhere (0.0.0.0/0)**. Render's free services have no fixed IP, so this is needed; the strong password protects it. Only FAKE demo data goes here.
3. **Database → Connect → Drivers** → copy the `mongodb+srv://...` string and put the password in it:
   `mongodb+srv://medchain-files:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority`

Files go into database `medchain_files` (separate from Member 1's timeline data); the free 512 MB tier is plenty.

## 3. Create the Blueprint on Render

1. dashboard.render.com → **New → Blueprint** → connect GitHub → pick `medchain-automation` → branch `main`.
2. Render shows the resources (3 web services, 1 Postgres, 1 env group) and asks for the `sync: false` values. Service URLs are `https://<service-name>.onrender.com`; use these:

| Asked for | Value |
|---|---|
| `GEMINI_API_KEY` (ai-worker) | a **fresh** Gemini key |
| `MONGODB_URI` (ai-worker **and** mock-api) | the Atlas string from step 2 |
| `WEBHOOK_URL` (n8n) | `https://medchain-n8n.onrender.com/` |
| `AI_WORKER_URL` (n8n) | `https://medchain-ai-worker.onrender.com` |
| `MEDCHAIN_API_URL` (n8n) | `https://medchain-mock-api.onrender.com` |
| `SOLANA_CLIENT_URL` (n8n) | `https://medchain-mock-api.onrender.com/solana` |
| `N8N_UPLOAD_WEBHOOK_URL` (mock-api) | `https://medchain-n8n.onrender.com/webhook/upload-pipeline` |

3. **Apply**. First builds take 5–10 min. When done, open each service and check its real URL at the top. If Render added a suffix (name already taken, e.g. `medchain-n8n-ab12.onrender.com`), fix the values above under that service's **Environment** tab → **Save, rebuild and deploy**.
4. Open **medchain-n8n → Environment**, copy `N8N_ENCRYPTION_KEY` into a password manager (losing it makes n8n credentials unreadable).

## 4. Check health

- `https://medchain-ai-worker.onrender.com/health` → `"storage": true, "gemini_key_set": true, "storage_backend": "mongodb"`
- `https://medchain-mock-api.onrender.com/health` → `"ok": true`
- `https://medchain-n8n.onrender.com/healthz` → `{"status":"ok"}`

`storage: false` → check `MONGODB_URI` (password, special characters must be URL-encoded) and Atlas Network Access.

## 5. Set up n8n — do this IMMEDIATELY

> The first person to open a fresh n8n becomes its owner.

1. Open `https://medchain-n8n.onrender.com` → create the owner account with a **strong password**.
2. **Create workflow → ⋯ → Import from File** → `automation/n8n/workflows/upload-pipeline.json` → **Save** → **Active / Publish**.

## 6. Test

**Webhook is public, HTTPS, token-protected:**
```powershell
curl.exe -X POST https://medchain-n8n.onrender.com/webhook/upload-pipeline -H "Content-Type: application/json" -d "{}"
```
→ `{"accepted":false,"errors":[...]}`

**Full pipeline:** get `DEMO_TOKEN` from **medchain-mock-api → Environment**. Then:
```powershell
curl.exe -F "file=@automation/ai/samples/fake_lab_report_01.png" -F "document_type=unknown" "https://medchain-mock-api.onrender.com/demo/upload?token=<DEMO_TOKEN>"
```
(or `https://medchain-mock-api.onrender.com/docs` → `POST /demo/upload` → fill `token`). After ~30 s–2 min:
`https://medchain-mock-api.onrender.com/demo/entries?token=<DEMO_TOKEN>` → `completed`, *"Lab report: 6 of 11 values flagged…"*. In Atlas → Browse Collections → `medchain_files` you'll see `documents.files` / `documents.chunks`.

## Before a demo

Open the three health URLs 1–2 minutes before presenting, so nothing is asleep. The first upload after waking can take ~1–2 min; later ones ~20 s.

## Updating

`git push` → Render redeploys automatically. If a workflow JSON changed, import it again in n8n and re-activate.

## When the real services exist

- Member 1's API: set `MEDCHAIN_API_URL` on medchain-n8n to the real API, and the real API needs `N8N_WEBHOOK_TOKEN` + the n8n webhook URL.
- Member 3's Solana client: set `SOLANA_CLIENT_URL`.
- Then delete `medchain-mock-api` (remove it from `render.yaml` and push).
