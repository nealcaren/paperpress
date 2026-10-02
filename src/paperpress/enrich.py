"""The optional enrich stage: an LLM builds each issue's table of contents.

Per page, one LLM call groups newspaper-ocr's regions into articles (headline,
author, type, section, language, advertisement or not). A second, small call
over every article's first and last words links stories that jump to another
page ("Continued on page 4"). The result is toc.json in the issue folder.

Nothing else depends on it: search, reading and export all work from the OCR
alone. With a toc.json, export adds articles.jsonl and the site adds a
table of contents.

Defaults come from the Negro World bake-off (2026-09): a cheap model that
segments faithfully for the per-page pass, a stronger one for the small
stitch pass. Any OpenAI-compatible endpoint works; set [enrich] in paper.toml.

Each page's result is cached under .paperpress/enrich/, keyed by model and
profile, so an interrupted run resumes without paying for pages it already did.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .ocr import NO_TEXT_STATUSES, region_text
from .project import Project, page_name, read_issue, write_issue

DEFAULT_ENRICH = {
    "model": "openai/gpt-5.6-luna",
    "stitch_model": "google/gemini-3.8-flash",
    "base_url": "https://openrouter.ai/api/v1",
    "api_key_env": "OPENROUTER_API_KEY",
}
REGION_TEXT_CAP = 600          # characters of each region shown to the model
NOT_IN_TOC = {"masthead", "advertisement"}
TYPES = ("news", "editorial", "letter", "speech", "report", "poem", "story", "essay", "column",
         "department", "notice", "advertisement", "masthead", "other")

PAGE_SCHEMA = """Return ONLY a JSON object:
{"articles": [
  {"title": "<headline as printed>",
   "author": <string or null>, "author_confidence": "high" | "low" | null,
   "type": "%s",
   "section": <department or column name, or null>,
   "language": "<ISO 639-1 code, e.g. en>",
   "is_advertisement": <true|false>,
   "continued_from": <page number or null>, "continued_to": <page number or null>,
   "region_ids": ["r0", "r3", ...]}
]}
Rules:
- Group the regions into articles. A headline, its decks and subheads, and the body text
  that follows belong to one article. Use region ids exactly as given; every region
  belongs to at most one article. List each article's regions in reading order.
- title: the headline as printed, correcting only obvious OCR errors. If a piece has no
  headline, write a short description in square brackets, e.g. "[Letter to the editor]".
- author: only from a byline ("By ...") or a signature at the end of the text. Never
  guess. Use author_confidence "low" for a signature, "high" for a byline.
- Nameplates, mastheads, page numbers and running heads: type "masthead".
- Advertisements: is_advertisement true and type "advertisement".
- continued_to / continued_from: only when the text says so ("Continued on page 4",
  "(Continued from page 1)").""" % "|".join(TYPES)

STITCH_PROMPT = """These are the articles found on each page of ONE issue of a historical
periodical, with the first and last words of each. Find articles that are the SAME story
continued on another page: use "continued on/from page N" notes and matching topics.
Most articles are complete on one page, so return few or no groups, and never group
articles that are merely about similar things.
Return ONLY JSON: {"merges": [["<id>", "<id>", ...], ...]}, each group in page order.

ARTICLES:
"""


class EnrichError(Exception):
    pass


# --- LLM client ------------------------------------------------------------

@dataclass
class LLM:
    """An OpenAI-compatible chat endpoint (OpenRouter by default)."""
    base_url: str
    api_key: str
    timeout: int = 240
    cost: float = 0.0
    tokens: int = 0
    _lock: object = field(default=None, repr=False)

    def __post_init__(self):
        import threading
        self._lock = threading.Lock()

    def __call__(self, prompt: str, model: str, temperature: float = 0) -> str:
        body = {"model": model, "temperature": temperature,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "usage": {"include": True}}
        req = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise EnrichError(f"{model}: HTTP {e.code} {e.read()[:300]!r}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise EnrichError(f"{model}: {e}") from e
        usage = data.get("usage") or {}
        with self._lock:
            self.cost += float(usage.get("cost") or 0)
            self.tokens += int(usage.get("total_tokens") or 0)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise EnrichError(f"{model}: unexpected response {str(data)[:300]}")
        if not content:
            raise EnrichError(f"{model}: empty response")
        return content


def make_llm(settings: dict) -> LLM:
    key = os.environ.get(settings["api_key_env"])
    if not key:
        raise EnrichError(f"set {settings['api_key_env']} to use enrich "
                          f"(an OpenRouter key by default: https://openrouter.ai/keys)")
    return LLM(settings["base_url"], key)


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text)


def ask_json(llm: Callable, prompt: str, model: str, check: Callable[[dict], bool]) -> dict:
    """Call the model, retrying at a higher temperature when the reply doesn't parse
    or fails `check` (temperature 0 repeats the same bad answer)."""
    last = None
    for temp in (0, 0.3, 0.6):
        try:
            data = parse_json(llm(prompt, model, temperature=temp))
            if check(data):
                return data
            last = EnrichError(f"{model}: reply didn't have the expected shape")
        except (json.JSONDecodeError, EnrichError) as e:
            last = e
    raise last


# --- profile ---------------------------------------------------------------

def load_profile(project: Project, slug: str) -> dict | None:
    path = project.title_dir(slug) / "profile.json"
    return json.loads(path.read_text()) if path.exists() else None


def profile_block(profile: dict | None, date: str | None) -> str:
    """The title's profile as prompt guidance. Reads paperpress's keys and the
    older Negro World ones (standing_sections, contributor_roster, ...)."""
    if not profile:
        return ""
    p = profile
    lines = ["", "ABOUT THIS PUBLICATION (use it to recognise sections and spell names; never "
             "invent bylines from it):"]
    for k in ("publisher", "place"):
        if p.get(k):
            lines.append(f"{k.title()}: {p[k]}")
    if date:
        lines.append(f"This issue: {date}. Prefer entries whose era includes it.")

    def era(x):
        return f" [{x['era']}]" if isinstance(x, dict) and x.get("era") else ""

    sections = p.get("sections") or p.get("standing_sections") or []
    if sections:
        lines.append("Regular sections: " + "; ".join(
            (s.get("name") if isinstance(s, dict) else s) + era(s) for s in sections))
    columns = p.get("columns") or p.get("recurring_columns") or []
    if columns:
        lines.append("Regular columns: " + "; ".join(
            c["title"] + (f" by {c['author']}" if c.get("author") else "") + era(c)
            for c in columns))
    people = p.get("contributors") or p.get("contributor_roster") or []
    if people:
        def person(c):
            s = c.get("name") or c.get("canonical")
            if c.get("aka"):
                s += " (OCR'd as " + ", ".join(c["aka"]) + ")"
            if c.get("role"):
                s += f", {c['role']}"
            return s + era(c)
        lines.append("Known contributors (correct a byline to one of these only when it "
                     "clearly matches): " + "; ".join(person(c) for c in people))
    orgs = p.get("organizations") or []
    if orgs:
        lines.append("Organizations: " + "; ".join(orgs))
    ads = p.get("ad_categories") or [a.get("category") for a in p.get("ad_taxonomy", [])]
    if ads:
        lines.append("Typical advertisements: " + "; ".join(a for a in ads if a))
    langs = p.get("languages") or []
    if langs:
        lines.append("Languages: " + "; ".join(
            (l.get("name") if isinstance(l, dict) else l) for l in langs))
    fixes = p.get("ocr_fixes") or {**p.get("ocr_normalization", {}).get("name_variants", {}),
                                    **p.get("ocr_normalization", {}).get("entity_fixes", {})}
    if fixes:
        lines.append("Known OCR misreadings: " + "; ".join(f"{k} -> {v}" for k, v in fixes.items()))
    for note in p.get("notes") or []:
        lines.append(f"Note: {note}")
    return "\n".join(lines)


# --- per page --------------------------------------------------------------

def page_prompt(page: dict, *, title_name: str, kind: str, date: str | None,
                profile: dict | None) -> str:
    rows = []
    for r in page.get("regions", []):
        if r.get("status") in NO_TEXT_STATUSES:
            continue
        b = r["bbox"]
        text = region_text(r.get("text", ""))[:REGION_TEXT_CAP]
        rows.append(f"[{r['id']}] {r.get('label')} x={b['x0']} y={b['y0']} "
                    f"w={b['x1'] - b['x0']} h={b['y1'] - b['y0']} | {text}")
    when = f", {date}" if date else ""
    return (f"You are building the table of contents for page {page['page']} of a historical "
            f"{kind}, {title_name}{when}. Below are the regions detected on the page "
            f"(width {page.get('width')}, height {page.get('height')}), in reading order, "
            f"with bounding boxes and OCR text. The OCR contains errors."
            f"{profile_block(profile, date)}\n\n{PAGE_SCHEMA}\n\nREGIONS:\n" + "\n".join(rows))


def clean_page_result(page: dict, data: dict) -> list[dict]:
    """Keep only real region ids, give each region to its first article, drop empties."""
    valid = {r["id"] for r in page.get("regions", [])}
    taken: set[str] = set()
    out = []
    for a in data.get("articles", []):
        if not isinstance(a, dict):
            continue
        ids = [i for i in a.get("region_ids") or [] if i in valid and i not in taken]
        if not ids:
            continue
        taken.update(ids)
        typ = a.get("type") if a.get("type") in TYPES else "other"
        ad = bool(a.get("is_advertisement")) or typ == "advertisement"
        out.append({
            "title": (a.get("title") or "").strip() or "[Untitled]",
            "author": a.get("author") or None,
            "author_confidence": a.get("author_confidence") if a.get("author") else None,
            "type": "advertisement" if ad else typ,
            "section": a.get("section") or None,
            "language": a.get("language") or None,
            "is_advertisement": ad,
            "continued_from": a.get("continued_from"),
            "continued_to": a.get("continued_to"),
            "regions": [{"page": page["page"], "ids": ids}],
        })
    return out


def article_text(issue_dir: Path, article: dict, _pages: dict | None = None) -> str:
    """An article's text: its regions' OCR in the order the article lists them."""
    pages = _pages if _pages is not None else {}
    parts = []
    for ref in article.get("regions", []):
        n = ref["page"]
        if n not in pages:
            f = issue_dir / f"{page_name(n)}.json"
            pages[n] = {r["id"]: r for r in json.loads(f.read_text())["regions"]} \
                if f.exists() else {}
        for rid in ref["ids"]:
            r = pages[n].get(rid)
            if r and r.get("status") not in NO_TEXT_STATUSES:
                parts.append(region_text(r.get("text", "")))
    return "\n\n".join(p for p in parts if p)


# --- volume and number ----------------------------------------------------

MASTHEAD_TOP = 0.3             # share of page 1 searched for the masthead
SOURCE_TEXT_CAP = 2500         # characters of the source's own text shown to the model
MASTHEAD_PROMPT = """Find the volume and issue number printed in the masthead of this issue
of a historical periodical, {title}{when}. Below are the top of its first page as read by
our OCR, and the start of the source's own text for the same issue. Both contain OCR errors.

Return ONLY JSON: {{"volume": <volume as a number in Arabic digits, e.g. "68" for
"VOLUME LXVIII", or null>, "number": <issue number in Arabic digits, or null>,
"evidence": "<the exact words you read them from, copied character for character from
the text below, e.g. VOLUME LXVIII, NO. 111>"}}
Use null for anything not printed; never guess from dates or other issues.

TOP OF PAGE 1 (OUR OCR):
{ocr}

START OF THE SOURCE'S OWN TEXT:
{source}"""


def _roman(n: int) -> str:
    out = ""
    for value, letters in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
                           (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"),
                           (4, "IV"), (1, "I")):
        while n >= value:
            out, n = out + letters, n - value
    return out


def _squash(text: str) -> str:
    return " ".join(text.split()).casefold()


def masthead_texts(issue_dir: Path, rec: dict, page1: dict) -> tuple[str, str]:
    """(top of page 1 from our OCR, start of the source's own text)."""
    h = page1.get("height") or 1
    top = [region_text(r.get("text", "")) for r in page1.get("regions", [])
           if r.get("status") not in NO_TEXT_STATUSES and r["bbox"]["y0"] < h * MASTHEAD_TOP]
    source = ""
    if rec.get("source", {}).get("ocr"):
        f = issue_dir / rec["source"]["ocr"]
        if f.exists():
            source = f.read_text(errors="replace")[:SOURCE_TEXT_CAP]
    return "\n".join(t for t in top if t)[:SOURCE_TEXT_CAP], source


def check_volume_number(data: dict, texts: str) -> dict:
    """The fields the evidence supports: the evidence must be in the text, and each
    number must appear in the evidence (in digits, or Roman numerals for a volume)."""
    evidence = data.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip() \
            or _squash(evidence) not in _squash(texts):
        return {}
    ev = evidence.upper()
    out = {}
    for field in ("volume", "number"):
        value = str(data.get(field) or "").strip()
        if not value.isdigit() or not 0 < int(value) < 10000:
            continue
        n = int(value)
        tokens = set(re.findall(r"[0-9]+|[IVXLCDM]+", ev))
        if str(n) in {t.lstrip("0") for t in tokens} or (field == "volume" and _roman(n) in tokens):
            out[field] = str(n)
    if out:
        out["evidence"] = " ".join(evidence.split())
    return out


def fill_volume_number(issue_dir: Path, rec: dict, page1: dict, llm: Callable, model: str,
                       title_name: str, *, force: bool = False) -> dict:
    """Read a missing volume and number from the masthead and save them in issue.json.
    Never replaces a value from the source or a person, only ones enrich filled before
    (and those only with force). Returns what was filled."""
    before = rec.get("enrich_filled", {})
    open_fields = [f for f in ("volume", "number")
                   if not rec.get(f) or (force and f in before.get("fields", []))]
    if not open_fields:
        return {}
    ocr, source = masthead_texts(issue_dir, rec, page1)
    if not (ocr or source):
        return {}
    when = f", {rec['date']}" if rec.get("date") else ""
    prompt = MASTHEAD_PROMPT.format(title=title_name, when=when, ocr=ocr or "(none)",
                                    source=source or "(none)")
    found = check_volume_number(ask_json(llm, prompt, model, lambda d: isinstance(d, dict)),
                                ocr + "\n" + source)
    filled = {f: found[f] for f in open_fields if f in found}
    if not filled:
        return {}
    rec.update(filled)
    rec["enrich_filled"] = {"fields": sorted(set(before.get("fields", [])) | set(filled)),
                            "evidence": found["evidence"], "model": model}
    write_issue(issue_dir, rec)
    return filled


# --- stitch ----------------------------------------------------------------

def stitch(articles: list[dict], issue_dir: Path, llm: Callable, model: str) -> list[dict]:
    """Merge articles the model says are one story continued across pages."""
    editorial = [a for a in articles if a["type"] not in NOT_IN_TOC]
    if len({a["start_page"] for a in editorial}) < 2:
        return articles
    cache: dict = {}
    items = []
    for a in editorial:
        text = article_text(issue_dir, a, cache)
        items.append({"id": a["id"], "page": a["start_page"], "title": a["title"],
                      "continued_from": a.get("continued_from"),
                      "continued_to": a.get("continued_to"),
                      "head": text[:160], "tail": text[-200:]})
    try:
        data = ask_json(llm, STITCH_PROMPT + json.dumps(items, ensure_ascii=False), model,
                        lambda d: isinstance(d.get("merges"), list))
    except (EnrichError, json.JSONDecodeError):
        return articles                         # linking is a bonus; keep per-page results
    by_id = {a["id"]: a for a in articles}
    gone: set[str] = set()
    for group in data["merges"]:
        group = [g for g in group if isinstance(g, str) and g in by_id and g not in gone]
        group = sorted(dict.fromkeys(group), key=lambda g: by_id[g]["start_page"])
        if len(group) < 2 or len({by_id[g]["start_page"] for g in group}) < 2:
            continue                            # a continuation has to cross pages
        head = by_id[group[0]]
        for g in group[1:]:
            head["regions"] += by_id[g]["regions"]
            gone.add(g)
        head["pages"] = sorted({r["page"] for r in head["regions"]})
        head["continued"] = True
    return [a for a in articles if a["id"] not in gone]


# --- issue -----------------------------------------------------------------

def is_done(issue_dir: Path) -> bool:
    return (issue_dir / "toc.json").exists()


def enrich_issue(project: Project, issue_dir: Path, llm: Callable, settings: dict, *,
                 force: bool = False, workers: int = 6) -> dict:
    rec = read_issue(issue_dir)
    title = project.titles[rec["title"]]
    profile = load_profile(project, title.slug)
    pages = []
    for p in rec["pages"]:
        f = issue_dir / f"{page_name(p['page'])}.json"
        if not f.exists():
            raise EnrichError(f"{issue_dir.name}: page {p['page']} isn't OCR'd yet")
        pages.append(json.loads(f.read_text()))

    key = str(issue_dir.relative_to(project.title_dir(title.slug)))
    cache_dir = project.root / ".paperpress" / "enrich" / title.slug / key
    model = settings["model"]
    profile_key = (hashlib.sha1(json.dumps(profile, sort_keys=True).encode()).hexdigest()[:12]
                   if profile else None)

    def one(page: dict) -> list[dict]:
        cached = cache_dir / f"{page_name(page['page'])}.json"
        if cached.exists() and not force:
            c = json.loads(cached.read_text())
            if c.get("model") == model and c.get("profile") == profile_key:
                return c["articles"]
        if not any(r.get("status") not in NO_TEXT_STATUSES for r in page.get("regions", [])):
            arts = []
        else:
            prompt = page_prompt(page, title_name=title.name, kind=title.kind,
                                 date=rec.get("date"), profile=profile)
            data = ask_json(llm, prompt, model, lambda d: isinstance(d.get("articles"), list))
            arts = clean_page_result(page, data)
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps({"model": model, "profile": profile_key, "articles": arts},
                                      ensure_ascii=False))
        return arts

    with ThreadPoolExecutor(max_workers=min(workers, len(pages) or 1)) as pool:
        per_page = list(pool.map(one, pages))
    articles = []
    for page, arts in zip(pages, per_page):
        for n, a in enumerate(arts, start=1):
            articles.append({"id": f"p{page['page']}a{n}", "start_page": page["page"],
                             "pages": [page["page"]], "continued": False, **a})
    articles = stitch(articles, issue_dir, llm, settings["stitch_model"])
    filled = fill_volume_number(issue_dir, rec, pages[0], llm, model, title.name,
                                force=force) if pages else {}

    toc = {
        "title": title.slug,
        "date": rec.get("date"),
        "enrich": {"model": model, "stitch_model": settings["stitch_model"],
                   "profile": bool(profile),
                   "at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        "articles": articles,
    }
    tmp = issue_dir / "toc.json.tmp"
    tmp.write_text(json.dumps(toc, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(issue_dir / "toc.json")
    return {"articles": len(articles),
            "toc": sum(1 for a in articles if a["type"] not in NOT_IN_TOC),
            "ads": sum(1 for a in articles if a["is_advertisement"]),
            "continued": sum(1 for a in articles if a.get("continued")),
            "filled": filled}


def toc_entries(toc: dict) -> list[dict]:
    """The articles that belong in a reader-facing table of contents."""
    return [a for a in toc.get("articles", []) if a.get("type") not in NOT_IN_TOC]
