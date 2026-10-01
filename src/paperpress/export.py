"""The export stage: OCR'd issues -> a research corpus.

Writes into one folder (default <project>/export/):

    pages.jsonl   one record per page: metadata + citation + text
    pages.csv     the same, for spreadsheets / R / Stata (UTF-8 with BOM so
                  Excel opens it correctly; Excel cuts cells at 32,767
                  characters, and a dense newspaper page can be longer)
    issues.csv    one row per issue, no text: a manifest of the corpus
    txt/          with txt=True: <title>_<date>_pNN.txt, one file per page
    README.txt    what's in the folder and where it came from

The folder is built beside the destination and swapped in at the end, so a
failed export never leaves a half-written corpus.
"""
from __future__ import annotations

import csv
import json
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

from . import __version__
from .project import Project, read_issue

PAGE_FIELDS = ["id", "title", "title_name", "date", "date_precision", "volume", "number",
               "page", "pages_in_issue", "words", "citation", "source", "source_id",
               "source_url", "source_file", "image", "ocr_engine", "text"]
ISSUE_FIELDS = ["title", "title_name", "date", "date_precision", "volume", "number", "pages",
                "words", "source", "source_id", "source_url", "source_file", "rights",
                "folder", "ocr_engine"]


def human_date(iso: str | None, precision: str | None) -> str | None:
    if not iso:
        return None
    d = date.fromisoformat(iso)
    return f"{d:%B} {d.year}" if precision == "month" else f"{d:%B} {d.day}, {d.year}"


def citation(title_name: str, rec: dict, page: int) -> str:
    """A footnote-ready reference, e.g. "The Woman's Journal, February 3, 1912, p. 2"."""
    parts = [title_name]
    when = human_date(rec.get("date"), rec.get("date_precision"))
    if rec.get("volume"):
        parts.append(f"vol. {rec['volume']}" + (f", no. {rec['number']}" if rec.get("number")
                                                  else ""))
    parts.append(when or "undated")
    return ", ".join(parts) + f", p. {page}"


def _source_fields(rec: dict, page: dict | None = None) -> dict:
    src = rec.get("source", {})
    url = src.get("url")
    if url and page is not None and src.get("type") == "internet_archive" \
            and page.get("source_leaf") is not None:
        url = f"{url}/page/n{page['source_leaf']}"
    return {"source": src.get("type"), "source_id": src.get("id"), "source_url": url,
            "source_file": src.get("path")}


def issue_rows(project: Project, slugs: list[str]):
    """(issue_dir, issue record, full_text) for every OCR'd issue, plus the issue
    folders skipped because they haven't been OCR'd yet."""
    skipped = []
    out = []
    for slug in slugs:
        for d in project.issue_dirs(slug, include_undated=True):
            ft = d / "full_text.json"
            if not ft.exists():
                skipped.append(d)
                continue
            out.append((d, read_issue(d), json.loads(ft.read_text())))
    return out, skipped


def export(project: Project, slugs: list[str], dest: Path, *, txt: bool = False) -> dict:
    issues, skipped = issue_rows(project, slugs)
    tmp = dest.with_name(dest.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    n_pages = n_words = 0
    try:
        if txt:
            (tmp / "txt").mkdir()
        with (tmp / "pages.jsonl").open("w", encoding="utf-8") as jl, \
             (tmp / "pages.csv").open("w", encoding="utf-8-sig", newline="") as pc, \
             (tmp / "issues.csv").open("w", encoding="utf-8-sig", newline="") as ic:
            pages_csv = csv.DictWriter(pc, PAGE_FIELDS)
            issues_csv = csv.DictWriter(ic, ISSUE_FIELDS)
            pages_csv.writeheader()
            issues_csv.writeheader()
            for d, rec, full in issues:
                title = project.titles[rec["title"]]
                key = d.name          # the date, date__source-id, or (undated) the source id
                engine = full.get("ocr", {}).get("engine")
                page_meta = {p["page"]: p for p in rec["pages"]}
                issue_words = 0
                for pg in full["pages"]:
                    words = len(pg["text"].split())
                    issue_words += words
                    meta = page_meta.get(pg["page"], {})
                    row = {
                        "id": f"{title.slug}_{key}_p{pg['page']:02d}",
                        "title": title.slug, "title_name": title.name,
                        "date": rec["date"], "date_precision": rec.get("date_precision"),
                        "volume": rec.get("volume"), "number": rec.get("number"),
                        "page": pg["page"], "pages_in_issue": len(full["pages"]),
                        "words": words, "citation": citation(title.name, rec, pg["page"]),
                        **_source_fields(rec, meta),
                        "image": str((d / meta["image"]).relative_to(project.root))
                                 if meta.get("image") else None,
                        "ocr_engine": engine, "text": pg["text"],
                    }
                    jl.write(json.dumps(row, ensure_ascii=False) + "\n")
                    pages_csv.writerow(row)
                    if txt:
                        (tmp / "txt" / f"{row['id']}.txt").write_text(pg["text"] + "\n",
                                                                       encoding="utf-8")
                    n_pages += 1
                n_words += issue_words
                issues_csv.writerow({
                    "title": title.slug, "title_name": title.name, "date": rec["date"],
                    "date_precision": rec.get("date_precision"), "volume": rec.get("volume"),
                    "number": rec.get("number"), "pages": len(full["pages"]),
                    "words": issue_words, **_source_fields(rec),
                    "rights": rec.get("source", {}).get("rights"),
                    "folder": str(d.relative_to(project.root)), "ocr_engine": engine,
                })
        (tmp / "README.txt").write_text(_readme(project, slugs, len(issues), n_pages, n_words,
                                                txt))
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)
    return {"issues": len(issues), "pages": n_pages, "words": n_words,
            "skipped": [str(d.relative_to(project.root)) for d in skipped]}


def _readme(project: Project, slugs, n_issues, n_pages, n_words, txt) -> str:
    titles = "\n".join(f"  {project.titles[s].name} ({s})" for s in slugs)
    return f"""{project.name}: text corpus
Exported {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by paperpress {__version__}

{n_issues} issues, {n_pages} pages, {n_words:,} words from:
{titles}

pages.jsonl   one JSON record per page (same columns as pages.csv)
pages.csv     one row per page; `text` is the page's OCR in reading order
issues.csv    one row per issue, without text
{"txt/          one plain-text file per page, named by the `id` column" if txt else ""}

Columns
  id              title_date_pNN, unique per page
  date            YYYY-MM-DD (date_precision "month" means the day is unknown)
  citation        a ready-to-use reference, e.g. "Title, March 4, 1960, p. 1"
  source_url      the page at its source (Internet Archive), if online
  source_file     the original PDF, for issues added from your own files
  image           the page image, relative to the project folder
  ocr_engine      the software that produced the text

The text is machine OCR and will contain errors. Check quotations against the page
images (the `image` and `source_url` columns) before citing them.
"""
