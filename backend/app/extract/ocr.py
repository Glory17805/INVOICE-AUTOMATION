"""Optional OCR, for documents that carry no text layer.

A scan or a photograph of a bill is a picture of words, not words. Everything
downstream of this module - the label reader, the table parser, the amounts
engine - works on text, so this is the one place that turns pixels into text
and hands them the same kind of input a born-digital PDF gives.

Two deliberate constraints shaped the design:

Nothing here is a hard requirement. Tesseract is a system install, not a
Python package, and a deployment that has not got one must keep working
exactly as it did before rather than failing to import. So every capability is
probed at call time and reported honestly, and `read` returns an empty string
when OCR is unavailable - the same value the caller already handles for a
scan it cannot read.

Poppler is avoided where possible. The usual recipe for a scanned PDF is
pdf2image, which shells out to poppler - a second system install. But a
scanned PDF is nearly always one full-page image per page, and those images
are embedded in the file where pypdf can reach them directly. That path is
tried first and needs nothing but Tesseract; poppler is the fallback for the
unusual file whose pages are drawn rather than placed.
"""

from __future__ import annotations

import logging
import os
import shutil
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("gst.ocr")

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}

# Where Tesseract puts itself on Windows when nobody adds it to PATH, which is
# the default for both the UB-Mannheim installer and winget.
_WINDOWS_CANDIDATES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)

# A scan of a bill is mostly a table. PSM 6 - "a single uniform block of text" -
# keeps rows together far better than the default page-segmentation mode, which
# hunts for columns and interleaves them.
_CONFIG = "--psm 6"

# Below this, a "text layer" is page furniture - a header, a page number - and
# the document is a scan in everything but name.
MEANINGFUL_TEXT = 40


def _binary() -> str | None:
    """The Tesseract executable, or None if this machine has not got one."""
    configured = os.environ.get("GST_TESSERACT_CMD", "").strip()
    if configured:
        return configured if Path(configured).exists() else None
    found = shutil.which("tesseract")
    if found:
        return found
    return next((c for c in _WINDOWS_CANDIDATES if Path(c).exists()), None)


@lru_cache(maxsize=1)
def _probe() -> tuple[bool, str]:
    """Whether OCR can run here, and why not when it cannot.

    Cached because it shells out to a subprocess, and the answer cannot change
    while the process is alive without someone installing software underneath
    it. `refresh()` clears it for the tests and for a deployment that installs
    Tesseract without restarting.
    """
    try:
        import pytesseract
    except ImportError:
        return False, "the pytesseract package is not installed"

    binary = _binary()
    if not binary:
        return False, (
            "no Tesseract engine was found. Install it and OCR turns itself on: "
            "winget install UB-Mannheim.TesseractOCR"
        )

    try:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = binary
        version = str(pytesseract.get_tesseract_version())
    except Exception as exc:  # a broken install is not a crash
        return False, f"Tesseract at {binary} could not be run: {exc}"

    log.info("OCR available: Tesseract %s at %s", version, binary)
    return True, f"Tesseract {version}"


def refresh() -> None:
    """Forget the cached probe, so a newly installed engine is picked up."""
    _probe.cache_clear()


def available() -> bool:
    return _probe()[0]


def status() -> str:
    """One line describing the OCR engine, for the settings screen."""
    ok, detail = _probe()
    return detail if ok else f"OCR unavailable - {detail}."


def _language() -> str:
    return os.environ.get("GST_OCR_LANG", "eng").strip() or "eng"


def _read_image(image) -> str:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = _binary()
    try:
        return pytesseract.image_to_string(image, lang=_language(), config=_CONFIG)
    except Exception as exc:
        log.warning("OCR failed on an image: %s", exc)
        return ""


def _prepare(image):
    """Greyscale and upscale a small image before reading it.

    Tesseract wants roughly 300 DPI. A phone photo of an A4 bill is often
    well under that, and upscaling a small one measurably improves the read
    where downscaling a large one would only cost time.
    """
    try:
        from PIL import Image
        if image.mode not in ("L", "RGB"):
            image = image.convert("RGB")
        image = image.convert("L")
        if min(image.size) < 1000:
            factor = max(2, 1000 // max(1, min(image.size)))
            image = image.resize(
                (image.width * factor, image.height * factor), Image.LANCZOS
            )
        return image
    except Exception:
        return image


def image_text(path: Path) -> str:
    """Read a picture of a document."""
    if not available():
        return ""
    try:
        from PIL import Image
        with Image.open(path) as image:
            return _read_image(_prepare(image))
    except Exception as exc:
        log.warning("Could not open %s as an image: %s", path.name, exc)
        return ""


def _embedded_page_images(path: Path):
    """The images placed on each page, in page order.

    This is the cheap path for a scanned PDF and it needs no poppler. Pages
    with several small images are skipped: those are a logo and a signature on
    a born-digital invoice, not a scan, and running OCR over them would return
    noise that looks like content.
    """
    from pypdf import PdfReader

    for page in PdfReader(str(path)).pages:
        try:
            images = list(page.images)
        except Exception:
            continue
        if len(images) != 1:
            continue
        try:
            yield images[0].image
        except Exception:
            continue


def _rendered_page_images(path: Path):
    """Every page rendered to an image. Needs poppler, so it is the fallback."""
    if not shutil.which("pdftoppm"):
        return
    try:
        from pdf2image import convert_from_path
        yield from convert_from_path(str(path), dpi=300)
    except Exception as exc:
        log.warning("Could not render %s: %s", path.name, exc)


def pdf_text(path: Path) -> str:
    """Read a PDF that has no usable text layer."""
    if not available():
        return ""

    pages = [_read_image(_prepare(image)) for image in _embedded_page_images(path)]
    text = "\n".join(p for p in pages if p.strip())
    if len(text.strip()) >= MEANINGFUL_TEXT:
        return text

    rendered = [_read_image(_prepare(image)) for image in _rendered_page_images(path)]
    return "\n".join(p for p in rendered if p.strip())


def read(path: Path) -> str:
    """OCR any supported file, or an empty string if it cannot be read."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return image_text(path)
    if suffix == ".pdf":
        return pdf_text(path)
    return ""


def unavailable_note(path: Path) -> str:
    """What to tell a person whose scan could not be read."""
    _, detail = _probe()
    kind = "photograph" if path.suffix.lower() in IMAGE_SUFFIXES else "scan"
    return (
        f"This file is a {kind} - there is no text in it to read - and {detail}. "
        "Until then it has to be entered by hand."
    )
