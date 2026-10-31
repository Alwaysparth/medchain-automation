# MedChain n8n workflows (Member 2)

| Workflow file | Trigger | What it does |
|---|---|---|
| `workflows/upload-pipeline.json` | `POST /webhook/upload-pipeline` (`document.uploaded`) | validate → compress + store → anchor SHA-256 on Solana → Gemini extraction → deterministic flagging → save via API → AI trace → notify. Error branch marks the entry *"Processing failed, please re-upload"*. |
| `whatsapp-upload.json`, `whatsapp-qa.json` | WhatsApp | Step 7 |

The workflows call three services, all configured through environment variables (no credentials to set up in n8n):

| Env var | Local default | Real service |
|---|---|---|
| `MEDCHAIN_API_URL` | `http://mock-api:8100` | Member 1's API |
| `SOLANA_CLIENT_URL` | `http://mock-api:8100/solana` | Member 3's Solana client |
| `AI_WORKER_URL` | `http://ai-worker:8000` | this repo, `automation/ai/worker` |

## Run everything locally (Docker Desktop)

```powershell
cd automation
copy .env.example .env        # then edit .env: GEMINI_API_KEY + the two tokens
docker compose up --build -d
docker compose ps             # ai-worker, mock-api, n8n should be "running"
```

Check: http://localhost:8000/health (`gemini_key_set: true`) and http://localhost:8100/health.

## Import and activate the workflow

1. Open http://localhost:5678 and create the local owner account (first time only).
2. Import, either way:
   - UI: **Workflows → Create → ⋯ menu → Import from File** → `automation/n8n/workflows/upload-pipeline.json`, then **Save**.
   - CLI: `docker compose exec n8n n8n import:workflow --input=/workflows/upload-pipeline.json`
3. Open the workflow and switch it **Active** (newer n8n versions call this **Publish**). The production URL `/webhook/upload-pipeline` only works while active.

## Send a test upload

Browser: http://localhost:8100/docs → `POST /demo/upload` → **Try it out** → choose `automation/ai/samples/fake_lab_report_01.png`, `document_type = unknown` → **Execute**. `sent_to_n8n.status_code` should be **202**.

PowerShell alternative (from the repo root):
```powershell
curl.exe -F "file=@automation/ai/samples/fake_lab_report_01.png" -F "document_type=unknown" http://localhost:8100/demo/upload
```

Then watch:
- n8n → **Executions**: every node green.
- http://localhost:8100/demo/entries → `processing_status: completed`, summary *"Lab report: 6 of 11 values flagged…"*, a `MOCKTX…` Solana tx.
- http://localhost:8100/demo/notifications → `upload_completed`.
- Traces: `docker compose exec ai-worker cat /data/traces.jsonl`

## Test the failure paths

| Test | How | Expected |
|---|---|---|
| Bad webhook token | `curl.exe -X POST http://localhost:5678/webhook/upload-pipeline -H "Content-Type: application/json" -d "{}"` | `400`, `accepted: false` with reasons |
| Worker down | `docker compose stop ai-worker`, upload, then `docker compose start ai-worker` | entry `failed`, `user_message: "Processing failed, please re-upload"`, `upload_failed` notification |
| Solana down | `.env`: `MOCK_SOLANA_FAIL_RATE=1`, `docker compose up -d mock-api`, upload | entry still `completed`, `solana_tx: null` (verification pending) |
| Not a medical document | upload any photo (e.g. a selfie) with `document_type = unknown` | entry `failed`, code `UNSUPPORTED_DOCUMENT` |

## Changing a workflow

Edit in the n8n UI, then **⋯ → Download** and replace the JSON in `workflows/`. Run `pytest automation/n8n -q` before committing: it checks node references, wiring, URLs against the real routes, and that all JavaScript compiles.
