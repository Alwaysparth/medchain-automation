"""Tests for compress_image.py. Generates FAKE images in memory; no network.

Run from repo root:  pytest automation/ai/compression -q
"""
import hashlib
import io
import random
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import compress_image as ci  # noqa: E402

SAMPLE_REPORT = HERE.parents[1] / "samples" / "fake_lab_report_01.png"


# ---------------------------------------------------------------- fake input builders
def to_bytes(img: Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def fake_phone_photo(w=4032, h=3024) -> Image.Image:
    """Paper-coloured background with text-like strokes and sensor noise, like a camera photo."""
    random.seed(1)
    img = Image.new("RGB", (w, h), (236, 232, 222))
    d = ImageDraw.Draw(img)
    for y in range(200, h - 200, 90):
        for x in range(200, w - 600, 260):
            d.rectangle((x, y, x + random.randint(80, 220), y + 28), fill=(40, 40, 45))
    noise = Image.effect_noise((w, h), 6).convert("RGB")
    return Image.blend(img, noise, 0.08)


def exif_with_gps_and_rotation() -> Image.Exif:
    exif = Image.Exif()
    exif[0x0112] = 6                                  # Orientation: rotate 90° CW to display
    exif[0x010F] = "FakePhone"                        # Make
    exif[0x8825] = {1: "N", 2: (28.0, 36.0, 0.0), 3: "E", 4: (77.0, 12.0, 0.0)}  # GPS (fake Delhi)
    return exif


# ---------------------------------------------------------------- type detection
@pytest.mark.parametrize("fmt, expected", [("JPEG", "jpeg"), ("PNG", "png"), ("WEBP", "webp"), ("PDF", "pdf")])
def test_detect_type_from_bytes(fmt, expected):
    assert ci.detect_type(to_bytes(Image.new("RGB", (20, 20), "white"), fmt)) == expected


@pytest.mark.parametrize("data", [
    b"MZ\x90\x00 this is an exe",
    b"<html><script>alert(1)</script>",
    b"just some text pretending to be report.jpg",
    b"GIF89a....",
])
def test_rejects_non_documents_even_with_image_name(data):
    with pytest.raises(ci.CompressionError) as e:
        ci.compress_document(data, "report.jpg")
    assert e.value.code == "UNSUPPORTED_FILE"


def test_rejects_empty_and_too_large(monkeypatch):
    with pytest.raises(ci.CompressionError) as e:
        ci.compress_document(b"")
    assert e.value.code == "CORRUPT_FILE"
    monkeypatch.setattr(ci, "MAX_FILE_BYTES", 1000)
    with pytest.raises(ci.CompressionError) as e:
        ci.compress_document(b"\xff\xd8\xff" + b"0" * 2000)
    assert e.value.code == "FILE_TOO_LARGE"


def test_truncated_jpeg_is_corrupt():
    data = to_bytes(fake_phone_photo(800, 600), "JPEG", quality=90)
    with pytest.raises(ci.CompressionError) as e:
        ci.compress_document(data[: len(data) // 3])
    assert e.value.code == "CORRUPT_FILE"


def test_decompression_bomb_rejected_before_decoding(monkeypatch):
    monkeypatch.setattr(ci, "MAX_PIXELS", 10_000)
    with pytest.raises(ci.CompressionError) as e:
        ci.compress_document(to_bytes(Image.new("RGB", (200, 200)), "PNG"))
    assert e.value.code == "IMAGE_TOO_LARGE"


# ---------------------------------------------------------------- hashes
def test_original_hash_is_sha256_of_untouched_bytes():
    data = to_bytes(fake_phone_photo(1200, 900), "JPEG", quality=92)
    r = ci.compress_document(data)
    assert r.original_sha256 == hashlib.sha256(data).hexdigest()
    assert r.stored_sha256 == hashlib.sha256(r.stored_bytes).hexdigest()
    assert r.original_sha256 != r.stored_sha256


def test_same_input_gives_same_output_and_hash():
    data = to_bytes(fake_phone_photo(1000, 750), "JPEG", quality=92)
    a, b = ci.compress_document(data), ci.compress_document(data)
    assert a.original_sha256 == b.original_sha256 and a.stored_sha256 == b.stored_sha256


# ---------------------------------------------------------------- phone photos
def test_large_phone_photo_is_downscaled_compressed_and_visually_lossless():
    data = to_bytes(fake_phone_photo(), "JPEG", quality=95)
    r = ci.compress_document(data, "photo.jpg")
    assert r.action == "compressed_webp" and r.stored_mime == "image/webp"
    assert max(r.width, r.height) == ci.MAX_SIDE
    assert (r.original_width, r.original_height) == (4032, 3024)
    assert r.stored_size_bytes < r.original_size_bytes
    assert r.psnr_db >= ci.MIN_PSNR
    assert Image.open(io.BytesIO(r.stored_bytes)).format == "WEBP"
    assert any("Downscaled" in w for w in r.warnings)


def test_small_photo_is_not_upscaled():
    r = ci.compress_document(to_bytes(fake_phone_photo(1600, 1200), "JPEG", quality=95))
    assert (r.width, r.height) == (1600, 1200)


def test_exif_rotation_applied_and_gps_metadata_removed():
    img = fake_phone_photo(1200, 900)  # stored landscape, EXIF says "rotate to portrait"
    data = to_bytes(img, "JPEG", quality=92, exif=exif_with_gps_and_rotation())
    assert Image.open(io.BytesIO(data)).getexif().get(0x8825)  # GPS really is in the input

    r = ci.compress_document(data)
    out = Image.open(io.BytesIO(r.stored_bytes))
    assert (out.width, out.height) == (900, 1200)               # now upright portrait
    assert r.metadata_stripped is True
    assert not out.getexif() and "exif" not in out.info         # no GPS, no camera model
    assert b"FakePhone" not in r.stored_bytes


def test_cmyk_jpeg_converted():
    data = to_bytes(fake_phone_photo(600, 400).convert("CMYK"), "JPEG", quality=92)
    r = ci.compress_document(data)
    assert Image.open(io.BytesIO(r.stored_bytes)).mode == "RGB"


# ---------------------------------------------------------------- PNG / screenshots
def test_png_screenshot_tries_lossless_and_keeps_smaller():
    img = Image.new("RGB", (1240, 1500), "white")
    d = ImageDraw.Draw(img)
    for y in range(100, 1400, 40):
        d.text((60, y), "Total Cholesterol   245   mg/dL   < 200", fill="black")
    r = ci.compress_document(to_bytes(img, "PNG"), "screenshot.png")
    assert r.psnr_db >= ci.MIN_PSNR
    lossless = ci._encode_webp(img, None)
    assert r.stored_size_bytes <= len(lossless)


def test_transparent_png_keeps_alpha_and_opaque_rgba_drops_it():
    clear = Image.new("RGBA", (100, 100), (255, 255, 255, 0))
    ImageDraw.Draw(clear).rectangle((20, 20, 80, 80), fill=(0, 0, 0, 255))
    assert Image.open(io.BytesIO(ci.compress_document(to_bytes(clear, "PNG")).stored_bytes)).mode == "RGBA"
    opaque = Image.new("RGBA", (100, 100), (255, 255, 255, 255))
    assert Image.open(io.BytesIO(ci.compress_document(to_bytes(opaque, "PNG")).stored_bytes)).mode == "RGB"


def test_quality_falls_back_when_90_is_not_good_enough(monkeypatch):
    monkeypatch.setattr(ci, "MIN_PSNR", 99)  # impossible for lossy -> must end lossless
    r = ci.compress_document(to_bytes(fake_phone_photo(400, 300), "JPEG", quality=95))
    assert r.webp_quality is None and r.to_dict()["lossless"] is True


@pytest.mark.skipif(not SAMPLE_REPORT.exists(), reason="sample report not present")
def test_fake_lab_report_sample():
    data = SAMPLE_REPORT.read_bytes()
    r = ci.compress_document(data, SAMPLE_REPORT.name)
    assert r.psnr_db >= ci.MIN_PSNR and r.stored_size_bytes < r.original_size_bytes


# ---------------------------------------------------------------- PDFs
def test_pdf_stored_as_is_with_thumbnail():
    pages = [Image.new("RGB", (1240, 1754), "white") for _ in range(3)]
    ImageDraw.Draw(pages[0]).text((100, 100), "FAKE LAB REPORT", fill="black")
    buf = io.BytesIO()
    pages[0].save(buf, "PDF", save_all=True, append_images=pages[1:])
    data = buf.getvalue()

    r = ci.compress_document(data, "report.pdf")
    assert r.action == "pdf_as_is" and r.stored_bytes == data
    assert r.stored_sha256 == r.original_sha256
    if ci.pdfium is not None:
        assert r.pages == 3
        thumb = Image.open(io.BytesIO(r.thumbnail_bytes))
        assert thumb.format == "WEBP" and max(thumb.size) <= ci.THUMB_SIDE


def test_broken_pdf_is_corrupt():
    if ci.pdfium is None:
        pytest.skip("pypdfium2 not installed")
    with pytest.raises(ci.CompressionError) as e:
        ci.compress_document(b"%PDF-1.4\n this is not really a pdf")
    assert e.value.code == "CORRUPT_FILE"


# ---------------------------------------------------------------- summary
def test_to_dict_is_json_safe_and_has_contract_fields():
    import json
    d = ci.compress_document(to_bytes(fake_phone_photo(800, 600), "JPEG", quality=92)).to_dict()
    json.dumps(d)
    for k in ("original_sha256", "stored_sha256", "original_size_bytes", "stored_size_bytes", "stored_mime", "pages"):
        assert k in d
