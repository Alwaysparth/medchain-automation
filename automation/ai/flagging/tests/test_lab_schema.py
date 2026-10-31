"""Sanity test for contracts/lab-result.schema.json.

Run from repo root:   python automation/ai/flagging/tests/test_lab_schema.py
Needs:                pip install jsonschema
"""
import json
import os
from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "../../../../contracts/lab-result.schema.json")
schema = json.load(open(SCHEMA_PATH, encoding="utf-8"))
Draft202012Validator.check_schema(schema)  # the schema itself is valid JSON Schema 2020-12
validator = Draft202012Validator(schema, format_checker=FormatChecker())


def make_test(**overrides):
    """One FAKE test row; override fields per case."""
    row = dict(
        name="Total Cholesterol", value=245, value_text="245", unit="mg/dL",
        range_low=None, range_high=200, range_high_inclusive=False, range_text="< 200",
        status="unknown", confidence=0.94, source={"page": 1, "region": "table row 3"},
    )
    row.update(overrides)
    return row


def make_doc(tests):
    return dict(
        schema_version="1.0", entry_id="e1", patient_id="p1", report_date="2026-10-20", tests=tests,
        extraction=dict(model="gemini-2.5-flash", extracted_at="2026-11-01T09:30:00Z", overall_confidence=0.9),
    )


no_range_text = make_test()
no_range_text.pop("range_text")

CASES = {
    "valid full report": (make_doc([
        make_test(),
        make_test(name="Urine Sugar", value=None, value_text="Negative", unit=None,
                  range_high=None, range_text=None),
    ]), True),
    "valid with no tests": (make_doc([]), True),
    "rejects status 'critical'": (make_doc([make_test(status="critical")]), False),
    "rejects value as string": (make_doc([make_test(value="245")]), False),
    "rejects confidence 1.5": (make_doc([make_test(confidence=1.5)]), False),
    "rejects missing range_text": (make_doc([no_range_text]), False),
    "rejects extra 'diagnosis' field": (make_doc([make_test(diagnosis="x")]), False),
}

if __name__ == "__main__":
    failures = 0
    for name, (doc, expected) in CASES.items():
        ok = validator.is_valid(doc) == expected
        failures += not ok
        print("PASS" if ok else "FAIL", name)
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    raise SystemExit(failures)
