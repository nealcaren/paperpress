"""`paperpress profile`: draft a title's profile.json from a sample of its issues.

The profile tells the enrich stage what a periodical is like: its regular
sections and columns, its contributors (and how OCR misspells them), the
organizations it covers, its advertising and languages. Writing one by hand
means reading a lot of issues; this drafts it from the OCR instead.

Two LLM passes. First, for each sampled issue, the model reads a digest of the
issue (every headline in full, plus the opening and closing words of every
text block, where bylines and signatures are) and lists what it sees. Then a
second call merges those lists into one profile, keeping what recurs and
marking when each item appears. The draft is a starting point for a person
to correct, not a finished reference.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .enrich import ask_json
from .ocr import NO_TEXT_STATUSES, region_text
from .project import DATE_DIR, Project, page_name, read_issue

DEFAULT_SAMPLE = 6
DIGEST_CAP = 14000             # characters of each issue shown to the model
HEAD, TAIL = 160, 90           # characters from the start and end of a text block
SKIP_LABELS = {"abandon", "figure", "isolate_formula", "formula_caption"}

FIELDS = """{"publisher": <string or null>, "place": <city, or null>,
 "sections": [{"name": "<department heading as printed>", "type": "<news|editorial|letter|column|department|notice|poem|other>"}],
 "columns": [{"title": "<recurring column name>", "author": <string or null>}],
 "contributors": [{"name": "<full name, correctly spelled>", "role": <"editor" etc., or null>,
                   "aka": ["<other spellings or initials seen, including OCR misreadings>"]}],
 "organizations": ["<organization the paper covers or belongs to>"],
 "ad_categories": ["<kind of advertiser, e.g. dry goods, patent medicine>"],
 "languages": ["<language of any text, e.g. English, German>"],
 "ocr_fixes": {"<OCR misreading of a person's, place's or organization's name>": "<correct form>"},
 "notes": ["<anything else that would help someone recognize parts of an issue>"]}"""

ISSUE_PROMPT = """You are helping catalogue a historical periodical, {name}. Below is a
digest of one issue ({date}): every headline, and the first and last words of every
block of text, page by page, in reading order. The text is OCR and has errors.

List what this issue shows about the periodical as a whole. Only include what the
digest supports: regular sections and departments, recurring columns, contributors
(people who sign or are credited with pieces, and the editors and officers named in
the masthead or staff box; not people who are merely written about), organizations it
is connected with, kinds of advertisers, and languages. For ocr_fixes, list only
misreadings of proper names you can confidently correct, not ordinary misspelled
words. Return ONLY JSON:
{fields}

DIGEST:
{digest}"""

MERGE_PROMPT = """Below are notes on {n} sample issues of a historical periodical, {name},
each made separately from that issue's OCR. Merge them into one profile of the
periodical, to help an assistant recognize sections and spell names when indexing
other issues.

- Combine entries that are the same thing spelled differently; use the correct spelling
  and put the variants in "aka" (contributors) or "ocr_fixes".
- Keep sections, columns and contributors that appear in more than one issue, or that
  are clearly a fixture of the paper (an editor or officer, a standing department).
  Drop one-off items.
- Keep ocr_fixes only for proper names, and every correctly spelled name in them should
  also appear as a contributor or organization if it is one.
- Add "era" to each section, column and contributor: the years of the sample issues it
  appears in, e.g. "1912" or "1912-1917".
- Do not add anything that isn't in the notes.

Return ONLY JSON in this form, with "era" added to each section, column and contributor:
{fields}

NOTES:
{notes}"""


class ProfileError(Exception):
    pass


def ocrd_issues(project: Project, slug: str) -> list[Path]:
    return [d for d in project.issue_dirs(slug, include_undated=True)
            if (d / "full_text.json").exists()]


def sample_issues(issues: list[Path], n: int) -> list[Path]:
    """`n` issues spread evenly across the run, dated issues first."""
    dated = [d for d in issues if DATE_DIR.match(d.name)] or issues
    if len(dated) <= n:
        return dated
    step = (len(dated) - 1) / (n - 1) if n > 1 else 0
    return [dated[round(i * step)] for i in range(n)]


def issue_digest(issue_dir: Path, cap: int = DIGEST_CAP) -> str:
    """Headlines in full, plus the start and end of every text block (where bylines
    and signatures are), page by page."""
    rec = read_issue(issue_dir)
    pages = []
    for p in rec["pages"]:
        f = issue_dir / f"{page_name(p['page'])}.json"
        if not f.exists():
            continue
        lines = []
        for r in json.loads(f.read_text()).get("regions", []):
            if r.get("status") in NO_TEXT_STATUSES or r.get("label") in SKIP_LABELS:
                continue
            text = region_text(r.get("text", ""))
            if not text:
                continue
            if r.get("label") == "title":
                lines.append("# " + text[:200])
            elif len(text) <= HEAD + TAIL + 10:
                lines.append(text)
            else:
                lines.append(f"{text[:HEAD]} … {text[-TAIL:]}")
        pages.append(f"--- page {p['page']} ---\n" + "\n".join(lines))
    # share the budget across pages, so a long front page can't crowd out the rest
    each = cap // max(len(pages), 1)
    return "\n".join(pg if len(pg) <= each else pg[:each] + " …" for pg in pages)


def _list(x) -> list:
    return x if isinstance(x, list) else []


def clean_profile(data: dict) -> dict:
    """Keep only well-formed entries in the keys enrich reads."""
    out = {}
    for k in ("publisher", "place"):
        if isinstance(data.get(k), str) and data[k].strip():
            out[k] = data[k].strip()
    out["sections"] = [s for s in _list(data.get("sections"))
                       if isinstance(s, dict) and isinstance(s.get("name"), str)]
    out["columns"] = [c for c in _list(data.get("columns"))
                      if isinstance(c, dict) and isinstance(c.get("title"), str)]
    people = []
    for c in _list(data.get("contributors")):
        if isinstance(c, dict) and isinstance(c.get("name"), str):
            c["aka"] = [a for a in _list(c.get("aka")) if isinstance(a, str) and a != c["name"]]
            if not c["aka"]:
                del c["aka"]
            people.append(c)
    out["contributors"] = people
    for k in ("organizations", "ad_categories", "languages", "notes"):
        out[k] = [x for x in _list(data.get(k)) if isinstance(x, str) and x.strip()]
    fixes = data.get("ocr_fixes")
    out["ocr_fixes"] = ({k: v for k, v in fixes.items() if isinstance(v, str) and k != v}
                        if isinstance(fixes, dict) else {})
    return out


def draft_profile(project: Project, slug: str, llm: Callable, model: str, *,
                  sample: int = DEFAULT_SAMPLE,
                  log: Callable[[str], None] = lambda m: None) -> dict:
    title = project.title(slug)
    issues = ocrd_issues(project, slug)
    if not issues:
        raise ProfileError(f"no OCR'd issues of {slug} yet (run `paperpress ocr {slug}`)")
    chosen = sample_issues(issues, sample)
    notes = []
    for d in chosen:
        date = read_issue(d).get("date") or d.name
        log(f"  reading {d.name}")
        prompt = ISSUE_PROMPT.format(name=title.name, date=date, fields=FIELDS,
                                     digest=issue_digest(d))
        found = clean_profile(ask_json(llm, prompt, model, lambda x: isinstance(x, dict)))
        notes.append({"issue": date, **found})
    if len(notes) == 1:
        merged = notes[0]
        merged.pop("issue")
    else:
        log("  merging")
        prompt = MERGE_PROMPT.format(n=len(notes), name=title.name, fields=FIELDS,
                                     notes=json.dumps(notes, ensure_ascii=False, indent=1))
        merged = ask_json(llm, prompt, model, lambda x: isinstance(x, dict))
    profile = {"title": title.name, **clean_profile(merged)}
    for k in ("sections", "columns", "contributors"):      # keep era where the model gave one
        for item in profile[k]:
            if not isinstance(item.get("era"), str):
                item.pop("era", None)
    profile["drafted"] = {
        "by": "paperpress profile", "model": model,
        "issues": [read_issue(d).get("date") or d.name for d in chosen],
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "review": "A draft from OCR and an LLM: check names and spellings, delete what's "
                  "wrong, add what you know.",
    }
    return profile


def write_profile(project: Project, slug: str, profile: dict) -> Path:
    path = project.title_dir(slug) / "profile.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return path
