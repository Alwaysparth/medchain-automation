"""Tests for the deterministic flagging code. No AI, no network.

Run from repo root:  pytest automation/ai/flagging -q
"""
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from flag_abnormal import RangeError, flag_lab_result, flag_test, parse_range  # noqa: E402

CONTRACT = json.loads((HERE.parents[3] / "contracts" / "lab-result.schema.json").read_text(encoding="utf-8"))


def row(value, range_text, value_text=None, unit="mg/dL", **kw):
    """Build one extracted test row the way extract.py produces it."""
    if value_text is None and value is not None:
        value_text = str(value)
    return {"name": "Test", "value": value, "value_text": value_text, "unit": unit,
            "range_low": None, "range_high": None, "range_text": range_text, "range_unit": None,
            "status": "unknown", "flag_reason": None, "confidence": 0.95, **kw}


# ---------------------------------------------------------------- range parsing
@pytest.mark.parametrize("text, low, high, lo_inc, hi_inc", [
    ("70-110", 70, 110, True, True),
    ("70 - 110", 70, 110, True, True),
    ("70 to 110", 70, 110, True, True),
    ("4.0 – 5.6", 4.0, 5.6, True, True),            # en dash
    ("0.27—4.20", 0.27, 4.20, True, True),          # em dash
    ("1,50,000 - 4,10,000", 150000, 410000, True, True),  # Indian commas
    ("150,000-410,000", 150000, 410000, True, True),
    ("-2 - +2", -2, 2, True, True),                 # signed (base excess)
    ("(70-110)", 70, 110, True, True),
    ("[13.0 - 17.0]", 13, 17, True, True),
    ("70-110 mg/dL", 70, 110, True, True),
    ("70-110 (Fasting)", 70, 110, True, True),
    ("< 200", None, 200, True, False),
    ("<200", None, 200, True, False),
    ("<= 200", None, 200, True, True),
    ("≤200", None, 200, True, True),
    ("=< 200", None, 200, True, True),
    ("Up to 130", None, 130, True, True),
    ("upto 5.0", None, 5.0, True, True),
    ("UP TO 40", None, 40, True, True),
    ("less than 200", None, 200, True, False),
    ("Below 5", None, 5, True, False),
    ("Max 200", None, 200, True, True),
    ("200 max", None, 200, True, True),
    ("> 40", 40, None, False, True),
    (">= 12", 12, None, True, True),
    ("≥12", 12, None, True, True),
    ("Above 40", 40, None, False, True),
    ("at least 12", 12, None, True, True),
    ("Min 12", 12, None, True, True),
    ("12 and above", 12, None, True, True),
    ("200 तक", None, 200, True, True),              # Hindi: up to
    ("200 से कम", None, 200, True, False),           # Hindi: less than
    ("40 से अधिक", 40, None, False, True),           # Hindi: more than
    ("७० - ११०", 70, 110, True, True),              # Devanagari digits
    ("Adults: 70-110", 70, 110, True, True),        # single labelled range
    ("Ref. range: 3.5-5.0", 3.5, 5.0, True, True),
])
def test_parse_range_formats(text, low, high, lo_inc, hi_inc):
    r = parse_range(text)
    assert (float(r.low) if r.low is not None else None) == low
    assert (float(r.high) if r.high is not None else None) == high
    assert (r.low_inclusive, r.high_inclusive) == (lo_inc, hi_inc)


@pytest.mark.parametrize("text, why", [
    (None, "no reference range printed"),
    ("", "no reference range printed"),
    ("   ", "no reference range printed"),
    ("See comments", "could not read range"),
    ("110-70", "could not read range"),                                  # reversed: don't guess
    ("Adults: 70-110; Children: 60-100", "multiple ranges"),
    ("M: 13-17  F: 12-15", "patient sex not provided"),
])
def test_parse_range_refuses_to_guess(text, why):
    with pytest.raises(RangeError) as e:
        parse_range(text)
    assert why in str(e.value)


@pytest.mark.parametrize("text, sex, low, high", [
    ("M: 13 - 17  F: 12 - 15", "M", 13, 17),
    ("M: 13 - 17  F: 12 - 15", "F", 12, 15),
    ("Male 13-17, Female 12-15", "F", 12, 15),
    ("Men: 13-17; Women: 12-15", "M", 13, 17),
    ("पुरुष: 13-17 महिला: 12-15", "F", 12, 15),
    ("Female: 12.0-15.5 Male: 13.5-17.5", "M", 13.5, 17.5),
])
def test_sex_specific_ranges(text, sex, low, high):
    r = parse_range(text, sex)
    assert (float(r.low), float(r.high)) == (low, high)


def test_desirable_label_is_picked_from_category_list():
    r = parse_range("Desirable < 200; Borderline 200-239; High >= 240")
    assert r.low is None and float(r.high) == 200 and not r.high_inclusive


# ---------------------------------------------------------------- numeric comparison
@pytest.mark.parametrize("value, range_text, expected", [
    (98, "70-110", "normal"),
    (70, "70-110", "normal"),        # inclusive boundaries
    (110, "70-110", "normal"),
    (69.9, "70-110", "low"),
    (110.01, "70-110", "high"),
    (245, "< 200", "high"),
    (199.99, "< 200", "normal"),
    (200, "< 200", "high"),          # strict '<': 200 is not < 200
    (200, "<= 200", "normal"),
    (150, "< 150", "high"),
    (130, "Up to 130", "normal"),
    (162, "Up to 130", "high"),
    (38, ">= 40", "low"),
    (40, ">= 40", "normal"),
    (40, "> 40", "low"),             # strict '>'
    (6.1, "4.0 - 5.6", "high"),
    (5.6, "4.0 - 5.6", "normal"),
    (5.60000001, "4.0 - 5.6", "high"),
    (150000, "1,50,000 - 4,10,000", "normal"),
    (149999, "1,50,000 - 4,10,000", "low"),
    (0, "-2 - +2", "normal"),
    (-3, "-2 - +2", "low"),
    (0, "0 - 5", "normal"),
])
def test_numeric_status(value, range_text, expected):
    assert flag_test(row(value, range_text))["status"] == expected


def test_decimal_boundary_is_exact():
    # Compared as Decimal, not float: 0.3 vs 0.3 is exactly equal (float maths could drift)
    assert flag_test(row(0.3, "0 - 0.3"))["status"] == "normal"


def test_output_fields_are_filled():
    t = flag_test(row(245, "< 200"))
    assert t["range_low"] is None and t["range_high"] == 200.0
    assert t["range_high_inclusive"] is False
    assert "245 > 200" in t["flag_reason"] and "printed: < 200" in t["flag_reason"]


def test_input_row_is_not_mutated():
    r = row(245, "< 200")
    flag_test(r)
    assert r["status"] == "unknown" and r["range_high"] is None


# ---------------------------------------------------------------- missing / unusable data
def test_missing_range_is_unknown():
    t = flag_test(row(18.2, None, unit="ng/mL"))
    assert t["status"] == "unknown" and "no reference range printed" in t["flag_reason"]


def test_missing_value_is_unknown():
    t = flag_test(row(None, "70-110", value_text=None))
    assert t["status"] == "unknown" and "no numeric value" in t["flag_reason"]


def test_sex_specific_without_sex_is_unknown_and_with_sex_decides():
    r = row(12.8, "M: 13 - 17  F: 12 - 15", unit="g/dL")
    assert flag_test(r)["status"] == "unknown"
    assert flag_test(r, "M")["status"] == "low"
    assert flag_test(r, "F")["status"] == "normal"


# ---------------------------------------------------------------- units
def test_unit_mismatch_range_unit_is_unknown():
    t = flag_test(row(5.4, "70-110", unit="mmol/L", range_unit="mg/dL"))
    assert t["status"] == "unknown" and "unit mismatch" in t["flag_reason"]


def test_unit_mismatch_inline_unit_is_unknown():
    t = flag_test(row(98, "3.9-6.1 mmol/L", unit="mg/dL"))
    assert t["status"] == "unknown" and "unit mismatch" in t["flag_reason"]


@pytest.mark.parametrize("unit, range_unit", [
    ("mg/dL", "mg/dl"), ("µIU/mL", "uIU/mL"), ("μIU/mL", "µIU/ml"),
    ("mcg/dL", "µg/dL"), ("/cumm", "/µL"), ("g/dL", "gm/dL"),
])
def test_equivalent_unit_spellings_are_not_a_mismatch(unit, range_unit):
    assert flag_test(row(5, "1-10", unit=unit, range_unit=range_unit))["status"] == "normal"


def test_trailing_word_after_range_is_not_treated_as_unit():
    assert flag_test(row(98, "70-110 Normal"))["status"] == "normal"


# ---------------------------------------------------------------- censored and qualitative values
@pytest.mark.parametrize("value_text, range_text, expected", [
    ("<0.1", "0.27 - 4.20", "low"),     # every possible value < 0.1 is below 0.27
    ("<0.1", "< 5", "normal"),
    (">1000", "< 200", "high"),
    (">1000", "70-110", "high"),
    ("<5", "2 - 10", "unknown"),         # could be 1 (low) or 3 (normal): don't guess
    (">50", ">= 40", "normal"),
])
def test_censored_values(value_text, range_text, expected):
    assert flag_test(row(None, range_text, value_text=value_text))["status"] == expected


@pytest.mark.parametrize("value_text, range_text, expected", [
    ("Negative", "Negative", "normal"),
    ("NEGATIVE", "negative", "normal"),
    ("Nil", "Negative/Nil", "normal"),
    ("Absent", "Nil", "normal"),
    ("Positive", "Negative", "unknown"),  # differs -> needs review (schema has no 'abnormal')
    ("Trace", "Nil", "unknown"),
    ("Negative", None, "unknown"),
])
def test_qualitative_values(value_text, range_text, expected):
    assert flag_test(row(None, range_text, value_text=value_text, unit=None))["status"] == expected


def test_low_confidence_adds_verify_note_but_still_flags():
    t = flag_test(row(245, "< 200", confidence=0.4))
    assert t["status"] == "high" and "please verify" in t["flag_reason"]


# ---------------------------------------------------------------- full document (real Gemini output)
FIXTURE = json.loads((HERE / "fixtures" / "gemini_fake_lab_report_01.json").read_text(encoding="utf-8"))

EXPECTED_NO_SEX = {
    "Total Cholesterol": "high",          # 245, < 200
    "HDL Cholesterol": "low",             # 38, >= 40
    "LDL Cholesterol": "high",            # 162, Up to 130
    "Triglycerides": "high",              # 150, < 150 (strict)
    "Fasting Blood Sugar": "normal",      # 98, 70 - 110
    "HbA1c": "high",                      # 6.1, 4.0 - 5.6
    "Haemoglobin / हीमोग्लोबिन": "unknown",  # sex-specific range, sex not given
    "TSH": "normal",                      # 2.45, 0.27 - 4.20
    "Platelet Count": "normal",           # 1,50,000 at the inclusive lower bound
    "Vitamin D (25-OH)": "unknown",       # no range printed
    "Urine Sugar": "unknown",             # qualitative, no range printed
}


def test_real_gemini_output_without_sex():
    out = flag_lab_result(FIXTURE["parsed"])
    assert {t["name"]: t["status"] for t in out["tests"]} == EXPECTED_NO_SEX
    assert out["flagging"]["abnormal_count"] == 5


def test_real_gemini_output_with_patient_sex_m():
    out = flag_lab_result(FIXTURE["parsed"], "M")
    hb = next(t for t in out["tests"] if t["name"].startswith("Haemoglobin"))
    assert hb["status"] == "low" and hb["range_low"] == 13.0
    assert out["flagging"]["abnormal_count"] == 6


def test_flagged_document_still_matches_team_contract():
    out = flag_lab_result(FIXTURE["parsed"], "M")
    errors = list(Draft202012Validator(CONTRACT, format_checker=FormatChecker()).iter_errors(out))
    assert errors == [], [e.message for e in errors]


# ---------------------------------------------------------------- formats found while probing
@pytest.mark.parametrize("value, range_text, unit, expected", [
    (12, "10-20 mg / dL", "mg/dL", "normal"),                                   # spaced unit
    (7.0, "<5.7 Normal, 5.7-6.4 Prediabetes, >=6.5 Diabetes", "%", "high"),     # trailing labels (HbA1c)
    (5.5, "<5.7 Normal, 5.7-6.4 Prediabetes, >=6.5 Diabetes", "%", "normal"),
    (180, "<200 Desirable, 200-239 Borderline High, >=240 High", "mg/dL", "normal"),
    (98, "Non-fasting: 70-140", "mg/dL", "normal"),
    (5, "Normal: 1-10; Abnormal: >10", None, "normal"),
    (250000, "1.5-4.1 lakhs/cumm", "/cumm", "unknown"),                         # no lakh conversion
])
def test_formats_found_while_probing(value, range_text, unit, expected):
    assert flag_test(row(value, range_text, unit=unit))["status"] == expected
