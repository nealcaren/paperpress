"""Full-text search over the OCR'd pages (SQLite FTS5, built from full_text.json).

The index lives at <project>/.paperpress/search.db. It is a cache: it records a
signature of every full_text.json it was built from (path, size, mtime), and
ensure_index() rebuilds it whenever that changes. So new OCR shows up in
search without anyone having to remember to re-index.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .project import Project

SCHEMA = """
CREATE VIRTUAL TABLE pages USING fts5(
    text, slug UNINDEXED, folder UNINDEXED, date UNINDEXED, page UNINDEXED,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""
HIT_OPEN, HIT_CLOSE = "\x01", "\x02"      # snippet markers, swapped for <mark> after escaping


def index_path(project: Project) -> Path:
    return project.root / ".paperpress" / "search.db"


def _sources(project: Project) -> list[tuple[str, str, Path]]:
    out = []
    for slug in project.titles:
        base = project.title_dir(slug)
        for d in project.issue_dirs(slug, include_undated=True):
            ft = d / "full_text.json"
            if ft.exists():
                out.append((slug, str(d.relative_to(base)), ft))
    return out


def _signature(sources) -> str:
    h = hashlib.sha256()
    for slug, folder, ft in sources:
        st = ft.stat()
        h.update(f"{slug}/{folder}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def ensure_index(project: Project) -> tuple[Path, bool]:
    """Make sure the index matches the OCR on disk. Returns (path, rebuilt)."""
    path = index_path(project)
    sources = _sources(project)
    sig = _signature(sources)
    if path.exists():
        try:
            with sqlite3.connect(path) as db:
                row = db.execute("SELECT value FROM meta WHERE key='signature'").fetchone()
            if row and row[0] == sig:
                return path, False
        except sqlite3.DatabaseError:
            pass                                   # corrupt or old: rebuild
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    try:
        db.executescript(SCHEMA)
        for slug, folder, ft in sources:
            full = json.loads(ft.read_text())
            db.executemany(
                "INSERT INTO pages (text, slug, folder, date, page) VALUES (?, ?, ?, ?, ?)",
                [(p["text"], slug, folder, full.get("date"), p["page"]) for p in full["pages"]])
        db.execute("INSERT INTO meta VALUES ('signature', ?)", (sig,))
        db.commit()
    finally:
        db.close()
    tmp.replace(path)
    return path, True


def query_terms(q: str) -> list[str]:
    """Phrases ("...") and words from a search box, in order."""
    return [a or b for a, b in re.findall(r'"([^"]+)"|(\S+)', q or "")
            if (a or b).strip('"').strip()]


def fts_query(q: str) -> str | None:
    """User text -> a safe FTS5 query: every word/phrase quoted and required.
    A trailing * on a word keeps prefix matching (e.g. suffrag*)."""
    parts = []
    for t in query_terms(q):
        words = re.findall(r"\w+", t)
        if not words:
            continue
        star = "*" if t.endswith("*") and " " not in t.strip() else ""
        parts.append('"' + " ".join(words) + '"' + star)
    return " ".join(parts) or None


@dataclass
class Hit:
    slug: str
    folder: str
    date: str | None
    page: int
    snippet: str          # HTML-safe, with <mark> around matches


def search(project: Project, q: str, *, slug: str | None = None, date_from: str | None = None,
           date_to: str | None = None, sort: str = "relevance", limit: int = 20,
           offset: int = 0) -> tuple[int, list[Hit]]:
    match = fts_query(q)
    if not match:
        return 0, []
    path, _ = ensure_index(project)
    where, args = ["pages MATCH ?"], [match]
    if slug:
        where.append("slug = ?")
        args.append(slug)
    if date_from:
        where.append("date >= ?")
        args.append(date_from)
    if date_to:
        where.append("date <= ?")
        args.append(date_to + "~")         # include the whole of a YYYY or YYYY-MM bound
    order = {"relevance": "rank", "oldest": "date IS NULL, date, page",
             "newest": "date IS NULL, date DESC, page"}[sort]   # undated last either way
    sql_where = " AND ".join(where)
    with sqlite3.connect(path) as db:
        try:
            total = db.execute(f"SELECT count(*) FROM pages WHERE {sql_where}", args).fetchone()[0]
            rows = db.execute(
                f"SELECT slug, folder, date, page, "
                f"snippet(pages, 0, ?, ?, ' … ', 28) FROM pages WHERE {sql_where} "
                f"ORDER BY {order} LIMIT ? OFFSET ?",
                [HIT_OPEN, HIT_CLOSE, *args, limit, offset]).fetchall()
        except sqlite3.OperationalError:
            return 0, []
    hits = [Hit(s, f, d, int(p), html.escape(snip).replace(HIT_OPEN, "<mark>")
                .replace(HIT_CLOSE, "</mark>")) for s, f, d, p, snip in rows]
    return total, hits
