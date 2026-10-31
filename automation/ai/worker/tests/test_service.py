"""Tests for the AI worker logic. Fake Gemini + temp folder storage; no network, no Docker.

Run from repo root:  pytest automation/ai/worker -q
"""
import hashlib
import io
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

AI_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AI_DIR))
from extract import ExtractionError  # noqa: E402
from storage import LocalStorage, StorageError, check_key  # noqa: E402
from worker import service  # noqa: E402

SAMPLE = AI_DIR / "samples" / "fake_lab_report_01.png"
FIXTURE = json.loads((AI_DIR / "flagging" / "tests" / "fixtures" / "gemini_fake_lab_report_01.json").read_text(encoding="utf-8"))
PATIENT, ENTRY = "11111111-1111-4111-8111-111111111111", "entry-0001"
RAW_KEY = f"raw/{PATIENT}/{ENTRY}.png"


def gemini_output_from_fixture() -> dict:
    """Turn the stored contract document back into what Gemini returned (no status/range fields)."""
    p = FIXTURE["parsed"]
    keep = ("name", "name_normalized", "value_text", "value", "unit", "range_text", "range_unit", "confidence", "source")
    return {"report_date": p["report_date"], "lab_name": p["lab_name"], "referring_doctor": p["referring_doctor"],
            "tests": [{k: t[k] for k in keep} for t in p["tests"]],
            "languages_detected": p["extraction"]["languages_detected"],
            "overall_confidence": p["extraction"]["overall_confidence"],
            "warnings": list(p["extraction"]["warnings"])}


def fake_gemini(*responses):
    queue = list(responses)

    def fn(file_bytes, mime, prompt, schema, feedback, model):
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return json.dumps(r)
    return fn


@pytest.fixture
def store(tmp_path):
    s = LocalStorage(tmp_path / "objects")
    s.put(RAW_KEY, SAMPLE.read_bytes(), "image/png")
    return s


# ---------------------------------------------------------------- storage safety
@pytest.mark.parametrize("key", [
    "raw/../../etc/passwd", "/etc/passwd", "raw/p1/../x.png", "other/p1/x.png",
    "raw/p1", "raw/p 1/x.png", "", "raw/p1/x.png/../../y",
])
def test_storage_rejects_unsafe_keys(key):
    with pytest.raises(StorageError):
        check_key(key)


def test_storage_roundtrip_and_missing(tmp_path):
    s = LocalStorage(tmp_path)
    s.put("raw/p1/e1.png", b"abc", "image/png")
    assert s.get("raw/p1/e1.png") == b"abc"
    with pytest.raises(StorageError) as e:
        s.get("raw/p1/nope.png")
    assert e.value.code == "NOT_FOUND"


# ---------------------------------------------------------------- compress + store
def test_compress_and_store(store):
    out = service.compress_and_store(store, entry_id=ENTRY, patient_id=PATIENT, raw_object_key=RAW_KEY)
    assert out["doc_hash"] == hashlib.sha256(SAMPLE.read_bytes()).hexdigest()
    f = out["file"]
    assert f["object_key"] == f"compressed/{PATIENT}/{ENTRY}.webp" and f["mime_type"] == "image/webp"
    assert Image.open(io.BytesIO(store.get(f["object_key"]))).format == "WEBP"
    assert store.get(f["thumbnail_key"])
    assert store.get(RAW_KEY) == SAMPLE.read_bytes()            # original untouched (verified badge)
    assert f["stored_sha256"] == hashlib.sha256(store.get(f["object_key"])).hexdigest()


def test_cannot_read_another_patients_file(store):
    with pytest.raises(service.BadRequest):
        service.compress_and_store(store, entry_id=ENTRY, patient_id="someone-else", raw_object_key=RAW_KEY)


@pytest.mark.parametrize("entry_id", ["../x", "a/b", "", "x" * 65, "e 1"])
def test_bad_ids_rejected(store, entry_id):
    with pytest.raises(service.BadRequest):
        service.compress_and_store(store, entry_id=entry_id, patient_id=PATIENT, raw_object_key=RAW_KEY)


def test_missing_raw_file(store):
    with pytest.raises(StorageError) as e:
        service.compress_and_store(store, entry_id=ENTRY, patient_id=PATIENT, raw_object_key=f"raw/{PATIENT}/gone.png")
    assert e.value.code == "NOT_FOUND"


# ---------------------------------------------------------------- extract
def test_extract_lab_report(store):
    out = service.extract_document(store, entry_id=ENTRY, patient_id=PATIENT, raw_object_key=RAW_KEY,
                                   document_type="lab_report", gemini_fn=fake_gemini(gemini_output_from_fixture()))
    assert out["document_type"] == "lab_report" and len(out["parsed"]["tests"]) == 11
    assert out["parsed"]["entry_id"] == ENTRY and out["parsed"]["patient_id"] == PATIENT


def test_extract_unknown_type_classifies_first(store):
    cls = {"document_type": "lab_report", "confidence": 0.95, "reason": "table of tests"}
    out = service.extract_document(store, entry_id=ENTRY, patient_id=PATIENT, raw_object_key=RAW_KEY,
                                   document_type="unknown", gemini_fn=fake_gemini(cls, gemini_output_from_fixture()))
    assert out["classification"]["document_type"] == "lab_report"


def test_extract_gemini_down_raises_api_error(store):
    with pytest.raises(ExtractionError) as e:
        service.extract_document(store, entry_id=ENTRY, patient_id=PATIENT, raw_object_key=RAW_KEY,
                                 document_type="lab_report",
                                 gemini_fn=fake_gemini(TimeoutError(), TimeoutError(), TimeoutError()))
    assert e.value.code == "GEMINI_API_ERROR"


# ---------------------------------------------------------------- flag + summary
def test_flag_lab_report_summary():
    out = service.flag_document(document_type="lab_report", parsed=FIXTURE["parsed"], patient_sex="M")
    assert out["abnormal_count"] == 6 and out["document_date"] == "2026-10-20"
    assert out["summary_for_timeline"].startswith("Lab report: 6 of 11 values flagged: Total Cholesterol (high)")
    assert "+3 more" in out["summary_for_timeline"] and "2 could not be checked" in out["summary_for_timeline"]
    assert out["warnings"] == ["Instruction-like text found in document and ignored."]


def test_flag_invalid_sex_is_ignored():
    out = service.flag_document(document_type="lab_report", parsed=FIXTURE["parsed"], patient_sex="X")
    assert out["abnormal_count"] == 5  # haemoglobin stays unknown


def test_flag_prescription_and_bill_pass_through():
    rx = {"doctor_name": "Dr. Demo Verma", "prescription_date": "2026-10-15", "medicines": [{}, {}], "warnings": []}
    out = service.flag_document(document_type="prescription", parsed=rx)
    assert out["parsed"] == rx and out["summary_for_timeline"] == "Prescription from Dr. Demo Verma: 2 medicine(s)"
    bill = {"vendor_name": "Demo Pharmacy", "bill_date": "2026-10-16", "total": 1540.5, "warnings": []}
    assert service.flag_document(document_type="bill", parsed=bill)["summary_for_timeline"] == "Bill from Demo Pharmacy: ₹1,540.50"


# ---------------------------------------------------------------- trace
def test_trace_has_no_raw_values(tmp_path):
    flagged = service.flag_document(document_type="lab_report", parsed=FIXTURE["parsed"], patient_sex="M")
    t = service.build_trace(entry_id=ENTRY, patient_id=PATIENT, document_type="lab_report",
                            parsed=flagged["parsed"], flagged=flagged["flagged"], correlation_id="c1")
    text = json.dumps(t, ensure_ascii=False)
    assert t["items_extracted"] == 11 and len(t["flagged"]) == 6 and t["flagged_by"].startswith("flag_abnormal@")
    assert "value_text" not in text and "Ramesh" not in text   # decisions + pointers only
    path = tmp_path / "traces.jsonl"
    service.record_trace(t, str(path))
    service.record_trace(t, str(path))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


# ---------------------------------------------------------------- the whole pipeline, as n8n will run it
def test_simulated_upload_pipeline(store, tmp_path):
    stage1 = service.compress_and_store(store, entry_id=ENTRY, patient_id=PATIENT, raw_object_key=RAW_KEY)
    solana_tx = "MOCK" + stage1["doc_hash"][:40]                     # stage 2 (mock Solana)
    stage3 = service.extract_document(store, entry_id=ENTRY, patient_id=PATIENT, raw_object_key=RAW_KEY,
                                      document_type="unknown",
                                      gemini_fn=fake_gemini({"document_type": "lab_report", "confidence": 0.9, "reason": "tests"},
                                                            gemini_output_from_fixture()))
    stage4 = service.flag_document(document_type=stage3["document_type"], parsed=stage3["parsed"], patient_sex="M")
    payload = {"document_type": stage4["document_type"], "document_date": stage4["document_date"],
               "file": stage1["file"], "doc_hash": stage1["doc_hash"], "solana_tx": solana_tx,
               "parsed": stage4["parsed"], "summary_for_timeline": stage4["summary_for_timeline"],
               "source_label": "Patient-uploaded, unverified"}
    json.dumps(payload)  # must be JSON-serialisable for the API
    assert payload["parsed"]["flagging"]["abnormal_count"] == 6
    service.record_trace(service.build_trace(entry_id=ENTRY, patient_id=PATIENT, document_type="lab_report",
                                             parsed=stage4["parsed"], flagged=stage4["flagged"],
                                             classification=stage3["classification"]), str(tmp_path / "t.jsonl"))
