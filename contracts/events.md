# MedChain — API ⇄ n8n Events Contract

**Owner:** Member 2 (AI & Automation) · **Version:** 1.1 · **Status:** DRAFT — needs sign-off from Member 1 (API) and Member 3 (Solana)

> **Changes in 1.1:** API→n8n auth is now a shared-secret header (simpler than HMAC for the prototype) · `document.uploaded` gains optional `patient_context.sex` (needed for sex-specific lab ranges) · object key layout fixed · `file` block in 2.4 gains `raw_object_key` and `thumbnail_key`.

This file lists every webhook/HTTP payload exchanged between the backend API (Member 1) and the n8n workflows (Member 2). Endpoint *paths on the API side* are proposals — the source of truth for those is `contracts/openapi.yaml` (Member 1). If they differ, update this file.

---

## 0. Conventions (apply to every event)

| Rule | Detail |
|---|---|
| Transport | HTTPS POST, `Content-Type: application/json` |
| Auth API → n8n | Header `X-MedChain-Webhook-Token: <N8N_WEBHOOK_TOKEN>` (shared secret; production should upgrade to an HMAC signature) |
| Auth n8n → API | Header `Authorization: Bearer <INTERNAL_SERVICE_TOKEN>` |
| Envelope | Every payload is wrapped in the envelope below |
| IDs | UUID v4 strings. `patient_id` is the internal UUID — **never Aadhaar or its hash** |
| Times | ISO-8601 UTC, e.g. `2026-11-01T09:30:00Z` |
| Idempotency | Receiver must ignore a repeated `event_id` |
| PHI | No raw report text, no file bytes in events. Files move only via Object Storage keys |

### Envelope

```json
{
  "event_id": "8b0c2f0e-6a1f-4d1e-9a51-1d3c9f6b7a10",
  "event_type": "document.uploaded",
  "version": "1.0",
  "occurred_at": "2026-11-01T09:30:00Z",
  "correlation_id": "c1a2...  (same across every event of one upload / one chat turn)",
  "data": { }
}
```

---

## 1. API → n8n (n8n webhooks)

### 1.1 `document.uploaded` → `POST {N8N_BASE_URL}/webhook/upload-pipeline`
Sent by the API after a provider or patient uploads a file through the web app. The API has already written the raw file to Object Storage and created a `timeline_entries` doc with `processing_status: "queued"`.

```json
{
  "entry_id": "665f1c...",
  "patient_id": "3f6c...",
  "uploaded_by": { "user_id": "a1b2...", "role": "patient | doctor | pharmacy | diagnostic_center" },
  "source": "provider | patient_uploaded",
  "document_type": "lab_report | prescription | bill | unknown",
  "file": {
    "raw_object_key": "raw/3f6c.../665f1c....jpg",
    "mime_type": "image/jpeg | image/png | application/pdf",
    "size_bytes": 2483021,
    "original_filename": "report.jpg"
  },
  "consent_grant_id": "uuid | null   (required when source = provider)",
  "channel": "web | whatsapp",
  "patient_context": { "sex": "M | F | null   (optional; only used to pick sex-specific lab ranges)" }
}
```
- Object key layout (both storage backends): `raw/<patient_id>/<entry_id>.<ext>` (original, never modified), `compressed/<patient_id>/<entry_id>.webp|.pdf`, `thumbs/<patient_id>/<entry_id>.webp`. n8n rejects a `raw_object_key` outside `raw/<patient_id>/`.
- Responses: `202 {"accepted": true}` or `400 {"accepted": false, "errors": [...]}` (bad token or fields).
- `document_type: "unknown"` → n8n asks Gemini to classify first.
- Processing is async after the 202.

### 1.2 `whatsapp.message.received` (internal to n8n)
Comes from Meta/Twilio, **not** the API — listed for completeness. n8n normalises it to:

```json
{
  "wa_message_id": "wamid.HBgM...",
  "from_phone": "+919999900001",
  "kind": "text | image | document",
  "text": "Meri last sugar report kya thi?",
  "media": { "provider_media_id": "...", "mime_type": "image/jpeg" }
}
```

### 1.3 `memory.delete.requested` → `POST {N8N_BASE_URL}/webhook/memory-delete`
Patient taps "Delete chatbot memory" in the app (DPDP right to erasure).
```json
{ "patient_id": "3f6c...", "requested_by": "patient", "scope": "all" }
```
n8n deletes the patient's Meko memory space + traces and the fallback rows, then calls 2.6.

---

## 2. n8n → API (API endpoints n8n calls)

### 2.1 Resolve WhatsApp phone → patient — `GET /internal/patients/by-phone?phone=+919999900001`
```json
// 200
{ "patient_id": "3f6c...", "phone_verified": true, "preferred_language": "hi" }
// 404 → n8n replies "Please register and verify your number in the MedChain app"
```

### 2.2 Create entry for a WhatsApp upload — `POST /internal/timeline-entries`
n8n uploads the raw file to Object Storage first, then:
```json
{
  "patient_id": "3f6c...",
  "source": "patient_uploaded",
  "channel": "whatsapp",
  "document_type": "unknown",
  "file": { "raw_object_key": "raw/3f6c.../wa-<id>.jpg", "mime_type": "image/jpeg", "size_bytes": 1830044 }
}
// 201 → { "entry_id": "665f1c..." }  then n8n runs the same pipeline as 1.1
```

### 2.3 `document.status.updated` — `PATCH /internal/timeline-entries/{entry_id}/status`
Sent at each stage so the frontend can show progress.
```json
{
  "processing_status": "compressing | stored | anchoring | extracting | flagging | completed | failed",
  "error": {
    "stage": "extracting",
    "code": "GEMINI_INVALID_JSON | GEMINI_TIMEOUT | STORAGE_ERROR | SOLANA_ERROR | UNSUPPORTED_FILE | LOW_QUALITY",
    "user_message": "Processing failed, please re-upload",
    "retries": 3
  }
}
```
`error` is present only when `processing_status = "failed"`.

### 2.4 `document.processed` — `PUT /internal/timeline-entries/{entry_id}/parsed`
Final result. The API writes to MongoDB (n8n does **not** write Mongo directly unless Member 1 prefers — see Open Questions).
```json
{
  "document_type": "lab_report",
  "document_date": "2026-10-20",
  "file": {
    "raw_object_key": "raw/3f6c.../665f1c....jpg",
    "object_key": "compressed/3f6c.../665f1c....webp",
    "thumbnail_key": "thumbs/3f6c.../665f1c....webp",
    "mime_type": "image/webp",
    "pages": 1,
    "original_sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "stored_sha256": "2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae",
    "original_size_bytes": 2483021,
    "stored_size_bytes": 412339
  },
  "doc_hash": "9f86d081...  (= original_sha256; this is what goes on-chain)",
  "solana_tx": "5h3k...  | null if anchoring failed (retried later)",
  "parsed": { "...": "lab_report → object valid against contracts/lab-result.schema.json; prescription/bill → see prompts (Step 2)" },
  "summary_for_timeline": "Lipid profile — 2 values flagged (LDL high, HDL low)",
  "source_label": "Patient-uploaded, unverified | Provider-verified"
}
```
**Hash rule:** `doc_hash` is SHA-256 of the **original bytes as uploaded**, before compression. The verified badge re-hashes the original (kept in `raw/`), not the WebP.

### 2.5 Fetch records for chatbot — `GET /internal/patients/{patient_id}/records?types=lab_report,prescription&since=2026-01-01&limit=20`
```json
{
  "records": [
    {
      "entry_id": "665f1c...",
      "document_type": "lab_report",
      "document_date": "2026-10-20",
      "source_label": "Patient-uploaded, unverified",
      "parsed": { "...": "same object as 2.4" }
    }
  ]
}
```
Must be scoped to that patient only. The API logs this as an access by `accessor: "medchain_assistant"`.

### 2.6 `assistant.access` — `POST /internal/access-logs`
Explicit log of what the assistant read to answer a question (patient-visible).
```json
{
  "patient_id": "3f6c...",
  "accessor_type": "ai_assistant",
  "accessor_id": "medchain_assistant",
  "purpose": "whatsapp_qa | upload_processing | memory_delete",
  "record_ids": ["665f1c...", "665f2a..."],
  "correlation_id": "c1a2..."
}
```

### 2.7 Chat memory fallback (only if Meko is unreachable)
`POST /internal/chat-fallback/{patient_id}/messages`
```json
{ "role": "user | assistant", "text": "short message text", "lang": "hi", "ts": "2026-11-01T09:31:00Z" }
```
`GET /internal/chat-fallback/{patient_id}/messages?limit=10` → `{ "messages": [ ... ] }`
`DELETE /internal/chat-fallback/{patient_id}` → `204`
API keeps only the last N (suggest 20) per patient in YugabyteDB.

### 2.8 `patient.notify` — `POST /internal/notifications`
```json
{
  "patient_id": "3f6c...",
  "channel": "whatsapp | in_app",
  "template": "upload_completed | upload_failed",
  "params": { "document_type": "lab report", "document_date": "20 Oct 2026", "flagged_count": 2 }
}
```
(If the API prefers n8n to send WhatsApp directly, n8n handles `whatsapp` itself and calls this only for `in_app`.)

---

## 3. n8n → Solana client (Member 3)

### 3.1 Anchor document hash — `POST {SOLANA_CLIENT_URL}/anchor`
```json
{ "kind": "document", "hash": "9f86d081...", "entry_id": "665f1c...", "patient_ref": "<opaque id, not patient_id>" }
// 200 → { "solana_tx": "5h3k...", "slot": 312345678, "network": "devnet" }
```
Retry 3× with backoff (2s, 8s, 30s). On final failure: continue pipeline with `solana_tx: null`; the entry shows "verification pending".

---

## 4. Processing-status lifecycle

```
queued → compressing → stored → anchoring → extracting → flagging → completed
   └──────────────────── any stage ─────────────────────→ failed ("Processing failed, please re-upload")
```

---

## 5. Open questions for the team
1. **Member 1:** OK with n8n writing to MongoDB *through the API* (2.4) rather than directly? (Recommended: one writer.)
2. **Member 1:** Are the `/internal/*` paths and `INTERNAL_SERVICE_TOKEN` auth acceptable? Please mirror them in `openapi.yaml`.
3. **Member 3:** Confirm the `/anchor` request/response shape and whether `patient_ref` is needed at all.
4. **Member 4:** UI needs `processing_status`, `source_label`, `status` per test, and `confidence < 0.6 → "please verify"`. Anything else?
