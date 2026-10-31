"""
MedChain deterministic lab flagging (Member 2).

The AI only TRANSCRIBES a report (value_text, unit, range_text exactly as printed).
THIS code decides high / low / normal / unknown. No AI, no medical knowledge,
no default ranges: if the report did not print a usable range, status is "unknown".

Handles
  ranges      "70-110", "70 - 110", "70 to 110", "4.0 – 5.6", "1,50,000 - 4,10,000", "-2 - +2"
  one-sided   "< 200", "<= 200", "≤200", "up to 200", "upto 5.0", "less than 200", "below 5",
              "max 200", "200 max", "> 40", ">= 12", "≥12", "above 40", "at least 12", "min 12"
  Hindi       "200 तक", "200 से कम", "40 से अधिक", Devanagari digits (१२३)
  units       "70-110 mg/dL" (inline unit), range_unit vs unit mismatch -> unknown (no conversion)
  multi       "M: 13-17  F: 12-15" (needs patient_sex), "Desirable < 200; Borderline 200-239"
  values      plain numbers, decimals, "<0.1" / ">1000" (censored), qualitative ("Negative")

Usage
  from flag_abnormal import flag_lab_result, flag_test
  python automation/ai/flagging/flag_abnormal.py extraction.json [--sex M] [--out flagged.json]
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

FLAGGER_VERSION = "flag_abnormal@1.0.0"
LOW_CONFIDENCE = 0.6  # below this, the reason says "please verify"

# ---------------------------------------------------------------- text normalisation
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_REPLACEMENTS = [
    ("≤", "<="), ("≥", ">="), ("=<", "<="), ("=>", ">="), ("＜", "<"), ("＞", ">"),
    ("–", "-"), ("—", "-"), ("−", "-"), ("~", "-"), ("μ", "µ"), (" ", " "),
]
_SLASH_SPACES = re.compile(r"(?<=[A-Za-zµ%])\s*/\s*(?=[A-Za-zµ])")  # "mg / dL" -> "mg/dL"


def _normalize(text: str) -> str:
    t = text.translate(_DEVANAGARI_DIGITS)
    for a, b in _REPLACEMENTS:
        t = t.replace(a, b)
    t = _SLASH_SPACES.sub("/", t)
    return re.sub(r"\s+", " ", t).strip()


# A number: optional sign, Indian or western thousands commas, optional decimals.
NUM = r"[-+]?(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d+)?|[-+]?\.\d+"


def _to_decimal(s: str) -> Decimal:
    return Decimal(s.replace(",", "").replace("+", ""))


# ---------------------------------------------------------------- range model
@dataclass
class Range:
    low: Decimal | None = None
    high: Decimal | None = None
    low_inclusive: bool = True
    high_inclusive: bool = True
    unit: str | None = None  # unit printed inside range_text, if any

    def describe(self) -> str:
        if self.low is not None and self.high is not None:
            return f"{_fmt(self.low)}–{_fmt(self.high)}"
        if self.high is not None:
            return f"{'≤' if self.high_inclusive else '<'} {_fmt(self.high)}"
        return f"{'≥' if self.low_inclusive else '>'} {_fmt(self.low)}"


def _fmt(d: Decimal) -> str:
    """Decimal -> short string without scientific notation ('150000', '5.6')."""
    s = format(d.normalize(), "f")
    return s


# Each pattern: (regex for the range body, builder(match) -> Range). Tried in order.
_UNIT = r"(?:\s*(?P<unit>[^\s\d<>=:;,()\-][^\s;,()]*))?(?:\s+(?P<tail>[^\d<>=]*))?"
_LABEL = r"(?P<label>[^\d<>=]*?)"


def _p(body: str) -> re.Pattern:
    return re.compile(rf"^{_LABEL}\s*{body}{_UNIT}\s*$", re.I)


_PATTERNS: list[tuple[re.Pattern, callable]] = [
    # "200 max", "200 तक", "200 से कम", "40 से अधिक", "12 and above"
    (_p(rf"(?P<n>{NUM})\s*(?P<op>max(?:imum)?\.?|and below|or less|तक|से कम|min(?:imum)?\.?|and above|or more|से अधिक|से ज़्यादा|से ज्यादा)(?=\s|$)"),
     lambda m: _postfix(m)),
    # "70-110", "70 to 110", "100 से 200"
    (_p(rf"(?P<a>{NUM})\s*(?:-|to|से)\s*(?P<b>{NUM})"),
     lambda m: Range(low=_to_decimal(m["a"]), high=_to_decimal(m["b"]))),
    # "< 200", "<= 200", "up to 200", "less than 200", "max 200"
    (_p(rf"(?P<op><=|<|less than or equal to|less than|below|under|not more than|up ?to|till|max(?:imum)?\.?:?)\s*(?P<n>{NUM})"),
     lambda m: Range(high=_to_decimal(m["n"]), high_inclusive=_upper_inclusive(m["op"]))),
    # "> 40", ">= 12", "above 40", "at least 12", "min 12"
    (_p(rf"(?P<op>>=|>|more than|greater than|above|over|at least|not less than|min(?:imum)?\.?:?)\s*(?P<n>{NUM})"),
     lambda m: Range(low=_to_decimal(m["n"]), low_inclusive=_lower_inclusive(m["op"]))),
]


def _upper_inclusive(op: str) -> bool:
    op = op.lower()
    return not (op.startswith("<") and not op.startswith("<=")) and op not in ("less than", "below", "under")


def _lower_inclusive(op: str) -> bool:
    op = op.lower()
    return not (op.startswith(">") and not op.startswith(">=")) and op not in ("more than", "greater than", "above", "over")


def _postfix(m: re.Match) -> Range:
    n, op = _to_decimal(m["n"]), m["op"].lower()
    if op.startswith("max") or op in ("and below", "or less", "तक"):
        return Range(high=n)
    if op == "से कम":
        return Range(high=n, high_inclusive=False)
    if op in ("से अधिक", "से ज़्यादा", "से ज्यादा"):
        return Range(low=n, low_inclusive=False)
    return Range(low=n)  # min, and above, or more


def _parse_segment(seg: str) -> tuple[str, Range] | None:
    seg = re.sub(r"\((?![\d<>])[^)]*\)\s*$", "", seg).strip()   # drop trailing note: "(Fasting)"
    seg = seg.strip("[]() ").strip()
    for rx, build in _PATTERNS:
        m = rx.match(seg)
        if m:
            r = build(m)
            unit, tail = m.groupdict().get("unit"), (m.groupdict().get("tail") or "").strip()
            label = m["label"].strip(" :=-")
            # A word after the range is a label, not a unit: "<5.7 Normal", "70-110 Desirable"
            if unit and not _looks_like_unit(unit):
                tail, unit = f"{unit} {tail}".strip(), None
            r.unit = unit
            if r.low is not None and r.high is not None and r.low > r.high:
                return None  # "110-70": malformed, don't guess
            return (label or tail).strip(" :=-").lower(), r
    return None


# ---------------------------------------------------------------- multi-range handling
_SEX_WORD = r"(?:[Mm]ales?|[Ff]emales?|MALES?|FEMALES?|[Mm]en|[Ww]omen|MEN|WOMEN|पुरुष|महिला|स्त्री|M|F)"
_SEX_SPLIT = re.compile(rf"(?<![\wऀ-ॿ]){_SEX_WORD}(?![\wऀ-ॿ])(?=\s*[:(\-]|\s+(?=[\d<>]))")
_MALE = {"m", "male", "males", "men", "पुरुष"}
_FEMALE = {"f", "female", "females", "women", "महिला", "स्त्री"}
_NORMAL_LABELS = {"normal", "desirable", "optimal", "reference", "ref", "ref range", "normal range",
                  "reference range", "biological reference range", "सामान्य"}


def _split_segments(text: str) -> list[str]:
    parts = re.split(r"[;|\n]|,(?!\d)", text)
    out: list[str] = []
    for p in parts:
        idx = [m.start() for m in _SEX_SPLIT.finditer(p)]
        if len(idx) > 1 or (idx and idx[0] > 0):
            bounds = ([0] if idx[0] > 0 else []) + idx + [len(p)]
            out += [p[a:b] for a, b in zip(bounds, bounds[1:])]
        else:
            out.append(p)
    return [s.strip() for s in out if s.strip()]


def _label_sex(label: str) -> str | None:
    label = label.strip(" :()-").lower()
    if label in _MALE:
        return "M"
    if label in _FEMALE:
        return "F"
    return None


class RangeError(Exception):
    """Range could not be used; message becomes flag_reason."""


def parse_range(range_text: str | None, patient_sex: str | None = None) -> Range:
    """Turn printed range text into a Range, or raise RangeError explaining why not."""
    if range_text is None or not str(range_text).strip():
        raise RangeError("no reference range printed on report")
    text = _normalize(str(range_text))
    segments = [(s, _parse_segment(s)) for s in _split_segments(text)]
    parsed = [(lbl, r) for _, res in segments if res for lbl, r in [res]]
    if not parsed:
        raise RangeError(f"could not read range '{range_text}'")

    sexed = [(lbl, r) for lbl, r in parsed if _label_sex(lbl)]
    if sexed:
        if not patient_sex:
            raise RangeError("sex-specific range; patient sex not provided")
        want = patient_sex.strip().upper()[:1]
        match = [r for lbl, r in sexed if _label_sex(lbl) == want]
        if len(match) != 1:
            raise RangeError(f"no single range printed for sex '{want}'")
        return match[0]

    if len(parsed) == 1:
        return parsed[0][1]
    normal = [r for lbl, r in parsed if lbl in _NORMAL_LABELS]
    if len(normal) == 1:
        return normal[0]
    raise RangeError(f"multiple ranges printed ('{range_text}'); needs review")


# ---------------------------------------------------------------- units
_UNIT_ALIASES = {
    "mcg": "µg", "ug": "µg", "uiu": "µiu", "uu": "µiu", "µu": "µiu", "ul": "µl",
    "cumm": "µl", "cu.mm": "µl", "mm3": "µl", "mm^3": "µl", "cmm": "µl",
    "gm": "g", "gms": "g", "mgs": "mg", "mg%": "mg/dl",
}


def _norm_unit(u: str | None) -> str | None:
    if not u:
        return None
    u = u.strip().lower().replace(" ", "").replace("μ", "µ")
    parts = [_UNIT_ALIASES.get(p, p) for p in u.split("/")]
    return "/".join(parts) or None


_UNITISH = {"mg", "g", "ng", "pg", "µg", "iu", "µiu", "miu", "u", "mmol", "µmol", "meq", "fl", "sec", "s", "%"}


def _looks_like_unit(u: str | None) -> bool:
    """Ignore trailing words like 'Normal' printed after a range; only real units are compared."""
    n = _norm_unit(u)
    return bool(n and ("/" in n or "%" in n or "^" in n or n in _UNITISH))


def _units_conflict(test_unit: str | None, range_unit: str | None) -> bool:
    a, b = _norm_unit(test_unit), _norm_unit(range_unit)
    return bool(a and b and a != b)


# ---------------------------------------------------------------- values
_CENSORED = re.compile(rf"^(?P<op><=|<|>=|>)\s*(?P<n>{NUM})$")
_QUAL_NEG = {"negative", "nil", "absent", "not detected", "non reactive", "non-reactive", "nonreactive", "neg"}


def _compare(v: Decimal, r: Range) -> tuple[str, str]:
    if r.low is not None and (v < r.low or (v == r.low and not r.low_inclusive)):
        return "low", f"{_fmt(v)} {'<' if v < r.low else 'is not >'} {_fmt(r.low)}"
    if r.high is not None and (v > r.high or (v == r.high and not r.high_inclusive)):
        return "high", f"{_fmt(v)} {'>' if v > r.high else 'is not <'} {_fmt(r.high)}"
    return "normal", f"{_fmt(v)} within {r.describe()}"


def _compare_censored(op: str, x: Decimal, r: Range) -> tuple[str, str] | None:
    """Value printed as '<x' or '>x'. Only decide when every possible value gives the same answer."""
    below = op.startswith("<")
    strict = op in ("<", ">")
    if below:  # true value v < x (or <= x)
        if r.low is not None and (x < r.low or (x == r.low and (strict or not r.low_inclusive))):
            return "low", f"'{op}{_fmt(x)}' is below {_fmt(r.low)}"
        if r.low is None and r.high is not None and (x < r.high or (x == r.high and (strict or r.high_inclusive))):
            return "normal", f"'{op}{_fmt(x)}' within {r.describe()}"
    else:      # true value v > x (or >= x)
        if r.high is not None and (x > r.high or (x == r.high and (strict or not r.high_inclusive))):
            return "high", f"'{op}{_fmt(x)}' is above {_fmt(r.high)}"
        if r.high is None and r.low is not None and (x > r.low or (x == r.low and (strict or r.low_inclusive))):
            return "normal", f"'{op}{_fmt(x)}' within {r.describe()}"
    return None


def _decimal_or_none(x) -> Decimal | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        return Decimal(str(x))
    except InvalidOperation:
        return None


# ---------------------------------------------------------------- public API
def flag_test(test: dict, patient_sex: str | None = None) -> dict:
    """Return a copy of one test row with range_low/high, inclusivity, status and flag_reason set."""
    t = copy.deepcopy(test)
    t.update(range_low=None, range_high=None, range_low_inclusive=True, range_high_inclusive=True)
    printed = f" (printed: {t.get('range_text')})" if t.get("range_text") else ""

    def done(status: str, reason: str) -> dict:
        if (t.get("confidence") is not None) and t["confidence"] < LOW_CONFIDENCE:
            reason += " — low read confidence, please verify"
        t["status"], t["flag_reason"] = status, reason
        return t

    value_text = (t.get("value_text") or "").strip()
    range_text = (t.get("range_text") or "").strip()

    # Qualitative result vs qualitative range: "Negative" vs "Negative/Nil"
    if t.get("value") is None and value_text and not re.search(r"\d", value_text):
        if range_text and not re.search(r"\d", range_text):
            v, r = _normalize(value_text).lower(), _normalize(range_text).lower()
            if v == r or (v in _QUAL_NEG and any(w in r for w in _QUAL_NEG)):
                return done("normal", f"'{value_text}' matches printed reference '{range_text}'")
            return done("unknown", f"'{value_text}' differs from printed reference '{range_text}'; needs review")
        return done("unknown", "qualitative result; no comparable range printed")

    try:
        rng = parse_range(t.get("range_text"), patient_sex)
    except RangeError as e:
        return done("unknown", str(e))

    t["range_low"] = float(rng.low) if rng.low is not None else None
    t["range_high"] = float(rng.high) if rng.high is not None else None
    t["range_low_inclusive"], t["range_high_inclusive"] = rng.low_inclusive, rng.high_inclusive

    for other in (rng.unit if _looks_like_unit(rng.unit) else None, t.get("range_unit")):
        if _units_conflict(t.get("unit"), other):
            return done("unknown", f"unit mismatch: result in {t.get('unit')}, range in {other}; not converted")

    v = _decimal_or_none(t.get("value"))
    if v is not None:
        status, why = _compare(v, rng)
        return done(status, why + printed)

    m = _CENSORED.match(_normalize(value_text)) if value_text else None
    if m:
        res = _compare_censored(m["op"], _to_decimal(m["n"]), rng)
        if res:
            return done(res[0], res[1] + printed)
        return done("unknown", f"'{value_text}' cannot be compared with {rng.describe()}")

    return done("unknown", "no numeric value to compare")


def flag_lab_result(doc: dict, patient_sex: str | None = None) -> dict:
    """Flag every test in a contracts/lab-result.schema.json document. Returns a new document."""
    out = copy.deepcopy(doc)
    out["tests"] = [flag_test(t, patient_sex) for t in doc.get("tests", [])]
    out["flagging"] = {
        "flagged_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "flagger_version": FLAGGER_VERSION,
        "abnormal_count": sum(t["status"] in ("high", "low") for t in out["tests"]),
    }
    return out


# ---------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="Flag abnormal lab values (deterministic, no AI)")
    ap.add_argument("file", help="JSON from extract.py (with 'parsed') or a bare lab-result document")
    ap.add_argument("--sex", choices=["M", "F"], help="patient sex, for sex-specific ranges")
    ap.add_argument("--out", help="write JSON here instead of stdout")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):  # Windows consoles: print Hindi names safely
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    data = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
    doc = data.get("parsed", data)
    flagged = flag_lab_result(doc, args.sex)

    text = json.dumps(flagged, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    print(f"\n{'TEST':<28}{'VALUE':>12}  {'RANGE':<24}STATUS", file=sys.stderr)
    for t in flagged["tests"]:
        print(f"{t['name'][:27]:<28}{(t.get('value_text') or ''):>12}  {(t.get('range_text') or '-')[:23]:<24}"
              f"{t['status'].upper():<8} {t['flag_reason']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
