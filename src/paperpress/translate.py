"""The optional translate stage: an LLM translates each page, region by region.

Each OCR region (a headline, a paragraph, a caption) is translated on its own
and keyed by its region id, so the reader can show the translation beside the
scan with the same boxes as the original. The result is translation.json in
the issue folder:

    {"title": ..., "date": ..., "target": "English",
     "translate": {"model": ..., "source": ..., "at": ...},
     "pages": [{"page": 1, "regions": {"r0": "...", ...}, "text": "..."}]}

`text` is the page's translation in reading order, like full_text.json's.
Nothing else depends on it: with a translation.json, the site adds a
translation to each page and export adds a `translation` column.

Pages are sent in chunks of regions (long pages would overrun a reply), and
each chunk's result is cached under .paperpress/translate/, keyed by model,
target language and the chunk's text, so an interrupted run resumes without
paying twice and an edited region is the only one redone.
"""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .enrich import EnrichError, ask_json, load_profile, profile_block
from .ocr import NO_TEXT_STATUSES, region_text
from .project import Project, page_name, read_issue

DEFAULT_TRANSLATE = {
    "model": "google/gemini-3.8-flash",
    "target": "English",
    "source": None,                         # the original's language; None: the model works it out
    "base_url": "https://openrouter.ai/api/v1",
    "api_key_env": "OPENROUTER_API_KEY",
}
CHUNK_CHARS = 6000          # source characters per call


class TranslateError(EnrichError):
    pass


def page_regions(page: dict) -> list[tuple[str, str, str]]:
    """(id, label, text) for every region with text, in reading order."""
    return [(r["id"], r.get("label", ""), t) for r in page.get("regions", [])
            if r.get("status") not in NO_TEXT_STATUSES and (t := region_text(r.get("text", "")))]


def chunks(regions: list, limit: int = CHUNK_CHARS) -> list[list]:
    out, cur, size = [], [], 0
    for reg in regions:
        if cur and size + len(reg[2]) > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(reg)
        size += len(reg[2])
    return out + [cur] if cur else out


def chunk_prompt(regions: list, *, page: int, title_name: str, kind: str, date: str | None,
                 source: str | None, target: str, profile: dict | None = None) -> str:
    lang = f"from {source} " if source else ""
    listing = "\n".join(f"[{rid}] ({label}) {text}" for rid, label, text in regions)
    return f"""Translate these regions of page {page} of {title_name} ({kind}, {date or 'undated'}) \
{lang}into {target}.
The text is machine OCR of an old scan, in reading order, and contains OCR errors: translate
what the writer evidently meant. Where a passage is too garbled to read, translate what you
can and write [illegible] for the rest. Translate everything, including headlines, ads and
passages in other languages; never summarize, shorten or add commentary. Keep personal names,
place names and organization names as printed (with their diacritics), translating only the
words around them. A region that is only numbers or a name is copied as it is.
{profile_block(profile, date)}
Return ONLY a JSON object mapping every region id to its translation:
{{"{regions[0][0]}": "<translation>", ...}}

REGIONS:
{listing}
"""


def translate_issue(project: Project, issue_dir: Path, llm: Callable, settings: dict, *,
                    force: bool = False, workers: int = 6) -> dict:
    rec = read_issue(issue_dir)
    title = project.titles[rec["title"]]
    profile = load_profile(project, title.slug)
    pages = []
    for p in rec["pages"]:
        f = issue_dir / f"{page_name(p['page'])}.json"
        if not f.exists():
            raise TranslateError(f"{issue_dir.name}: page {p['page']} isn't OCR'd yet")
        pages.append(json.loads(f.read_text()))

    key = str(issue_dir.relative_to(project.title_dir(title.slug)))
    cache_dir = project.root / ".paperpress" / "translate" / title.slug / key
    model, target, source = settings["model"], settings["target"], settings.get("source")

    jobs = [(pg["page"], n, ch) for pg in pages
            for n, ch in enumerate(chunks(page_regions(pg)), start=1)]

    def one(job) -> dict:
        n_page, n, regions = job
        sig = hashlib.sha1(json.dumps([model, target, source, regions],
                                      ensure_ascii=False).encode()).hexdigest()[:16]
        cached = cache_dir / f"{page_name(n_page)}_{n:02d}.json"
        if cached.exists() and not force:
            c = json.loads(cached.read_text())
            if c.get("sig") == sig:
                return c["regions"]
        ids = [r[0] for r in regions]
        prompt = chunk_prompt(regions, page=n_page, title_name=title.name, kind=title.kind,
                              date=rec.get("date"), source=source, target=target,
                              profile=profile)
        data = ask_json(llm, prompt, model,
                        lambda d: all(isinstance(d.get(i), str) for i in ids))
        out = {i: data[i].strip() for i in ids}
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps({"sig": sig, "regions": out}, ensure_ascii=False))
        return out

    with ThreadPoolExecutor(max_workers=min(workers, len(jobs) or 1)) as pool:
        results = list(pool.map(one, jobs))
    by_page: dict[int, dict] = {pg["page"]: {} for pg in pages}
    for (n_page, _, _), out in zip(jobs, results):
        by_page[n_page].update(out)

    doc = {
        "title": title.slug,
        "date": rec.get("date"),
        "target": target,
        "translate": {"model": model, "source": source,
                      "at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        "pages": [{"page": pg["page"], "regions": by_page[pg["page"]],
                   "text": "\n\n".join(t for rid, _, _ in page_regions(pg)
                                       if (t := by_page[pg["page"]].get(rid)))}
                  for pg in pages],
    }
    tmp = issue_dir / "translation.json.tmp"
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(issue_dir / "translation.json")
    return {"pages": len(pages), "regions": sum(len(v) for v in by_page.values()),
            "calls": len(jobs)}


def is_done(issue_dir: Path) -> bool:
    return (issue_dir / "translation.json").exists()


def load(issue_dir: Path) -> dict | None:
    """translation.json, or None."""
    f = issue_dir / "translation.json"
    return json.loads(f.read_text()) if f.exists() else None


def page_translation(doc: dict | None, page: int) -> dict | None:
    """One page's entry ({"regions": ..., "text": ...}) from translation.json."""
    if not doc:
        return None
    return next((p for p in doc.get("pages", []) if p["page"] == page), None)
