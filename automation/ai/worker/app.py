"""
MedChain AI worker: thin HTTP layer over worker/service.py, called by n8n.

Run locally (from automation/ai):   uvicorn worker.app:app --reload --port 8000
Docs in browser:                    http://localhost:8000/docs
Every route except /health needs:   Authorization: Bearer <INTERNAL_SERVICE_TOKEN>

Error responses: {"detail": {"code": "...", "message": "..."}}
  400 bad input · 404 file not found · 422 document cannot be processed (don't retry)
  502 storage problem · 503 Gemini temporarily unavailable (retry later)
"""
from __future__ import annotations

import os
import secrets
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from compression.compress_image import CompressionError
from extract import DEFAULT_MODEL, ExtractionError
from storage import StorageError, get_storage
from worker import service

app = FastAPI(title="MedChain AI Worker", version="1.0.0",
              description="Compression, Gemini extraction, deterministic flagging and AI traces. Member 2.")
storage = get_storage()


def require_token(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("INTERNAL_SERVICE_TOKEN", "")
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not expected or not secrets.compare_digest(given, expected):
        raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "Missing or wrong service token"})


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": {"code": code, "message": message}})


@app.exception_handler(service.BadRequest)
async def _bad_request(_: Request, e: service.BadRequest):
    return _err(400, e.code, str(e))


@app.exception_handler(StorageError)
async def _storage(_: Request, e: StorageError):
    return _err({"NOT_FOUND": 404, "BAD_KEY": 400}.get(e.code, 502), e.code, str(e))


@app.exception_handler(CompressionError)
async def _compression(_: Request, e: CompressionError):
    return _err(422, e.code, str(e))


@app.exception_handler(ExtractionError)
async def _extraction(_: Request, e: ExtractionError):
    return _err(503 if e.code == "GEMINI_API_ERROR" else 422, e.code, str(e))


# ---------------------------------------------------------------- request models
class CompressStoreIn(BaseModel):
    entry_id: str
    patient_id: str
    raw_object_key: str


class ExtractIn(CompressStoreIn):
    document_type: Literal["lab_report", "prescription", "bill", "unknown"] = "unknown"


class FlagIn(BaseModel):
    document_type: Literal["lab_report", "prescription", "bill"]
    parsed: dict
    patient_sex: Literal["M", "F"] | None = None


class TraceIn(BaseModel):
    entry_id: str
    patient_id: str
    document_type: str
    parsed: dict
    flagged: list[dict] = Field(default_factory=list)
    correlation_id: str | None = None
    classification: dict | None = None


# ---------------------------------------------------------------- routes
@app.get("/health")
def health():
    return {"ok": True, "storage": storage.healthy(), "gemini_key_set": bool(os.getenv("GEMINI_API_KEY")),
            "model": DEFAULT_MODEL, "storage_backend": os.getenv("STORAGE_BACKEND", "local")}


@app.post("/pipeline/compress-store", dependencies=[Depends(require_token)])
def compress_store(body: CompressStoreIn):
    return service.compress_and_store(storage, **body.model_dump())


@app.post("/pipeline/extract", dependencies=[Depends(require_token)])
def extract(body: ExtractIn):
    return service.extract_document(storage, **body.model_dump())


@app.post("/pipeline/flag", dependencies=[Depends(require_token)])
def flag(body: FlagIn):
    return service.flag_document(**body.model_dump())


@app.post("/trace", dependencies=[Depends(require_token)])
def trace(body: TraceIn):
    t = service.build_trace(**body.model_dump())
    service.record_trace(t)
    return {"recorded": True, "trace": t}
