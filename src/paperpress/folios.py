"""Printed page numbers: what a citation should say.

The page's position in the issue (page_NN) is not always its printed number.
Magazines and many weeklies number pages through the whole volume, so the
sixth page of an issue may be printed "46". Citations need the printed number.

Within an issue, printed numbers almost always run consecutively, so the job is
to find one offset (printed = position + offset). Evidence, one vote per page:

  - folios in our own OCR: numbers in short regions in the top or bottom strip
    of the page (skipping the issue's year and day of month, which also live
    in running heads)
  - the source's own guess where it has one and is confident (Internet
    Archive's page_numbers.json, stored per page as source_page_number)

The offset wins only with at least two votes and twice as many as any rival,
so a stray number in a footer can't renumber an issue. With no clear winner
there is no printed numbering, and citations use the page's position in the
issue instead.

A person can settle it: set "printed_offset" in the issue's issue.json.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

from .project import page_name

TOP, BOTTOM = 0.09, 0.93           # header / footer strips, as a fraction of page height
MAX_FOLIO_REGION = 120             # characters: folios live in short regions
MIN_SOURCE_PROB = 80               # IA pageProb needed for its guess to count
NUMBER = re.compile(r"(?<![\d,.])(\d{1,4})(?![\d,])")


def folio_candidates(page: dict, date: str | None) -> set[int]:
    height = page.get("height") or 0
    skip = set()
    if date:
        skip = {int(date[:4]), int(date[8:10])} if len(date) >= 10 else {int(date[:4])}
    found = set()
    for r in page.get("regions", []):
        b, text = r.get("bbox") or {}, r.get("text") or ""
        if not height or len(text) > MAX_FOLIO_REGION:
            continue
        if b.get("y1", height) < TOP * height or b.get("y0", 0) > BOTTOM * height:
            found.update(n for n in map(int, NUMBER.findall(text)) if n and n not in skip)
    return found


def infer_offset(votes_by_page: dict[int, set[int]]) -> tuple[int | None, int]:
    """(offset, votes) from {position: {candidate printed numbers}}."""
    votes = Counter()
    for pos, cands in votes_by_page.items():
        for offset in {c - pos for c in cands if c - pos >= 0}:
            votes[offset] += 1
    if not votes:
        return None, 0
    (best, n), *rest = votes.most_common(2) + [(None, 0)]
    runner_up = rest[0][1]
    if n >= 2 and n >= 2 * runner_up:
        return best, n
    return None, n


def _evidence(issue_dir: Path, rec: dict) -> dict[int, set[int]]:
    out: dict[int, set[int]] = {}
    for p in rec["pages"]:
        n = p["page"]
        cands: set[int] = set()
        f = issue_dir / f"{page_name(n)}.json"
        if f.exists():
            cands |= folio_candidates(json.loads(f.read_text()), rec.get("date"))
        num, prob = p.get("source_page_number"), p.get("source_page_prob")
        if num and str(num).isdigit() and (prob or 0) >= MIN_SOURCE_PROB:
            cands.add(int(num))
        out[n] = cands
    return out


def printed_pages(issue_dir: Path, rec: dict) -> dict:
    """{"offset": int|None, "votes": int, "how": "set"|"detected"|None,
        "pages": {position: printed number}} for one issue."""
    if isinstance(rec.get("printed_offset"), int):
        offset, votes, how = rec["printed_offset"], 0, "set"
    else:
        stamp = tuple((issue_dir / f"{page_name(p['page'])}.json").stat().st_mtime_ns
                      if (issue_dir / f"{page_name(p['page'])}.json").exists() else 0
                      for p in rec["pages"])
        offset, votes = _cached_offset(str(issue_dir), json.dumps(rec["pages"]),
                                       rec.get("date"), stamp)
        how = "detected" if offset is not None else None
    pages = {p["page"]: p["page"] + offset for p in rec["pages"]} if offset is not None else {}
    return {"offset": offset, "votes": votes, "how": how, "pages": pages}


@lru_cache(maxsize=4096)
def _cached_offset(issue_dir: str, pages_json: str, date: str | None, _stamp: tuple):
    rec = {"pages": json.loads(pages_json), "date": date}
    return infer_offset(_evidence(Path(issue_dir), rec))


def printed(numbering: dict, positions: int | list[int]) -> int | list[int]:
    """Map page position(s) to printed numbers where known, else keep positions."""
    pages = numbering.get("pages") or {}
    if isinstance(positions, list):
        return [pages.get(p, p) for p in positions]
    return pages.get(positions, positions)
