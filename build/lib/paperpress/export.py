"""The export stage: OCR'd issues -> a research corpus.

Writes into one folder (default <project>/export/):

    pages.jsonl   one record per page: metadata + citation + text
    pages.csv     the same, for spreadsheets / R / Stata (UTF-8 with BOM so
                  Excel opens it correctly; Excel cuts cells at 32,767
                  characters, and a dense newspaper page can be longer)
    issues.csv    one row per issue, no text: a manifest of the corpus
    articles.jsonl / articles.csv
                  one record per article, for issues with a table of contents
                  (`paperpress enrich`); mastheads are left out, ads are kept
                  and flagged
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
from .folios import printed, printed_pages
from .project import Project, read_issue

PAGE_FIELDS = ["id", "title", "title_name", "date", "date_precision", "volume", "number",
               "page", "printed_page", "pages_in_issue", "words", "citation", "source", "source_id",
               "source_url", "source_file", "image", "ocr_engine", "text"]
ARTICLE_FIELDS = ["id", "title", "title_name", "date", "date_precision", "volume", "number",
                  "headline", "author", "author_confidence", "type", "section", "language",
                  "is_advertisement", "start_page", "pages", "printed_pages", "continued", "words", "citation",
                  "source_url", "enrich_model", "text"]
ISSUE_FIELDS = ["title", "title_name", "date", "date_precision", "volume", "number", "pages",
                "words", "source", "source_id", "source_url", "source_file", "rights",
                "folder", "ocr_engine"]


def human_date(iso: str | None, precision: str | None) -> str | None:
    if not iso:
        return None
    d = date.fromisoformat(iso)
    return f"{d:%B} {d.year}" if precision == "month" else f"{d:%B} {d.day}, {d.year}"


def page_span(pages: int | list[int]) -> str:
    """"p. 3", "pp. 5–7" for consecutive pages, "pp. 1, 6" for a story that jumps."""
    pages = sorted(set(pages)) if isinstance(pages, list) else [pages]
    if len(pages) == 1:
        return f"p. {pages[0]}"
    runs = [[pages[0], pages[0]]]               # [first, last] of each consecutive run
    for p in pages[1:]:
        if p == runs[-1][1] + 1:
            runs[-1][1] = p
        else:
            runs.append([p, p])
    return "pp. " + ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in runs)


def _quotable(headline: str) -> str:
    """A headline ready to sit inside double quotes: drop quotes wrapping the whole
    thing and trailing punctuation, and make inner double quotes single (Chicago)."""
    h = headline.strip().rstrip(".,")
    if len(h) > 1 and h[0] in "\"“" and h[-1] in "\"”":
        h = h[1:-1].strip()
    return h.replace("“", "‘").replace("”", "’").replace('"', "'").rstrip(".,")


def citation(title_name: str, rec: dict, page: int | list[int],
             headline: str | None = None) -> str:
    """A footnote-ready reference, e.g. "The Woman's Journal, February 3, 1912, p. 2",
    or for an article '"Headline," The Suffragist, ..., pp. 5–7'."""
    parts = [f'"{_quotable(headline)},"' if headline else None, title_name]
    when = human_date(rec.get("date"), rec.get("date_precision"))
    if rec.get("volume"):
        parts.append(f"vol. {rec['volume']}" + (f", no. {rec['number']}" if rec.get("number")
                                                  else ""))
    parts.append(when or "undated")
    return (parts[0] + " " if parts[0] else "") + ", ".join(parts[1:]) + f", {page_span(page)}"


def _source_fields(rec: dict, page: dict | None = None) -> dict:
    src = rec.get("source", {})
    url = src.get("url")
    # source_index is the page's position at IA (/page/n<index>); issues fetched by
    # paperpress 0.1 stored that position as source_leaf
    index = page.get("source_index", page.get("source_leaf")) if page else None
    if url and index is not None and src.get("type") == "internet_archive":
        url = f"{url}/page/n{index}"
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
    n_pages = n_words = n_articles = 0
    try:
        if txt:
            (tmp / "txt").mkdir()
        with (tmp / "pages.jsonl").open("w", encoding="utf-8") as jl, \
             (tmp / "pages.csv").open("w", encoding="utf-8-sig", newline="") as pc, \
             (tmp / "issues.csv").open("w", encoding="utf-8-sig", newline="") as ic, \
             (tmp / "articles.jsonl").open("w", encoding="utf-8") as aj, \
             (tmp / "articles.csv").open("w", encoding="utf-8-sig", newline="") as ac:
            pages_csv = csv.DictWriter(pc, PAGE_FIELDS)
            issues_csv = csv.DictWriter(ic, ISSUE_FIELDS)
            articles_csv = csv.DictWriter(ac, ARTICLE_FIELDS)
            pages_csv.writeheader()
            issues_csv.writeheader()
            articles_csv.writeheader()
            for d, rec, full in issues:
                title = project.titles[rec["title"]]
                key = d.name          # the date, date__source-id, or (undated) the source id
                engine = full.get("ocr", {}).get("engine")
                numbering = printed_pages(d, rec)
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
                        "page": pg["page"],
                        "printed_page": numbering["pages"].get(pg["page"]),
                        "pages_in_issue": len(full["pages"]),
                        "words": words,
                        "citation": citation(title.name, rec, printed(numbering, pg["page"])),
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
                n_articles += _write_articles(d, rec, title, page_meta, numbering, aj,
                                              articles_csv)
                issues_csv.writerow({
                    "title": title.slug, "title_name": title.name, "date": rec["date"],
                    "date_precision": rec.get("date_precision"), "volume": rec.get("volume"),
                    "number": rec.get("number"), "pages": len(full["pages"]),
                    "words": issue_words, **_source_fields(rec),
                    "rights": rec.get("source", {}).get("rights"),
                    "folder": str(d.relative_to(project.root)), "ocr_engine": engine,
                })
        if not n_articles:
            (tmp / "articles.jsonl").unlink()
            (tmp / "articles.csv").unlink()
        (tmp / "README.txt").write_text(_readme(project, slugs, len(issues), n_pages, n_words,
                                                txt, n_articles))
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)
    return {"issues": len(issues), "pages": n_pages, "words": n_words, "articles": n_articles,
            "skipped": [str(d.relative_to(project.root)) for d in skipped]}


def _write_articles(d: Path, rec: dict, title, page_meta: dict, numbering: dict, jl,
                    writer) -> int:
    """Write one issue's articles (from toc.json, if it has one). Returns the count."""
    from .enrich import article_text

    toc_file = d / "toc.json"
    if not toc_file.exists():
        return 0
    toc = json.loads(toc_file.read_text())
    cache: dict = {}
    n = 0
    for a in toc.get("articles", []):
        if a.get("type") == "masthead":
            continue
        text = article_text(d, a, cache)
        row = {
            "id": f"{title.slug}_{d.name.replace('/', '_')}_{a['id']}",
            "title": title.slug, "title_name": title.name,
            "date": rec["date"], "date_precision": rec.get("date_precision"),
            "volume": rec.get("volume"), "number": rec.get("number"),
            "headline": a["title"], "author": a.get("author"),
            "author_confidence": a.get("author_confidence"), "type": a.get("type"),
            "section": a.get("section"), "language": a.get("language"),
            "is_advertisement": a.get("is_advertisement", False),
            "start_page": a["start_page"], "pages": a["pages"],
            "printed_pages": [numbering["pages"][p] for p in a["pages"]]
                             if numbering["pages"] else None,
            "continued": a.get("continued", False), "words": len(text.split()),
            "citation": citation(title.name, rec, printed(numbering, a["pages"]),
                                 None if a["title"].startswith("[") else a["title"]),
            "source_url": _source_fields(rec, page_meta.get(a["start_page"], {}))["source_url"],
            "enrich_model": toc.get("enrich", {}).get("model"), "text": text,
        }
        jl.write(json.dumps(row, ensure_ascii=False) + "\n")
        writer.writerow({**row, "pages": " ".join(map(str, a["pages"])),
                         "printed_pages": " ".join(map(str, row["printed_pages"] or []))})
        n += 1
    return n


def _readme(project: Project, slugs, n_issues, n_pages, n_words, txt, n_articles=0) -> str:
    titles = "\n".join(f"  {project.titles[s].name} ({s})" for s in slugs)
    files = ["pages.jsonl   one JSON record per page (same columns as pages.csv)",
             "pages.csv     one row per page; `text` is the page's OCR in reading order",
             "issues.csv    one row per issue, without text"]
    if n_articles:
        files.append(f"articles.jsonl, articles.csv   {n_articles:,} articles from the LLM table "
                     f"of contents\n              (paperpress enrich): headline, author, type, "
                     f"section, text.\n              Headlines and authors are machine-read too.")
    if txt:
        files.append("txt/          one plain-text file per page, named by the `id` column")
    return f"""{project.name}: text corpus
Exported {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by paperpress {__version__}

{n_issues} issues, {n_pages} pages, {n_words:,} words from:
{titles}

""" + "\n".join(files) + """

Columns
  id              title_date_pNN, unique per page
  page            the page's position in the issue (1 = first scan)
  printed_page    the page number printed on the page, when it could be read from
                  the page itself (folios) or the source; citations use it
  date            YYYY-MM-DD (date_precision "month" means the day is unknown)
  citation        a ready-to-use reference, e.g. "Title, March 4, 1960, p. 1"
  source_url      the page at its source (Internet Archive), if online
  source_file     the original PDF, for issues added from your own files
  image           the page image, relative to the project folder
  ocr_engine      the software that produced the text

The text is machine OCR and will contain errors. Check quotations against the page
images (the `image` and `source_url` columns) before citing them.
"""
