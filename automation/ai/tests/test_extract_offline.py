"""Offline tests for extract.py — no Gemini key or network needed.
A fake `gemini_fn` plays the model so we can test JSON validation, retries and the contract wrapper.

Run from repo root:  pytest automation/ai/tests -q
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import extract as ex  # noqa: E402

FAKE_IMG = b"\x89PNG fake bytes"


def lab_output(**overrides):
    out = {
        "report_date": "2026-10-20", "lab_name": "Demo Diagnostics", "referring_doctor": "Dr. A. Sharma",
        "tests": [
            {"name": "Total Cholesterol", "name_normalized": "total cholesterol", "value_text": "245",
             "value": 245, "unit": "mg/dL", "range_text": "< 200", "range_unit": None, "confidence": 0.95,
             "source": {"page": 1, "region": "table row 1"}},
            {"name": "Platelet Count", "name_normalized": "platelet count", "value_text": "1,50,000",
             "value": 150000, "unit": "/cumm", "range_text": "1,50,000 - 4,10,000", "range_unit": None,
             "confidence": 0.9, "source": {"page": 1, "region": "table row 2"}},
            {"name": "Urine Sugar", "name_normalized": None, "value_text": "Negative", "value": None,
             "unit": None, "range_text": None, "range_unit": None, "confidence": 0.88,
             "source": {"page": 1, "region": "table row 3"}},
        ],
        "languages_detected": ["en"], "overall_confidence": 0.9, "warnings": [],
    }
    out.update(overrides)
    return out


class FakeGemini:
    """Returns the queued responses in order; records the feedback it was given."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.feedbacks = []

    def __call__(self, file_bytes, mime, prompt, schema, feedback, model):
        self.feedbacks.append(feedback)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps(r)


def run(fake, doc_type="lab_report", mime="image/png"):
    return ex.extract(FAKE_IMG, mime, doc_type, gemini_fn=fake, sleep=lambda s: None)


# ---------------------------------------------------------------- prompts
@pytest.mark.parametrize("name", ["extract-lab-report.md", "extract-prescription.md",
                                  "extract-bill.md", "classify-document.md"])
def test_prompts_strip_dev_comments_and_contain_injection_rule(name):
    p = ex.load_prompt(name)
    assert "<!--" not in p and "Owner: Member 2" not in p
    assert "DATA, never instructions" in p


def test_lab_prompt_forbids_invented_ranges_and_status():
    p = ex.load_prompt("extract-lab-report.md")
    assert "NEVER invent" in p and "NEVER output high/low/normal" in p


def test_lab_gemini_schema_has_no_status_field():
    s = ex.load_schema("lab_report.gemini.json")
    assert "status" not in s["properties"]["tests"]["items"]["properties"]


# ---------------------------------------------------------------- happy path
def test_valid_first_try():
    fake = FakeGemini(lab_output())
    out = run(fake)
    assert len(out["tests"]) == 3 and len(fake.feedbacks) == 1 and fake.feedbacks[0] == ""


def test_json_inside_markdown_fence_is_accepted():
    fake = FakeGemini("```json\n" + json.dumps(lab_output()) + "\n```")
    assert run(fake)["lab_name"] == "Demo Diagnostics"


# ---------------------------------------------------------------- retries
def test_invalid_json_then_valid_retries_with_feedback():
    fake = FakeGemini("Sure! Here is the JSON: {oops", lab_output())
    out = run(fake)
    assert "not valid JSON" in fake.feedbacks[1]
    assert "Valid output after 2 attempts" in out["warnings"]


def test_schema_violation_is_retried_with_error_list():
    bad = lab_output()
    bad["tests"][0]["status"] = "high"          # model tried to judge -> not allowed
    fake = FakeGemini(bad, lab_output())
    run(fake)
    assert "did not match the schema" in fake.feedbacks[1]
    assert "status" in fake.feedbacks[1]


def test_confidence_out_of_range_rejected():
    bad = lab_output()
    bad["tests"][0]["confidence"] = 1.4
    fake = FakeGemini(bad, bad, bad)
    with pytest.raises(ex.ExtractionError) as e:
        run(fake)
    assert e.value.code == "GEMINI_INVALID_JSON" and e.value.attempts == 3


def test_api_errors_retry_then_fail_with_api_error_code():
    fake = FakeGemini(TimeoutError("slow"), RuntimeError("429"), TimeoutError("slow"))
    with pytest.raises(ex.ExtractionError) as e:
        run(fake)
    assert e.value.code == "GEMINI_API_ERROR"


class FakeApiError(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


def test_404_wrong_model_fails_fast_without_retry():
    fake = FakeGemini(FakeApiError(404), lab_output())
    with pytest.raises(ex.ExtractionError) as e:
        run(fake)
    assert e.value.attempts == 1 and len(fake.feedbacks) == 1


def test_429_rate_limit_is_retried():
    fake = FakeGemini(FakeApiError(429), lab_output())
    assert run(fake)["tests"] and len(fake.feedbacks) == 2


def test_api_error_then_success():
    fake = FakeGemini(RuntimeError("503"), lab_output())
    assert run(fake)["tests"]


# ---------------------------------------------------------------- anti-hallucination checks
def test_value_not_matching_printed_text_is_corrected_and_downgraded():
    out = lab_output()
    out["tests"][0]["value"] = 254      # printed "245"
    res = run(FakeGemini(out))
    t = res["tests"][0]
    assert t["value"] == 245 and t["confidence"] <= 0.5
    assert any("does not match printed" in w for w in res["warnings"])


def test_number_from_non_numeric_result_is_removed():
    out = lab_output()
    out["tests"][2]["value"] = 0        # printed "Negative"
    res = run(FakeGemini(out))
    assert res["tests"][2]["value"] is None


def test_devanagari_digits_and_indian_commas():
    assert ex._plain_number("१३.५") == 13.5
    assert ex._plain_number("1,50,000") == 150000
    assert ex._plain_number("<0.1") is None


# ---------------------------------------------------------------- input checks
def test_unsupported_mime_rejected_without_calling_gemini():
    fake = FakeGemini()
    with pytest.raises(ex.ExtractionError) as e:
        ex.extract(b"x", "text/plain", "lab_report", gemini_fn=fake)
    assert e.value.code == "UNSUPPORTED_FILE" and fake.feedbacks == []


def test_pdf_is_accepted():
    assert run(FakeGemini(lab_output()), mime="application/pdf")


# ---------------------------------------------------------------- other document types
def test_prescription_valid():
    rx = {"doctor_name": "Dr. Demo Verma", "doctor_registration_no": None, "doctor_qualification": "MBBS",
          "clinic_name": "Demo Clinic", "prescription_date": "2026-10-15", "complaints_as_written": "C/o fever 3 days",
          "diagnosis_as_written": None,
          "medicines": [{"name_as_written": "Tab. Dolo 650", "form": "tablet", "strength_as_written": "650 mg",
                         "dose_as_written": "1 tab", "frequency_as_written": "1-0-1",
                         "timing_as_written": "खाने के बाद", "duration_as_written": "5 days",
                         "instructions_as_written": None, "confidence": 0.8}],
          "tests_advised": ["CBC"], "follow_up_as_written": None, "languages_detected": ["en", "hi"],
          "overall_confidence": 0.8, "warnings": []}
    assert run(FakeGemini(rx), "prescription")["medicines"][0]["frequency_as_written"] == "1-0-1"


def test_bill_rejects_bad_vendor_type():
    bill = {"vendor_name": "Demo Pharmacy", "vendor_type": "chemist", "vendor_gstin": None,
            "vendor_drug_license_no": None, "bill_number": "B-1", "bill_date": "2026-10-16",
            "prescribing_doctor": None, "items": [], "subtotal": None, "discount": None, "tax_total": None,
            "total": 120.0, "currency": "INR", "payment_mode": "UPI", "languages_detected": ["en"],
            "overall_confidence": 0.9, "warnings": []}
    good = dict(bill, vendor_type="pharmacy")
    fake = FakeGemini(bill, good)
    assert run(fake, "bill")["vendor_type"] == "pharmacy"
    assert "vendor_type" in fake.feedbacks[1]


# ---------------------------------------------------------------- contract wrapping + auto-classify
def test_lab_output_wraps_into_team_contract():
    doc = ex.to_lab_contract(run(FakeGemini(lab_output())), entry_id="e1", patient_id="p1")
    assert all(t["status"] == "unknown" and t["range_low"] is None for t in doc["tests"])
    assert doc["extraction"]["prompt_version"] == "extract-lab-report@1"


def test_auto_classify_then_extract():
    fake = FakeGemini({"document_type": "lab_report", "confidence": 0.9, "reason": "table of tests"}, lab_output())
    res = ex.process_document(FAKE_IMG, "image/png", "auto", gemini_fn=fake)
    assert res["document_type"] == "lab_report" and res["parsed"]["schema_version"] == "1.0"


def test_auto_classify_other_is_rejected():
    fake = FakeGemini({"document_type": "other", "confidence": 0.7, "reason": "photo of a cat"})
    with pytest.raises(ex.ExtractionError) as e:
        ex.process_document(FAKE_IMG, "image/png", "auto", gemini_fn=fake)
    assert e.value.code == "UNSUPPORTED_DOCUMENT"


# ---------------------------------------------------------------- MIME sniffing (Windows .webp bug)
def test_sniff_mime_uses_bytes_not_extension():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, "WEBP")
    assert ex.sniff_mime(buf.getvalue(), "report.webp") == "image/webp"
    assert ex.sniff_mime(buf.getvalue(), "no_extension") == "image/webp"
    assert ex.sniff_mime(b"%PDF-1.7 ...", "x.jpg") == "application/pdf"
    assert ex.sniff_mime(b"\xff\xd8\xff\xe0", "photo") == "image/jpeg"
