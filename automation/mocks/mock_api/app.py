"""
MOCK of Member 1's API and Member 3's Solana client, for local testing of the n8n workflows.
Implements the endpoints in contracts/events.md with an in-memory store (saved to MOCK_DB_FILE).
FAKE DATA ONLY. Replace with the real services by changing MEDCHAIN_API_URL / SOLANA_CLIENT_URL.

Browser:  http://localhost:8100/docs
  POST /demo/upload        upload a file the way the real API would -> triggers the n8n pipeline
  GET  /demo/entries       see processing status, summary and flagged values
  GET  /demo/notifications, /demo/access-logs, /demo/anchors
"""
from __future__ import annotations

import json
import os
import random
import re
import secrets
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile

sys.path.insert(0, os.getenv("AI_DIR", "/app/automation/ai"))  # reuse storage.py from the worker
from storage import get_storage  # noqa: E402

app = FastAPI(title="MedChain MOCK API + MOCK Solana", version="1.0.0",
              description="Local stand-in for Member 1 (API) and Member 3 (Solana). FAKE DATA ONLY.")
storage = get_storage()
DB_FILE = Path(os.getenv("MOCK_DB_FILE", "/data/mock_db.json"))
LOCK = threading.Lock()

DEMO_PATIENT = "11111111-1111-4111-8111-111111111111"
DEFAULT_DB = {
    "patients": {
        DEMO_PATIENT: {"patient_id": DEMO_PATIENT, "phone": "+919999900001", "phone_verified": True,
                       "preferred_language": "hi", "sex": "M", "name": "Ramesh Demo Kumar (FAKE)"},
    },
    "entries": {}, "notifications": [], "access_logs": [], "anchors": [], "chat_fallback": {},
}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_mongo_col = None


def _mongo():
    """MOCK_DB_BACKEND=mongodb keeps the mock's state in MongoDB (needed on Render: its disk is wiped
    whenever a free service sleeps). Default: a JSON file at MOCK_DB_FILE."""
    global _mongo_col
    if _mongo_col is None and os.getenv("MOCK_DB_BACKEND", "file").lower() == "mongodb":
        from pymongo import MongoClient
        client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=10000, appname="medchain-mock-api")
        _mongo_col = client[os.getenv("MONGODB_DB", "medchain_files")]["mock_api_state"]
    return _mongo_col


def load() -> dict:
    col = _mongo()
    if col is not None:
        doc = col.find_one({"_id": "state"})
        return doc["data"] if doc else json.loads(json.dumps(DEFAULT_DB))
    if DB_FILE.exists():
        return json.loads(DB_FILE.read_text(encoding="utf-8"))
    return json.loads(json.dumps(DEFAULT_DB))


def save(db: dict) -> None:
    col = _mongo()
    if col is not None:
        col.replace_one({"_id": "state"}, {"_id": "state", "data": db}, upsert=True)
        return
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    DB_FILE.write_text(json.dumps(db, indent=2, ensure_ascii=False), encoding="utf-8")


def require_token(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("INTERNAL_SERVICE_TOKEN", "")
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not expected or not secrets.compare_digest(given, expected):
        raise HTTPException(401, "Missing or wrong service token")


def require_demo_token(token: str | None = None, x_demo_token: str | None = Header(default=None)) -> None:
    """/demo/* is open on your laptop. When DEMO_TOKEN is set (e.g. on Render, where this mock is public),
    pass it as ?token=... (easy in the /docs page) or an X-Demo-Token header."""
    expected = os.getenv("DEMO_TOKEN", "")
    if expected and not secrets.compare_digest(token or x_demo_token or "", expected):
        raise HTTPException(401, "Demo token required: add ?token=<DEMO_TOKEN>")


def entry_or_404(db: dict, entry_id: str) -> dict:
    if entry_id not in db["entries"]:
        raise HTTPException(404, f"No entry {entry_id}")
    return db["entries"][entry_id]


# ================================================================ demo helpers (what the real API + frontend do)
EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic", "application/pdf": "pdf"}


def sniff(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[4:8] == b"ftyp":
        return "image/heic"
    return None


@app.post("/demo/upload", tags=["demo"], dependencies=[Depends(require_demo_token)])
def demo_upload(file: UploadFile = File(...),
                document_type: str = Form("unknown"),
                source: str = Form("patient_uploaded"),
                patient_id: str = Form(DEMO_PATIENT)):
    """Store the raw file, create a 'queued' timeline entry, and send document.uploaded to n8n."""
    data = file.file.read()
    mime = sniff(data)
    if not mime:
        raise HTTPException(400, "Not a JPEG/PNG/WebP/HEIC/PDF file")
    with LOCK:
        db = load()
        if patient_id not in db["patients"]:
            raise HTTPException(404, "Unknown patient")
        entry_id = str(uuid.uuid4())
        raw_key = f"raw/{patient_id}/{entry_id}.{EXT[mime]}"
        storage.put(raw_key, data, mime)
        db["entries"][entry_id] = {"entry_id": entry_id, "patient_id": patient_id, "source": source,
                                   "channel": "web", "document_type": document_type,
                                   "processing_status": "queued", "status_history": [["queued", now()]],
                                   "raw_object_key": raw_key, "created_at": now()}
        save(db)
        sex = db["patients"][patient_id].get("sex")

    event = {
        "event_id": str(uuid.uuid4()), "event_type": "document.uploaded", "version": "1.0",
        "occurred_at": now(), "correlation_id": str(uuid.uuid4()),
        "data": {"entry_id": entry_id, "patient_id": patient_id,
                 "uploaded_by": {"user_id": patient_id, "role": "patient"},
                 "source": source, "document_type": document_type,
                 "file": {"raw_object_key": raw_key, "mime_type": mime, "size_bytes": len(data),
                          "original_filename": file.filename},
                 "consent_grant_id": "demo-consent-grant" if source == "provider" else None,
                 "channel": "web", "patient_context": {"sex": sex}},
    }
    url = os.getenv("N8N_UPLOAD_WEBHOOK_URL", "http://n8n:5678/webhook/upload-pipeline")
    try:
        # Long timeout: on Render's free tier a sleeping n8n needs ~30-60 s to wake up
        r = httpx.post(url, json=event, timeout=float(os.getenv("N8N_WEBHOOK_TIMEOUT", "90")),
                       headers={"X-MedChain-Webhook-Token": os.getenv("N8N_WEBHOOK_TOKEN", "")})
        n8n = {"status_code": r.status_code, "body": r.text[:500]}
    except Exception as exc:
        n8n = {"error": f"Could not reach n8n at {url}: {exc}"}
    return {"entry_id": entry_id, "raw_object_key": raw_key, "sent_to_n8n": n8n,
            "next": f"GET /demo/entries/{entry_id} to watch progress"}


@app.get("/demo/entries", tags=["demo"], dependencies=[Depends(require_demo_token)])
def demo_entries():
    db = load()
    return [{k: e.get(k) for k in ("entry_id", "document_type", "processing_status", "summary_for_timeline",
                                   "solana_tx", "source_label", "error", "created_at")}
            for e in sorted(db["entries"].values(), key=lambda e: e["created_at"], reverse=True)]


@app.get("/demo/entries/{entry_id}", tags=["demo"], dependencies=[Depends(require_demo_token)])
def demo_entry(entry_id: str):
    return entry_or_404(load(), entry_id)


@app.get("/demo/notifications", tags=["demo"], dependencies=[Depends(require_demo_token)])
def demo_notifications():
    return load()["notifications"][-50:]


@app.get("/demo/access-logs", tags=["demo"], dependencies=[Depends(require_demo_token)])
def demo_access_logs():
    return load()["access_logs"][-50:]


@app.get("/demo/anchors", tags=["demo"], dependencies=[Depends(require_demo_token)])
def demo_anchors():
    return load()["anchors"][-50:]


@app.post("/demo/reset", tags=["demo"], dependencies=[Depends(require_demo_token)])
def demo_reset():
    with LOCK:
        save(json.loads(json.dumps(DEFAULT_DB)))
    return {"reset": True}


# ================================================================ /internal (contracts/events.md section 2)
@app.get("/internal/patients/by-phone", dependencies=[Depends(require_token)], tags=["internal"])
def by_phone(phone: str):
    for p in load()["patients"].values():
        if p["phone"] == phone:
            return {"patient_id": p["patient_id"], "phone_verified": p["phone_verified"],
                    "preferred_language": p["preferred_language"], "sex": p.get("sex")}
    raise HTTPException(404, "No patient with this verified phone")


@app.post("/internal/timeline-entries", status_code=201, dependencies=[Depends(require_token)], tags=["internal"])
def create_entry(body: dict):
    with LOCK:
        db = load()
        if body.get("patient_id") not in db["patients"]:
            raise HTTPException(404, "Unknown patient")
        entry_id = str(uuid.uuid4())
        db["entries"][entry_id] = {"entry_id": entry_id, "patient_id": body["patient_id"],
                                   "source": body.get("source", "patient_uploaded"),
                                   "channel": body.get("channel", "whatsapp"),
                                   "document_type": body.get("document_type", "unknown"),
                                   "processing_status": "queued", "status_history": [["queued", now()]],
                                   "raw_object_key": (body.get("file") or {}).get("raw_object_key"),
                                   "created_at": now()}
        save(db)
    return {"entry_id": entry_id}


@app.patch("/internal/timeline-entries/{entry_id}/status", dependencies=[Depends(require_token)], tags=["internal"])
def set_status(entry_id: str, body: dict):
    allowed = {"queued", "compressing", "stored", "anchoring", "extracting", "flagging", "completed", "failed"}
    if body.get("processing_status") not in allowed:
        raise HTTPException(400, f"processing_status must be one of {sorted(allowed)}")
    with LOCK:
        db = load()
        e = entry_or_404(db, entry_id)
        e["processing_status"] = body["processing_status"]
        e["status_history"].append([body["processing_status"], now()])
        if body.get("error"):
            e["error"] = body["error"]
        save(db)
    return {"entry_id": entry_id, "processing_status": e["processing_status"]}


@app.put("/internal/timeline-entries/{entry_id}/parsed", dependencies=[Depends(require_token)], tags=["internal"])
def save_parsed(entry_id: str, body: dict):
    for k in ("document_type", "file", "doc_hash", "parsed", "summary_for_timeline", "source_label"):
        if k not in body:
            raise HTTPException(400, f"Missing field: {k}")
    if not re.fullmatch(r"[0-9a-f]{64}", str(body["doc_hash"])):
        raise HTTPException(400, "doc_hash must be 64 lowercase hex characters (SHA-256)")
    with LOCK:
        db = load()
        e = entry_or_404(db, entry_id)
        e.update({k: body.get(k) for k in ("document_type", "document_date", "file", "doc_hash", "solana_tx",
                                           "parsed", "summary_for_timeline", "source_label")})
        e["parsed_saved_at"] = now()
        save(db)
    return {"entry_id": entry_id, "saved": True}


@app.get("/internal/patients/{patient_id}/records", dependencies=[Depends(require_token)], tags=["internal"])
def records(patient_id: str, types: str | None = None, limit: int = 20):
    wanted = set(types.split(",")) if types else None
    db = load()
    out = [{k: e.get(k) for k in ("entry_id", "document_type", "document_date", "source_label",
                                  "summary_for_timeline", "parsed")}
           for e in db["entries"].values()
           if e["patient_id"] == patient_id and e.get("processing_status") == "completed"
           and (not wanted or e.get("document_type") in wanted)]
    out.sort(key=lambda r: r.get("document_date") or "", reverse=True)
    return {"records": out[:limit]}


@app.post("/internal/access-logs", status_code=201, dependencies=[Depends(require_token)], tags=["internal"])
def access_log(body: dict):
    with LOCK:
        db = load()
        db["access_logs"].append({**body, "logged_at": now()})
        save(db)
    return {"logged": True}


@app.post("/internal/notifications", status_code=201, dependencies=[Depends(require_token)], tags=["internal"])
def notify(body: dict):
    with LOCK:
        db = load()
        db["notifications"].append({**body, "sent_at": now(), "delivered": "mock (not really sent)"})
        save(db)
    return {"queued": True}


# ================================================================ MOCK Solana client (contracts/events.md section 3)
@app.post("/solana/anchor", tags=["mock solana"])
def anchor(body: dict):
    """Pretends to write the hash to Solana devnet. Set MOCK_SOLANA_FAIL_RATE=0.5 to test failures."""
    if not re.fullmatch(r"[0-9a-f]{64}", str(body.get("hash", ""))):
        raise HTTPException(400, "hash must be a SHA-256 hex string")
    if random.random() < float(os.getenv("MOCK_SOLANA_FAIL_RATE", "0")):
        raise HTTPException(503, "Mock Solana: simulated devnet failure")
    tx = "MOCKTX" + secrets.token_hex(20)
    with LOCK:
        db = load()
        db["anchors"].append({"kind": body.get("kind"), "hash": body["hash"], "entry_id": body.get("entry_id"),
                              "solana_tx": tx, "at": now()})
        save(db)
    return {"solana_tx": tx, "slot": random.randint(300_000_000, 399_999_999), "network": "mock-devnet"}


@app.get("/health", tags=["demo"])
def health():
    return {"ok": True, "mock": True, "storage": storage.healthy()}
