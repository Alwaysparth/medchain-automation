"""
MedChain document compression (Member 2).

Images (JPEG / PNG / WebP / HEIC*) -> WebP, "visually lossless":
  * EXIF rotation applied, then ALL metadata removed (phone photos carry GPS location!)
  * very large photos downscaled so the longest side is <= COMPRESS_MAX_SIDE (default 3000 px,
    still sharp for small print on lab reports)
  * encoded at quality 90, then 95, then lossless, until the result is at least
    COMPRESS_MIN_PSNR dB (default 40) compared with the uncompressed image. PNG sources
    (screenshots, digital reports) also try lossless WebP and keep whichever is smaller.
PDFs -> stored AS IS (already compact; text stays vector and selectable) + a small WebP
thumbnail of page 1 for the timeline UI.

The SHA-256 of the ORIGINAL bytes (before any change) is the document hash that goes to
Solana (contracts/events.md 2.4 `doc_hash`). The stored file has its own `stored_sha256`.

  * HEIC needs:  pip install pillow-heif

Usage
  from compress_image import compress_document
  result = compress_document(file_bytes, filename="report.jpg")
  result.stored_bytes, result.thumbnail_bytes, result.to_dict()

  python automation/ai/compression/compress_image.py photo.jpg report.pdf samples/ --out-dir out
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageChops, ImageOps, ImageStat

try:  # optional: iPhone HEIC photos
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:
    HEIC_SUPPORTED = False

try:  # optional: PDF page count + thumbnail
    import pypdfium2 as pdfium
except ImportError:
    pdfium = None

MAX_FILE_BYTES = int(os.getenv("COMPRESS_MAX_FILE_MB", "20")) * 1024 * 1024
MAX_SIDE = int(os.getenv("COMPRESS_MAX_SIDE", "3000"))
MIN_PSNR = float(os.getenv("COMPRESS_MIN_PSNR", "40"))
MAX_PIXELS = 60_000_000          # refuse "decompression bomb" images (~ 8000 x 7500)
QUALITY_LADDER = (90, 95)        # lossy tries before falling back to lossless
THUMB_SIDE = 800

MIME = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
        "heic": "image/heic", "pdf": "application/pdf"}


class CompressionError(Exception):
    """`code` matches the error codes in contracts/events.md (UNSUPPORTED_FILE, ...)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class CompressResult:
    original_sha256: str
    original_size_bytes: int
    original_type: str                 # jpeg | png | webp | heic | pdf (detected from bytes)
    stored_bytes: bytes = field(repr=False)
    stored_mime: str = ""
    stored_sha256: str = ""
    action: str = ""                   # compressed_webp | pdf_as_is
    width: int | None = None
    height: int | None = None
    original_width: int | None = None
    original_height: int | None = None
    pages: int | None = None
    webp_quality: int | None = None    # None = lossless
    psnr_db: float | None = None       # inf = pixel-identical
    metadata_stripped: bool = False
    thumbnail_bytes: bytes | None = field(default=None, repr=False)
    warnings: list[str] = field(default_factory=list)

    @property
    def stored_size_bytes(self) -> int:
        return len(self.stored_bytes)

    def to_dict(self) -> dict:
        """JSON-safe summary (no bytes). Field names follow contracts/events.md 2.4 `file`."""
        return {
            "original_sha256": self.original_sha256,
            "original_size_bytes": self.original_size_bytes,
            "original_type": self.original_type,
            "stored_mime": self.stored_mime,
            "stored_sha256": self.stored_sha256,
            "stored_size_bytes": self.stored_size_bytes,
            "saved_percent": round(100 * (1 - self.stored_size_bytes / self.original_size_bytes), 1),
            "action": self.action,
            "width": self.width, "height": self.height,
            "original_width": self.original_width, "original_height": self.original_height,
            "pages": self.pages,
            "webp_quality": self.webp_quality,
            "lossless": self.action == "compressed_webp" and self.webp_quality is None,
            "psnr_db": None if self.psnr_db is None else ("inf" if math.isinf(self.psnr_db) else round(self.psnr_db, 2)),
            "metadata_stripped": self.metadata_stripped,
            "has_thumbnail": self.thumbnail_bytes is not None,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------- helpers
def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def detect_type(data: bytes) -> str:
    """Decide the file type from its first bytes, never from the file name or claimed MIME."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
        return "heic"
    raise CompressionError("UNSUPPORTED_FILE", "Only JPEG, PNG, WebP, HEIC images and PDFs are accepted")


def psnr(a: Image.Image, b: Image.Image) -> float:
    """Peak signal-to-noise ratio in dB (higher = closer). >= 40 dB is visually identical for documents."""
    stats = ImageStat.Stat(ImageChops.difference(a.convert("RGB"), b.convert("RGB")))
    mse = sum(r * r for r in stats.rms) / len(stats.rms)
    return math.inf if mse == 0 else 10 * math.log10(255 ** 2 / mse)


def _encode_webp(img: Image.Image, quality: int | None) -> bytes:
    buf = io.BytesIO()
    if quality is None:
        img.save(buf, "WEBP", lossless=True, quality=80, method=4)
    else:
        img.save(buf, "WEBP", quality=quality, method=6)
    return buf.getvalue()


def _thumbnail(img: Image.Image) -> bytes:
    t = img.copy()
    t.thumbnail((THUMB_SIDE, THUMB_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    t.save(buf, "WEBP", quality=80, method=6)
    return buf.getvalue()


# ---------------------------------------------------------------- images
def _prepare_image(data: bytes, ftype: str, warnings: list[str]) -> tuple[Image.Image, tuple[int, int], bool]:
    if ftype == "heic" and not HEIC_SUPPORTED:
        raise CompressionError("UNSUPPORTED_FILE", "HEIC photo: install pillow-heif, or send as JPEG")
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_PIXELS:  # checked from header before decoding
            raise CompressionError("IMAGE_TOO_LARGE", f"Image is {img.width}x{img.height}; too many pixels")
        if getattr(img, "n_frames", 1) > 1:
            warnings.append("Animated/multi-frame image: only the first frame was kept")
        img.load()
    except CompressionError:
        raise
    except Exception as exc:
        raise CompressionError("CORRUPT_FILE", f"Could not read image: {exc}") from exc

    had_metadata = bool(img.info.get("exif") or img.getexif() or img.info.get("xmp"))
    original_size = img.size
    img = ImageOps.exif_transpose(img)  # phone photos: apply rotation before EXIF is dropped

    # Normalise colour mode. Keep alpha only if some pixel is actually transparent.
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        if img.getchannel("A").getextrema()[0] == 255:
            img = img.convert("RGB")
    elif img.mode != "RGB":
        img = img.convert("RGB")   # CMYK, L, P, I;16 ... -> RGB

    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
        warnings.append(f"Downscaled from {original_size[0]}x{original_size[1]} to {img.width}x{img.height}")
    return img, original_size, had_metadata


def _compress_image(data: bytes, ftype: str) -> CompressResult:
    warnings: list[str] = []
    img, (ow, oh), had_meta = _prepare_image(data, ftype, warnings)

    # Try candidates in order; keep the smallest one that passes the quality bar.
    candidates: list[tuple[bytes, int | None, float]] = []
    for q in QUALITY_LADDER:
        out = _encode_webp(img, q)
        score = psnr(img, Image.open(io.BytesIO(out)))
        if score >= MIN_PSNR:
            candidates.append((out, q, score))
            break
    if not candidates or ftype == "png":
        out = _encode_webp(img, None)
        candidates.append((out, None, psnr(img, Image.open(io.BytesIO(out)))))
    stored, quality, score = min(candidates, key=lambda c: len(c[0]))

    if len(stored) > len(data):
        warnings.append("Stored WebP is larger than the original (original was already small)")
    return CompressResult(
        original_sha256=sha256_hex(data), original_size_bytes=len(data), original_type=ftype,
        stored_bytes=stored, stored_mime="image/webp", stored_sha256=sha256_hex(stored),
        action="compressed_webp", width=img.width, height=img.height,
        original_width=ow, original_height=oh, pages=1,
        webp_quality=quality, psnr_db=score, metadata_stripped=had_meta,
        thumbnail_bytes=_thumbnail(img), warnings=warnings,
    )


# ---------------------------------------------------------------- PDFs
def _compress_pdf(data: bytes) -> CompressResult:
    warnings: list[str] = []
    pages, thumb = None, None
    if pdfium is None:
        warnings.append("pypdfium2 not installed: no page count or thumbnail")
    else:
        try:
            pdf = pdfium.PdfDocument(data)
            pages = len(pdf)
            if pages:
                page = pdf[0]
                scale = THUMB_SIDE / max(page.get_size())
                thumb = _thumbnail(page.render(scale=scale).to_pil().convert("RGB"))
            pdf.close()
        except Exception as exc:
            if "password" in str(exc).lower():
                raise CompressionError("UNSUPPORTED_FILE", "PDF is password-protected") from exc
            raise CompressionError("CORRUPT_FILE", f"Could not read PDF: {exc}") from exc
    return CompressResult(
        original_sha256=sha256_hex(data), original_size_bytes=len(data), original_type="pdf",
        stored_bytes=data, stored_mime="application/pdf", stored_sha256=sha256_hex(data),
        action="pdf_as_is", pages=pages, thumbnail_bytes=thumb, warnings=warnings,
    )


# ---------------------------------------------------------------- public API
def compress_document(data: bytes, filename: str | None = None) -> CompressResult:
    """Validate and compress one uploaded document. `filename` is only used in messages."""
    if not data:
        raise CompressionError("CORRUPT_FILE", "Empty file")
    if len(data) > MAX_FILE_BYTES:
        raise CompressionError("FILE_TOO_LARGE", f"File is {len(data) // 1024 // 1024} MB; limit is {MAX_FILE_BYTES // 1024 // 1024} MB")
    ftype = detect_type(data)
    return _compress_pdf(data) if ftype == "pdf" else _compress_image(data, ftype)


# ---------------------------------------------------------------- CLI
def _iter_inputs(paths: list[str]):
    for p in map(Path, paths):
        if p.is_dir():
            yield from sorted(f for f in p.iterdir() if f.is_file())
        else:
            yield p


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Compress medical documents to WebP (PDFs kept as is)")
    ap.add_argument("inputs", nargs="+", help="files or folders")
    ap.add_argument("--out-dir", default="compressed_out")
    ap.add_argument("--json", action="store_true", help="print full JSON summaries")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"{'FILE':<34}{'TYPE':<6}{'ORIG KB':>9}{'STORED KB':>11}{'SAVED':>8}  {'PSNR':>7}  ACTION")
    failures = 0
    for path in _iter_inputs(args.inputs):
        try:
            r = compress_document(path.read_bytes(), path.name)
        except CompressionError as e:
            failures += 1
            print(f"{path.name[:33]:<34}{'-':<6}{'':>9}{'':>11}{'':>8}  {'':>7}  REJECTED {e.code}: {e}")
            continue
        ext = ".pdf" if r.stored_mime == "application/pdf" else ".webp"
        (out_dir / f"{path.stem}{ext}").write_bytes(r.stored_bytes)
        if r.thumbnail_bytes:
            (out_dir / f"{path.stem}.thumb.webp").write_bytes(r.thumbnail_bytes)
        d = r.to_dict()
        print(f"{path.name[:33]:<34}{r.original_type:<6}{r.original_size_bytes / 1024:>9.0f}"
              f"{r.stored_size_bytes / 1024:>11.0f}{d['saved_percent']:>7.0f}%  {str(d['psnr_db'] or '-'):>7}  "
              f"{r.action}" + ("" if r.action == "pdf_as_is" else f" q={'lossless' if d['lossless'] else r.webp_quality}"))
        if args.json:
            print(json.dumps(d, indent=2))
    print(f"\nOutput written to {out_dir.resolve()}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
