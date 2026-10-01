"""The OCR stage: newspaper-ocr over every page of every issue.

Per issue folder it writes:

    page_NN.json     newspaper-ocr's page JSON (regions in reading order, with
                     bbox / label / text / status), plus page, image and an
                     `ocr` block recording the engine, settings and timing
    full_text.json   the issue's text, page by page; written last, so its
                     presence means the issue's OCR is complete

Pages already OCR'd are skipped, so an interrupted run resumes where it
stopped. The page JSON is kept exactly as newspaper-ocr produced it; text
clean-up (rejoining hyphenated line breaks) happens only in full_text.json.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from .project import page_name, read_issue

# The default engine for now: works on any laptop with Tesseract installed.
# Switch per project in paper.toml's [ocr] table (e.g. MinerU on a CUDA GPU).
DEFAULT_OCR = {
    "detector": "doclayout_yolo",
    "recognizer": "tesseract",
    "recognizer_model": "news_combo_fast",
}
ENGINE_KEYS = {"detector", "recognizer", "recognizer_model", "hole_fill_detector", "device"}
NO_TEXT_STATUSES = {"timeout", "error"}     # their `text` is a placeholder, not page text


class Engine(Protocol):
    def describe(self) -> dict: ...
    def page(self, image: Path) -> dict: ...


class NewspaperOCR:
    """newspaper-ocr's Pipeline, returning the page JSON as a dict."""

    def __init__(self, settings: dict | None = None):
        unknown = set(settings or {}) - ENGINE_KEYS
        if unknown:
            raise ValueError(f"unknown [ocr] setting(s): {', '.join(sorted(unknown))}")
        self.settings = {**DEFAULT_OCR, **(settings or {})}
        from newspaper_ocr import Pipeline

        self.pipe = Pipeline(output="json", **self.settings)

    def describe(self) -> dict:
        from importlib.metadata import version

        return {"engine": f"newspaper-ocr {version('newspaper-ocr')}", **self.settings}

    def page(self, image: Path) -> dict:
        return json.loads(self.pipe.run(image))


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def is_done(issue_dir: Path) -> bool:
    return (issue_dir / "full_text.json").exists()


def region_text(text: str) -> str:
    """One region's OCR as running text: rejoin words hyphenated across line
    breaks, then turn the remaining line breaks into spaces."""
    text = re.sub(r"(\w)-\s*\n\s*([a-z])", r"\1\2", text or "")
    return re.sub(r"\s+", " ", text).strip()


def page_text(page: dict) -> str:
    return "\n\n".join(t for r in page.get("regions", [])
                       if r.get("status") not in NO_TEXT_STATUSES
                       and (t := region_text(r.get("text", ""))))


def ocr_issue(issue_dir: Path, engine: Engine, *, force: bool = False,
              log: Callable[[str], None] = lambda m: None) -> dict:
    """OCR one issue folder. Returns a summary: pages, done (newly OCR'd),
    seconds, regions, flagged (regions whose status isn't ok)."""
    rec = read_issue(issue_dir)
    about = engine.describe()
    pages, done, seconds = [], 0, 0.0
    for p in rec["pages"]:
        out = issue_dir / f"{page_name(p['page'])}.json"
        if out.exists() and not force:
            pages.append(json.loads(out.read_text()))
            continue
        log(f"page {p['page']}/{len(rec['pages'])}")
        t = time.monotonic()
        data = engine.page(issue_dir / p["image"])
        took = time.monotonic() - t
        data = {"page": p["page"], "image": p["image"], **data,
                "ocr": {**about, "seconds": round(took, 1),
                        "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}}
        _atomic_write_json(out, data)
        pages.append(data)
        done += 1
        seconds += took

    regions = [r for pg in pages for r in pg.get("regions", [])]
    flagged = sum(1 for r in regions if r.get("status") != "ok")
    _atomic_write_json(issue_dir / "full_text.json", {
        "title": rec["title"],
        "date": rec["date"],
        "ocr": about,
        "pages": [{"page": pg["page"], "text": page_text(pg),
                   "regions": len(pg.get("regions", [])),
                   "flagged": sum(1 for r in pg.get("regions", []) if r.get("status") != "ok")}
                  for pg in pages],
    })
    return {"pages": len(pages), "done": done, "seconds": seconds,
            "regions": len(regions), "flagged": flagged}
