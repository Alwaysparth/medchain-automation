"""
AI worker business logic (Member 2). Plain functions, no web framework, so they are easy to test.
worker/app.py exposes them over HTTP for n8n.

Pipeline stages (called by the n8n upload-pipeline workflow, in this order):
  1. compress_and_store  raw file -> WebP/PDF + thumbnail in object storage, SHA-256 of the original
  2. (n8n calls the Solana client with doc_hash)
  3. extract_document    Gemini transcription (classifies first if type is unknown)
  4. flag_document       deterministic high/low for lab reports + one-line timeline summary
  5. record_trace        small audit record of the AI decision (Step 6 sends it to Meko)
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from compression.compress_image import compress_document
from extract import DEFAULT_MODEL, GeminiFn, call_gemini, process_document, sniff_mime
from flagging.flag_abnormal import flag_lab_result
from storage import StorageError, check_key

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DOC_LABELS = {"lab_report": "Lab report", "prescription": "Prescription", "bill": "Bill"}


class BadRequest(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _check_id(name: str, value: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise BadRequest("BAD_REQUEST", f"{name} must be 1-64 letters, digits, '-' or '_'")
    return value


def _check_raw_key(key: str, patient_id: str) -> str:
    check_key(key)
    if not key.startswith(f"raw/{patient_id}/"):
        raise BadRequest("BAD_REQUEST", "raw_object_key does not belong to this patient")
    return key


# ---------------------------------------------------------------- 1. compress + store
def compress_and_store(storage, *, entry_id: str, patient_id: str, raw_object_key: str) -> dict:
    _check_id("entry_id", entry_id)
    _check_id("patient_id", patient_id)
    _check_raw_key(raw_object_key, patient_id)

    raw = storage.get(raw_object_key)
    r = compress_document(raw, raw_object_key)
    ext = "pdf" if r.stored_mime == "application/pdf" else "webp"
    object_key = f"compressed/{patient_id}/{entry_id}.{ext}"
    storage.put(object_key, r.stored_bytes, r.stored_mime)
    thumb_key = None
    if r.thumbnail_bytes:
        thumb_key = f"thumbs/{patient_id}/{entry_id}.webp"
        storage.put(thumb_key, r.thumbnail_bytes, "image/webp")

    s = r.to_dict()
    return {
        "doc_hash": r.original_sha256,  # what goes on-chain (events.md 2.4)
        "file": {
            "raw_object_key": raw_object_key,
            "object_key": object_key,
            "thumbnail_key": thumb_key,
            "mime_type": r.stored_mime,
            "pages": r.pages,
            "original_sha256": r.original_sha256,
            "stored_sha256": r.stored_sha256,
            "original_size_bytes": r.original_size_bytes,
            "stored_size_bytes": r.stored_size_bytes,
        },
        "compression": {k: s[k] for k in ("action", "webp_quality", "lossless", "psnr_db",
                                          "saved_percent", "metadata_stripped", "width", "height")},
        "warnings": r.warnings,
    }


# ---------------------------------------------------------------- 3. extract
def extract_document(storage, *, entry_id: str, patient_id: str, raw_object_key: str,
                     document_type: str, gemini_fn: GeminiFn = call_gemini,
                     model: str = DEFAULT_MODEL) -> dict:
    """Extract from the ORIGINAL upload (best fidelity). Raises ExtractionError on failure."""
    _check_id("entry_id", entry_id)
    _check_id("patient_id", patient_id)
    _check_raw_key(raw_object_key, patient_id)
    if document_type not in ("lab_report", "prescription", "bill", "unknown", "auto"):
        raise BadRequest("BAD_REQUEST", f"Unknown document_type '{document_type}'")

    raw = storage.get(raw_object_key)
    result = process_document(raw, sniff_mime(raw, raw_object_key), document_type,
                              entry_id=entry_id, patient_id=patient_id, gemini_fn=gemini_fn, model=model)
    result["model"] = model
    return result


# ---------------------------------------------------------------- 4. flag + summary
def _document_date(document_type: str, parsed: dict) -> str | None:
    key = {"lab_report": "report_date", "prescription": "prescription_date", "bill": "bill_date"}.get(document_type)
    return parsed.get(key) if key else None


def _summary(document_type: str, parsed: dict, flagged: list[dict]) -> str:
    if document_type == "lab_report":
        tests = parsed.get("tests", [])
        unknown = sum(t["status"] == "unknown" for t in tests)
        if not flagged:
            text = f"Lab report: {len(tests)} tests, none flagged"
        else:
            names = ", ".join(f"{f['name']} ({f['status']})" for f in flagged[:3])
            more = f" +{len(flagged) - 3} more" if len(flagged) > 3 else ""
            text = f"Lab report: {len(flagged)} of {len(tests)} values flagged: {names}{more}"
        return text + (f"; {unknown} could not be checked" if unknown else "")
    if document_type == "prescription":
        doctor = parsed.get("doctor_name") or "unknown doctor"
        return f"Prescription from {doctor}: {len(parsed.get('medicines', []))} medicine(s)"
    if document_type == "bill":
        vendor = parsed.get("vendor_name") or "unknown vendor"
        total = parsed.get("total")
        return f"Bill from {vendor}" + (f": ₹{total:,.2f}" if isinstance(total, (int, float)) else "")
    return "Document"


def flag_document(*, document_type: str, parsed: dict, patient_sex: str | None = None) -> dict:
    """Lab reports: deterministic flagging. Other types: passed through unchanged."""
    if document_type not in DOC_LABELS:
        raise BadRequest("BAD_REQUEST", f"Unknown document_type '{document_type}'")
    if patient_sex not in (None, "M", "F"):
        patient_sex = None
    flagged: list[dict] = []
    if document_type == "lab_report":
        parsed = flag_lab_result(parsed, patient_sex)
        flagged = [{"name": t["name"], "status": t["status"], "reason": t["flag_reason"]}
                   for t in parsed["tests"] if t["status"] in ("high", "low")]
    warnings = (parsed.get("extraction") or {}).get("warnings") or parsed.get("warnings") or []
    return {
        "document_type": document_type,
        "document_date": _document_date(document_type, parsed),
        "parsed": parsed,
        "flagged": flagged,
        "abnormal_count": len(flagged),
        "summary_for_timeline": _summary(document_type, parsed, flagged),
        "warnings": warnings,
    }


# ---------------------------------------------------------------- 5. trace
def build_trace(*, entry_id: str, patient_id: str, document_type: str, parsed: dict,
                flagged: list[dict], correlation_id: str | None = None,
                classification: dict | None = None) -> dict:
    """Small, explainable record of what the AI did. Pointers + decisions only, never the report itself."""
    extraction = parsed.get("extraction") or {}
    items = parsed.get("tests") or parsed.get("medicines") or parsed.get("items") or []
    return {
        "type": "document_extraction",
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "entry_id": entry_id,
        "patient_id": patient_id,
        "correlation_id": correlation_id,
        "document_type": document_type,
        "classification": ({"type": classification.get("document_type"),
                            "confidence": classification.get("confidence")} if classification else None),
        "model": extraction.get("model"),
        "prompt_version": extraction.get("prompt_version"),
        "items_extracted": len(items),
        "overall_confidence": extraction.get("overall_confidence", parsed.get("overall_confidence")),
        "low_confidence_items": sum(1 for i in items if (i.get("confidence") or 1) < 0.6),
        "flagged": [{"name": f["name"], "status": f["status"], "reason": f["reason"]} for f in flagged],
        "flagged_by": (parsed.get("flagging") or {}).get("flagger_version"),
        "warnings": extraction.get("warnings", parsed.get("warnings", [])),
    }


def record_trace(trace: dict, path: str | None = None) -> None:
    """Step 5: append to a local JSONL file. Step 6 replaces this with Meko (fallback: this file)."""
    p = Path(path or os.getenv("TRACE_FILE", "./traces.jsonl"))
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(trace, ensure_ascii=False) + "\n")


__all__ = ["BadRequest", "StorageError", "compress_and_store", "extract_document",
           "flag_document", "build_trace", "record_trace"]
