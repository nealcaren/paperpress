"""Bring-your-own PDFs: one PDF file = one issue, dated from its file name.

Page images: when a page is a single full-page JPEG at or below the target
resolution (the usual case for microfilm-derived PDFs), the original JPEG bytes
are copied out untouched. Otherwise the page is rendered at the target ppi
(never above the scan's own resolution, so nothing is upsampled).

A text layer, if the PDF has one, is saved to source/pdf_text.txt for
comparison only; paperpress's own OCR is what the archive uses.
"""
from __future__ import annotations

import hashlib
import io
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import __version__
from ..dates import date_from_filename, infer_filename_format
from ..project import Project, page_name, staged_issue, write_issue

DEFAULT_PPI = 300
GRAY, RGB = 1, 2                 # pdfium colorspace codes we can pass through as JPEG


class PDFError(Exception):
    pass


@dataclass
class PlannedPDF:
    path: Path
    date: str | None
    problem: str | None = None   # why this file will be skipped


def find_pdfs(paths: list[Path]) -> list[Path]:
    out = []
    for p in paths:
        if p.is_dir():
            out += [f for f in p.rglob("*") if f.suffix.lower() == ".pdf"
                    and not any(part.startswith(".") for part in f.relative_to(p).parts)]
        elif p.suffix.lower() == ".pdf":
            out.append(p)
        else:
            raise PDFError(f"{p}: not a PDF or a folder")
    return sorted(set(out), key=lambda f: (f.name, str(f)))


def plan(paths: list[Path], date_format: str | None = None) -> tuple[str, list[PlannedPDF]]:
    """Decide the date of every PDF before touching any of them.

    Returns the date format used (inferred from all the file names together
    unless given) and one PlannedPDF per file. Empty files still count toward
    inferring the format (their names are evidence) but are flagged rather than
    added, since a 0-byte "PDF" is a failed copy or download.
    """
    files = find_pdfs(paths)
    if not files:
        raise PDFError("no PDF files found")
    if not date_format:
        date_format = infer_filename_format([f.name for f in files])
    planned = []
    for f in files:
        if f.stat().st_size == 0:
            planned.append(PlannedPDF(f, None, "empty file (0 bytes)"))
            continue
        d = date_from_filename(f.name, date_format)
        planned.append(PlannedPDF(f, d, None if d else f"no {date_format} date in name"))
    return date_format or "", planned


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _passthrough_jpeg(page, target_ppi: int | None):
    """The page's original JPEG if the page is just that one image, else None."""
    import pypdfium2.raw as raw

    if page.get_rotation():
        return None
    images = list(page.get_objects(filter=(raw.FPDF_PAGEOBJ_IMAGE,), max_depth=1))
    if len(images) != 1:
        return None
    img = images[0]
    pw, ph = page.get_size()
    left, bottom, right, top = img.get_bounds()
    if (right - left) * (top - bottom) < 0.9 * pw * ph:
        return None
    a, b, c, d, _, _ = img.get_matrix().get()
    if b or c or a <= 0 or d <= 0:              # rotated or mirrored placement
        return None
    meta = img.get_metadata()
    if img.get_filters() != ["DCTDecode"] or meta.colorspace not in (GRAY, RGB):
        return None
    native = meta.width / ((right - left) / 72)
    if target_ppi and native > target_ppi * 1.05:
        return None                             # needs downsampling: render instead
    buf = io.BytesIO()
    img.extract(buf)
    return buf.getvalue(), meta.width, meta.height, round(native)


def _native_ppi(page) -> float | None:
    """Resolution of the largest image on the page, if it has any."""
    import pypdfium2.raw as raw

    best = None
    for img in page.get_objects(filter=(raw.FPDF_PAGEOBJ_IMAGE,), max_depth=1):
        left, _, right, _ = img.get_bounds()
        if right > left:
            ppi = img.get_metadata().width / ((right - left) / 72)
            best = max(best or 0, ppi)
    return best


def page_image(page, target_ppi: int | None) -> tuple[bytes, str, int, int, int, str]:
    """(image bytes, extension, width, height, ppi, how) for one PDF page."""
    found = _passthrough_jpeg(page, target_ppi)
    if found:
        data, w, h, ppi = found
        return data, "jpg", w, h, ppi, "extracted"
    native = _native_ppi(page)
    ppi = target_ppi or native or DEFAULT_PPI
    if native:
        ppi = min(ppi, native)                  # never upsample
    pil = page.render(scale=ppi / 72).to_pil()
    if pil.mode != "L" and _looks_gray(pil):
        pil = pil.convert("L")
    buf = io.BytesIO()
    pil.save(buf, "JPEG", quality=90)
    return buf.getvalue(), "jpg", pil.width, pil.height, round(ppi), "rendered"


def _looks_gray(pil) -> bool:
    from PIL import ImageChops

    r, g, b = pil.convert("RGB").resize((64, 64)).split()
    return max(ImageChops.difference(r, g).getextrema()[1],
               ImageChops.difference(g, b).getextrema()[1]) < 12


def add_pdf(project: Project, title_slug: str, path: Path, date: str | None, *,
            ppi: int | None = DEFAULT_PPI, copy: bool = False,
            force: bool = False) -> tuple[str, Path]:
    """Add one PDF as an issue. Returns (status, issue_dir).

    The PDF's sha256 identifies it, so re-adding the same file (even renamed
    or moved) is skipped.
    """
    import pypdfium2 as pdfium

    digest = sha256(path)
    existing = project.find_issue(title_slug, digest)
    if existing and not force:
        return "skipped", existing
    try:
        pdf = pdfium.PdfDocument(path)
    except pdfium.PdfiumError as e:
        raise PDFError(f"{path.name}: not a readable PDF ({e})") from e

    dest = existing or project.new_issue_dir(title_slug, date, path.stem)
    try:
        with staged_issue(dest) as tmp:
            pages, texts = [], []
            for n in range(1, len(pdf) + 1):
                page = pdf[n - 1]
                data, ext, w, h, page_ppi, how = page_image(page, ppi)
                name = f"{page_name(n)}.{ext}"
                (tmp / "images" / name).write_bytes(data)
                pages.append({"page": n, "image": f"images/{name}", "width": w, "height": h,
                              "ppi": page_ppi, "image_from": how})
                texts.append(page.get_textpage().get_text_bounded())
            if not pages:
                raise PDFError(f"{path.name}: PDF has no pages")
            ocr_file = None
            if sum(len(t.split()) for t in texts) >= 20 * len(texts):
                (tmp / "source" / "pdf_text.txt").write_text("\f".join(texts))
                ocr_file = "source/pdf_text.txt"
            copied = None
            if copy:
                shutil.copy2(path, tmp / "source" / "original.pdf")
                copied = "source/original.pdf"
            write_issue(tmp, {
                "title": title_slug,
                "date": date,
                "date_precision": "day" if date else None,
                "date_from": "filename" if date else None,
                "volume": None,
                "number": None,
                "source": {
                    "type": "pdf",
                    "id": digest,
                    "filename": path.name,
                    "path": str(path.resolve()),
                    "bytes": path.stat().st_size,
                    "sha256": digest,
                    "copy": copied,
                    "ocr": ocr_file,
                },
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "fetched_by": f"paperpress {__version__}",
                "pages": pages,
            })
    finally:
        pdf.close()
    return ("added" if date else "added-undated"), dest
