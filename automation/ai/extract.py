"""
MedChain document extraction (Member 2).

Reads ONE document (image or PDF), asks Gemini to transcribe it into strict JSON,
validates that JSON, retries with feedback when it is invalid, and (for lab reports)
wraps it into contracts/lab-result.schema.json with status = "unknown".
The flagging code (Step 3) later decides high/low. The AI never does.

CLI (from repo root):
    python automation/ai/extract.py path/to/report.jpg --type lab_report
    python automation/ai/extract.py path/to/photo.jpg  --type auto      # classify first
    python automation/ai/extract.py --list-models                       # check your key

Env: GEMINI_API_KEY (required), GEMINI_MODEL (default below), GEMINI_MAX_ATTEMPTS (default 3)
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from jsonschema import Draft202012Validator, FormatChecker

AI_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = AI_DIR / "prompts"
SCHEMAS_DIR = PROMPTS_DIR / "schemas"
CONTRACTS_DIR = AI_DIR.parent.parent / "contracts"

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
MAX_ATTEMPTS = int(os.getenv("GEMINI_MAX_ATTEMPTS", "3"))
SUPPORTED_MIME = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "application/pdf"}

# document_type -> (prompt file, Gemini output schema, prompt version tag)
DOC_TYPES = {
    "lab_report": ("extract-lab-report.md", "lab_report.gemini.json", "extract-lab-report@1"),
    "prescription": ("extract-prescription.md", "prescription.gemini.json", "extract-prescription@1"),
    "bill": ("extract-bill.md", "bill.gemini.json", "extract-bill@1"),
    "classify": ("classify-document.md", "classify.gemini.json", "classify-document@1"),
}

# Signature of the function that actually talks to Gemini. Tests pass a fake one.
GeminiFn = Callable[[bytes, str, str, dict, str, str], str]


class ExtractionError(Exception):
    """Raised when extraction fails for good. `code` matches contracts/events.md error codes."""

    def __init__(self, code: str, message: str, attempts: int = 0):
        super().__init__(message)
        self.code = code
        self.attempts = attempts


# ---------------------------------------------------------------- prompts & schemas
def load_prompt(filename: str) -> str:
    """Prompt files keep developer notes in <!-- --> comments; strip them before sending."""
    text = (PROMPTS_DIR / filename).read_text(encoding="utf-8")
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


def load_schema(filename: str, folder: Path = SCHEMAS_DIR) -> dict:
    return json.loads((folder / filename).read_text(encoding="utf-8"))


def validator_for(schema: dict) -> Draft202012Validator:
    return Draft202012Validator(schema, format_checker=FormatChecker())


# ---------------------------------------------------------------- Gemini call
def call_gemini(file_bytes: bytes, mime_type: str, system_prompt: str, schema: dict,
                extra_user_text: str, model: str) -> str:
    """The ONLY function that talks to Gemini. Returns raw response text.
    Swap this out if the team moves to the newer Interactions API."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    user_text = "Transcribe the attached document into JSON following your instructions."
    if extra_user_text:
        user_text += "\n\n" + extra_user_text
    resp = client.models.generate_content(
        model=model,
        contents=[types.Part.from_bytes(data=file_bytes, mime_type=mime_type), user_text],
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_json_schema=schema,  # Gemini enforces structure; we re-validate anyway
            temperature=0,                # transcription, not creativity
        ),
    )
    return resp.text or ""


def sniff_mime(data: bytes, filename: str = "") -> str:
    """Detect the MIME type from the file's first bytes (file names and OS registries lie;
    e.g. Python 3.10 on Windows does not know '.webp'). Falls back to the extension."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1"):
        return "image/heic"
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


# ---------------------------------------------------------------- validation helpers
def parse_json(text: str) -> dict:
    """Parse model output. Tolerates a ```json fence, nothing else."""
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, flags=re.S)
    if fence:
        cleaned = fence.group(1)
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("Top-level JSON must be an object")
    return data


def schema_errors(data: dict, schema: dict, limit: int = 8) -> list[str]:
    errs = sorted(validator_for(schema).iter_errors(data), key=lambda e: list(e.path))
    return [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in errs[:limit]]


def _plain_number(text: str | None) -> float | None:
    """'1,50,000' -> 150000.0, '13.5' -> 13.5, anything else -> None. Handles Devanagari digits."""
    if text is None:
        return None
    t = text.strip().translate(str.maketrans("०१२३४५६७८९", "0123456789")).replace(",", "")
    return float(t) if re.fullmatch(r"[-+]?\d+(\.\d+)?", t) else None


def lab_consistency_checks(data: dict) -> None:
    """Cheap anti-hallucination checks on a lab extraction. Mutates `data` in place:
    adds warnings and lowers confidence; never invents values."""
    for i, t in enumerate(data.get("tests", [])):
        label = t.get("name") or f"row {i + 1}"
        parsed = _plain_number(t.get("value_text"))
        if t.get("value") is not None and parsed is not None and abs(parsed - t["value"]) > 1e-9:
            data["warnings"].append(f"'{label}': value {t['value']} does not match printed '{t['value_text']}'; using printed text")
            t["value"] = parsed
            t["confidence"] = min(t["confidence"], 0.5)
        elif t.get("value") is not None and t.get("value_text") and parsed is None:
            # model produced a number from something like '<0.1' or 'Negative' -> not allowed
            data["warnings"].append(f"'{label}': printed result '{t['value_text']}' is not a plain number; value set to null")
            t["value"] = None
        elif t.get("value") is None and parsed is not None:
            t["value"] = parsed  # model forgot to fill the number


# ---------------------------------------------------------------- core extraction
def extract(file_bytes: bytes, mime_type: str, doc_type: str, *,
            gemini_fn: GeminiFn = call_gemini, model: str = DEFAULT_MODEL,
            max_attempts: int = MAX_ATTEMPTS, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Return validated Gemini output for doc_type. Raises ExtractionError after max_attempts."""
    if doc_type not in DOC_TYPES:
        raise ExtractionError("UNSUPPORTED_TYPE", f"Unknown document type '{doc_type}'")
    if mime_type not in SUPPORTED_MIME:
        raise ExtractionError("UNSUPPORTED_FILE", f"Unsupported file type '{mime_type}'")

    prompt_file, schema_file, _ = DOC_TYPES[doc_type]
    prompt, schema = load_prompt(prompt_file), load_schema(schema_file)

    feedback = ""
    last_code, last_msg = "GEMINI_INVALID_JSON", ""
    for attempt in range(1, max_attempts + 1):
        try:
            raw = gemini_fn(file_bytes, mime_type, prompt, schema, feedback, model)
        except Exception as exc:
            last_code, last_msg = "GEMINI_API_ERROR", f"{type(exc).__name__}: {exc}"
            status = getattr(exc, "code", None)
            if isinstance(status, int) and 400 <= status < 500 and status != 429:
                # 400 bad request, 401/403 bad key, 404 wrong model: retrying won't help
                raise ExtractionError("GEMINI_API_ERROR", last_msg, attempts=attempt) from exc
            sleep(2 ** attempt)  # 429 rate limit, 5xx, network, timeout -> back off and retry
            continue

        try:
            data = parse_json(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            last_code, last_msg = "GEMINI_INVALID_JSON", f"Not valid JSON: {exc}"
            feedback = ("Your previous answer was not valid JSON. Return ONLY one JSON object "
                        "that matches the schema. No markdown, no text.")
            continue

        errors = schema_errors(data, schema)
        if errors:
            last_code, last_msg = "GEMINI_INVALID_JSON", "; ".join(errors)
            feedback = ("Your previous JSON did not match the schema. Fix these problems and return the "
                        "full JSON again:\n- " + "\n- ".join(errors))
            continue

        if doc_type == "lab_report":
            lab_consistency_checks(data)
        if attempt > 1:
            data.setdefault("warnings", []).append(f"Valid output after {attempt} attempts")
        return data

    raise ExtractionError(last_code, last_msg, attempts=max_attempts)


def to_lab_contract(gemini_out: dict, *, entry_id: str, patient_id: str,
                    model: str = DEFAULT_MODEL) -> dict:
    """Wrap Gemini lab output into contracts/lab-result.schema.json.
    range_low/high are parsed by the flagging code (Step 3); status starts as 'unknown'."""
    tests = []
    for t in gemini_out["tests"]:
        tests.append({
            "name": t["name"],
            "name_normalized": t.get("name_normalized"),
            "value": t["value"],
            "value_text": t.get("value_text"),
            "unit": t.get("unit"),
            "range_low": None,
            "range_high": None,
            "range_text": t.get("range_text"),
            "range_unit": t.get("range_unit"),
            "status": "unknown",
            "flag_reason": None,
            "confidence": t["confidence"],
            "source": t.get("source"),
        })
    doc = {
        "schema_version": "1.0",
        "entry_id": entry_id,
        "patient_id": patient_id,
        "report_date": gemini_out.get("report_date"),
        "lab_name": gemini_out.get("lab_name"),
        "referring_doctor": gemini_out.get("referring_doctor"),
        "tests": tests,
        "extraction": {
            "model": model,
            "prompt_version": DOC_TYPES["lab_report"][2],
            "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "overall_confidence": gemini_out["overall_confidence"],
            "languages_detected": gemini_out.get("languages_detected", []),
            "warnings": gemini_out.get("warnings", []),
        },
    }
    errs = schema_errors(doc, load_schema("lab-result.schema.json", CONTRACTS_DIR))
    if errs:  # should never happen; means our wrapper and the contract drifted apart
        raise ExtractionError("CONTRACT_MISMATCH", "; ".join(errs))
    return doc


def process_document(file_bytes: bytes, mime_type: str, doc_type: str = "auto", *,
                     entry_id: str | None = None, patient_id: str | None = None,
                     gemini_fn: GeminiFn = call_gemini, model: str = DEFAULT_MODEL) -> dict:
    """What n8n will call (Step 5). Returns {document_type, classification?, parsed}."""
    result: dict = {}
    if doc_type in ("auto", "unknown"):
        cls = extract(file_bytes, mime_type, "classify", gemini_fn=gemini_fn, model=model)
        result["classification"] = cls
        doc_type = cls["document_type"]
        if doc_type == "other":
            raise ExtractionError("UNSUPPORTED_DOCUMENT", f"Not a lab report, prescription or bill: {cls['reason']}")
    out = extract(file_bytes, mime_type, doc_type, gemini_fn=gemini_fn, model=model)
    if doc_type == "lab_report":
        out = to_lab_contract(out, entry_id=entry_id or "local-test",
                              patient_id=patient_id or "demo-patient", model=model)
    result.update(document_type=doc_type, parsed=out)
    return result


# ---------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="Extract a medical document with Gemini")
    ap.add_argument("file", nargs="?", help="image or PDF")
    ap.add_argument("--type", default="auto", choices=["auto", "lab_report", "prescription", "bill"])
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--out", help="write JSON here instead of stdout")
    ap.add_argument("--list-models", action="store_true", help="list models your key can use")
    args = ap.parse_args()

    if not os.getenv("GEMINI_API_KEY"):
        print("Set GEMINI_API_KEY first (see automation/.env.example)", file=sys.stderr)
        return 2
    if args.list_models:
        from google import genai
        # Keep the client in a variable: the list is fetched lazily, and a temporary
        # client gets garbage-collected (closing its connection) before the request runs.
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        for m in client.models.list():
            if "generateContent" in (getattr(m, "supported_actions", None) or ["generateContent"]):
                print(m.name)
        return 0
    if not args.file:
        ap.error("file is required")

    path = Path(args.file)
    data = path.read_bytes()
    mime = sniff_mime(data, path.name)
    try:
        result = process_document(data, mime, args.type,
                                  entry_id=f"local-{uuid.uuid4().hex[:8]}", model=args.model)
    except ExtractionError as e:
        print(json.dumps({"error": e.code, "message": str(e), "attempts": e.attempts}, indent=2))
        return 1
    text = json.dumps(result, indent=2, ensure_ascii=False)
    Path(args.out).write_text(text, encoding="utf-8") if args.out else print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
