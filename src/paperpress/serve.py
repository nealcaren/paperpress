"""`paperpress serve`: browse, read and search the archive on your own machine.

A small standard-library web server bound to 127.0.0.1, so nothing leaves the
laptop. Pages:

    /                               titles
    /t/<slug>/                      issues by year
    /t/<slug>/<issue>/              page thumbnails for one issue
    /t/<slug>/<issue>/p/<n>         reader: scan + region boxes beside the text
    /search?q=...                   full-text search (search.py)
    /img/<slug>/<issue>/<n>         full page image
    /thumb/<slug>/<issue>/<n>       cached thumbnail

<issue> is the issue's folder under titles/<slug>/ (a date, date__source-id,
or _undated/<source-id>). Every path is resolved against the catalog, never
joined from the URL, so the server can only hand out files in the project.
"""
from __future__ import annotations

import html
import io
import json
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .export import citation, human_date, page_span
from .folios import printed, printed_pages
from .ocr import NO_TEXT_STATUSES, region_text
from .project import Project, page_name, read_issue
from . import search as search_mod

THUMB_WIDTH = 360
PER_PAGE = 20


# --- catalog ---------------------------------------------------------------

@dataclass
class Issue:
    slug: str
    key: str                 # folder relative to titles/<slug>/
    dir: Path
    rec: dict
    ocrd: bool

    @property
    def url(self) -> str:
        return f"/t/{self.slug}/{urllib.parse.quote(self.key)}/"

    @property
    def label(self) -> str:
        return human_date(self.rec.get("date"), self.rec.get("date_precision")) or "Undated"

    @property
    def volno(self) -> str:
        v, n = self.rec.get("volume"), self.rec.get("number")
        return ", ".join(x for x in (f"vol. {v}" if v else "", f"no. {n}" if n else "") if x)


@dataclass
class Catalog:
    project: Project
    issues: dict[str, list[Issue]] = field(default_factory=dict)
    built: float = 0.0

    def refresh(self, max_age: float = 5.0) -> "Catalog":
        if time.monotonic() - self.built < max_age:
            return self
        issues = {}
        for slug in self.project.titles:
            base = self.project.title_dir(slug)
            issues[slug] = [Issue(slug, str(d.relative_to(base)), d, read_issue(d),
                                  (d / "full_text.json").exists())
                            for d in self.project.issue_dirs(slug, include_undated=True)]
        self.issues, self.built = issues, time.monotonic()
        return self

    def issue(self, slug: str, key: str) -> Issue | None:
        return next((i for i in self.issues.get(slug, []) if i.key == key), None)


# --- HTML ------------------------------------------------------------------

def e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def layout(project: Project, title: str, body: str, *, q: str = "", crumbs=()) -> str:
    trail = "".join(f' <span class="sep">/</span> <a href="{e(u)}">{e(t)}</a>' if u
                    else f' <span class="sep">/</span> <span>{e(t)}</span>' for t, u in crumbs)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title><link rel="stylesheet" href="/static/style.css"></head>
<body><header class="top">
  <nav class="crumbs"><a class="home" href="/">{e(project.name)}</a>{trail}</nav>
  <form class="searchbox" action="/search"><input type="search" name="q" value="{e(q)}"
    placeholder="Search the text" aria-label="Search the text"></form>
</header>
<main>{body}</main>
<script src="/static/app.js"></script></body></html>"""


def thumb(issue: Issue, n: int = 1, cls: str = "thumb") -> str:
    return (f'<img class="{cls}" loading="lazy" alt="" '
            f'src="/thumb/{issue.slug}/{urllib.parse.quote(issue.key)}/{n}">')


def page_home(cat: Catalog) -> str:
    p = cat.project
    cards = []
    for slug, t in p.titles.items():
        iss = cat.issues.get(slug, [])
        dates = [i.rec["date"] for i in iss if i.rec.get("date")]
        years = sorted({d[:4] for d in dates})
        span = (years[0] if len(years) == 1 else f"{years[0]}–{years[-1]}") if years else ""
        pages = sum(len(i.rec["pages"]) for i in iss)
        cover = thumb(iss[0]) if iss else '<div class="thumb empty"></div>'
        cards.append(f"""<a class="card title-card" href="/t/{slug}/">{cover}
          <div><h2>{e(t.name)}</h2><p class="meta">{len(iss)} issues · {pages} pages
          {f"· {span}" if span else ""}</p>
          <p class="meta">{sum(i.ocrd for i in iss)} of {len(iss)} OCR'd</p></div></a>""")
    body = f"""<h1>{e(p.name)}</h1>
      <div class="grid titles">{''.join(cards) or '<p>No titles in paper.toml yet.</p>'}</div>"""
    return layout(p, p.name, body)


def page_title(cat: Catalog, slug: str) -> str:
    p, t = cat.project, cat.project.titles[slug]
    by_year: dict[str, list[Issue]] = {}
    for i in cat.issues.get(slug, []):
        by_year.setdefault(i.rec["date"][:4] if i.rec.get("date") else "Undated", []).append(i)
    years = sorted(y for y in by_year if y != "Undated") + \
        (["Undated"] if "Undated" in by_year else [])
    jump = " ".join(f'<a href="#y{y}">{y}</a>' for y in years)
    sections = []
    for y in years:
        cards = "".join(f"""<a class="card issue-card" href="{i.url}">{thumb(i)}
            <div><strong>{e(i.label)}</strong><span class="meta">{e(i.volno)}</span>
            <span class="meta">{len(i.rec['pages'])} pp{'' if i.ocrd else ' · not OCR’d'}
            </span></div></a>""" for i in by_year[y])
        sections.append(f'<h2 id="y{y}">{y}</h2>'
                        f'<div class="grid issues">{cards}</div>')
    body = (f'<h1>{e(t.name)}</h1><p class="years">{jump}</p>' + "".join(sections)
            if sections else f"<h1>{e(t.name)}</h1><p>No issues yet.</p>")
    return layout(p, t.name, body, crumbs=[(t.name, None)])


def _source_link(rec: dict) -> str:
    src = rec.get("source", {})
    if src.get("url"):
        return f'<a href="{e(src["url"])}" rel="noopener">{e(src["url"])}</a>'
    if src.get("path"):
        return f"<code>{e(src['path'])}</code>"
    return ""


def page_issue(cat: Catalog, issue: Issue) -> str:
    p, t = cat.project, cat.project.titles[issue.slug]
    pages = "".join(f"""<a class="card page-card" href="{issue.url}p/{pg['page']}">
        {thumb(issue, pg['page'])}<span>Page {pg['page']}</span></a>"""
                    for pg in issue.rec["pages"])
    note = "" if issue.ocrd else ('<p class="note">Not OCR’d yet. Run '
                                  '<code>paperpress ocr</code> to make it searchable.</p>')
    contents = _contents(issue)
    body = f"""<h1>{e(t.name)}</h1>
      <p class="issue-meta">{e(issue.label)}{f" · {e(issue.volno)}" if issue.volno else ""}
      · {len(issue.rec['pages'])} pages</p>
      <p class="meta">Source: {_source_link(issue.rec)}</p>{note}{contents}
      <h2>Pages</h2><div class="grid pages">{pages}</div>"""
    return layout(p, f"{t.name}, {issue.label}", body,
                  crumbs=[(t.name, f"/t/{issue.slug}/"), (issue.label, None)])


def _contents(issue: Issue) -> str:
    """The issue's table of contents from toc.json (paperpress enrich), if any."""
    from .enrich import toc_entries

    f = issue.dir / "toc.json"
    if not f.exists():
        return ""
    toc = json.loads(f.read_text())
    numbering = printed_pages(issue.dir, issue.rec)

    def entry(a):
        first = a["regions"][0]
        href = f"{issue.url}p/{first['page']}?r={urllib.parse.quote(first['ids'][0])}"
        where = page_span(printed(numbering, a["pages"]))
        by = f' <span class="by">{e(a["author"])}</span>' if a.get("author") else ""
        kind = a.get("section") or (a["type"] if a["type"] not in ("news", "other") else "")
        return (f'<li><a href="{href}">{e(a["title"])}</a>{by}'
                f'<span class="meta"> {e(kind)} · {where}</span></li>')

    items = "".join(entry(a) for a in toc_entries(toc))
    ads = [a for a in toc.get("articles", []) if a.get("is_advertisement")]
    ad_list = (f'<details><summary>{len(ads)} advertisement{"s" if len(ads) != 1 else ""}</summary><ul class="toc">'
               f'{"".join(entry(a) for a in ads)}</ul></details>' if ads else "")
    model = toc.get("enrich", {}).get("model", "an LLM")
    return (f'<h2>Contents</h2><ul class="toc">{items}</ul>{ad_list}'
            f'<p class="meta small">Contents drafted by {e(model)} from the OCR; '
            f'headlines and authors may contain errors.</p>')


def _highlighter(q: str):
    """Mark words matching the query, approximating FTS5's porter stemming by
    prefix: a term matches words starting with its first max(4, len-2) letters."""
    words = []
    for term in search_mod.query_terms(q):
        for w in re.findall(r"\w+", term):
            w = w.lower()
            words.append(re.escape(w if len(w) <= 4 else w[:max(4, len(w) - 2)]))
    if not words:
        return None
    return re.compile(r"\b(?:" + "|".join(words) + r")\w*", re.IGNORECASE)


def page_reader(cat: Catalog, issue: Issue, n: int, q: str, select: str = "") -> str:
    p, t = cat.project, cat.project.titles[issue.slug]
    meta = next(pg for pg in issue.rec["pages"] if pg["page"] == n)
    total = len(issue.rec["pages"])
    ocr_file = issue.dir / f"{page_name(n)}.json"
    rx = _highlighter(q)
    regions_svg, regions_txt, w, h = [], [], meta.get("width"), meta.get("height")
    if ocr_file.exists():
        page = json.loads(ocr_file.read_text())
        w, h = page.get("width", w), page.get("height", h)
        for r in page.get("regions", []):
            text = region_text(r.get("text", "")) if r.get("status") not in NO_TEXT_STATUSES \
                else ""
            body = e(text)
            hit = bool(rx and text and rx.search(text))
            if rx:
                body = rx.sub(lambda m: f"<mark>{m.group(0)}</mark>", body)
            b = r["bbox"]
            # labels are prefixed: DocLayout has a label called "text", which as a bare
            # class would collide with the page's own class names
            cls = f"r lab-{e(r.get('label', ''))}{' hit' if hit else ''}"
            regions_svg.append(f'<rect class="{cls}" data-r="{e(r["id"])}" x="{b["x0"]}" '
                               f'y="{b["y0"]}" width="{b["x1"] - b["x0"]}" '
                               f'height="{b["y1"] - b["y0"]}"></rect>')
            flag = (f'<span class="flag">{e(r.get("status"))}</span>'
                    if r.get("status") != "ok" else "")
            tag = "h3" if r.get("label") in ("title", "doc_title") else "p"
            if text or flag:
                regions_txt.append(f'<{tag} class="{cls}" data-r="{e(r["id"])}">'
                                   f'{body}{flag}</{tag}>')
        text_pane = "".join(regions_txt) or '<p class="note">No text found on this page.</p>'
    else:
        text_pane = ('<p class="note">This page hasn’t been OCR’d yet. Run '
                     '<code>paperpress ocr</code>.</p>')
    qs = f"?q={urllib.parse.quote(q)}" if q else ""
    prev_link = f'<a rel="prev" href="{issue.url}p/{n - 1}{qs}">‹ Page {n - 1}</a>' if n > 1 \
        else '<span></span>'
    next_link = f'<a rel="next" href="{issue.url}p/{n + 1}{qs}">Page {n + 1} ›</a>' \
        if n < total else '<span></span>'
    img = f"/img/{issue.slug}/{urllib.parse.quote(issue.key)}/{n}"
    numbering = printed_pages(issue.dir, issue.rec)
    cite = citation(t.name, issue.rec, printed(numbering, n))
    printed_label = (f" <span class='meta'>(printed p. {numbering['pages'][n]})</span>"
                     if numbering["pages"] and numbering["pages"][n] != n else "")
    body = f"""<div class="reader" data-w="{w}" data-h="{h}" data-select="{e(select)}">
  <section class="viewer">
    <div class="tools">
      <button type="button" data-zoom="-1" aria-label="Zoom out">−</button>
      <button type="button" data-zoom="0">Fit</button>
      <button type="button" data-zoom="1" aria-label="Zoom in">+</button>
      <label><input type="checkbox" id="boxes" checked> Boxes</label>
      <span class="pager">{prev_link}<span>Page {n} of {total}{printed_label}</span>{next_link}</span>
    </div>
    <div class="scroller"><div class="canvas">
      <img src="{img}" alt="Page {n} of {e(cite)}">
      <svg viewBox="0 0 {w} {h}" preserveAspectRatio="none">{''.join(regions_svg)}</svg>
    </div></div>
  </section>
  <aside class="textpane">
    <p class="cite">{e(cite)} <button type="button" class="copy" data-copy="{e(cite)}">Copy
      citation</button></p>
    {text_pane}
  </aside>
</div>"""
    return layout(p, cite, body, q=q, crumbs=[(t.name, f"/t/{issue.slug}/"),
                                               (issue.label, issue.url), (f"Page {n}", None)])


def page_search(cat: Catalog, params: dict) -> str:
    p = cat.project
    q = params.get("q", "")
    slug = params.get("title") if params.get("title") in p.titles else None
    sort = params.get("sort") if params.get("sort") in ("relevance", "oldest", "newest") \
        else "relevance"
    dfrom, dto = params.get("from", ""), params.get("to", "")
    try:
        pg = max(1, int(params.get("page", "1")))
    except ValueError:
        pg = 1
    opts = "".join(f'<option value="{s}"{" selected" if s == slug else ""}>{e(t.name)}</option>'
                   for s, t in p.titles.items())
    sorts = "".join(f'<option{" selected" if s == sort else ""}>{s}</option>'
                    for s in ("relevance", "oldest", "newest"))
    form = f"""<form class="filters" action="/search">
      <input type="search" name="q" value="{e(q)}" placeholder="Words or &quot;a phrase&quot;" autofocus>
      <select name="title"><option value="">All titles</option>{opts}</select>
      <input name="from" value="{e(dfrom)}" placeholder="From (1912)" size="10">
      <input name="to" value="{e(dto)}" placeholder="To (1914-06)" size="10">
      <select name="sort">{sorts}</select><button>Search</button></form>"""
    results = ""
    if q:
        total, hits = search_mod.search(p, q, slug=slug, date_from=dfrom or None,
                                        date_to=dto or None, sort=sort, limit=PER_PAGE,
                                        offset=(pg - 1) * PER_PAGE)
        items = []
        for h in hits:
            issue = cat.issue(h.slug, h.folder)
            if not issue:
                continue
            cite = citation(p.titles[h.slug].name, issue.rec, h.page)
            items.append(f"""<li><a href="{issue.url}p/{h.page}?q={urllib.parse.quote(q)}">
              {e(cite)}</a><p class="snippet">{h.snippet}</p></li>""")
        nav = ""
        if total > PER_PAGE:
            base = {k: v for k, v in params.items() if v and k != "page"}
            links = []
            if pg > 1:
                links.append(f'<a href="/search?{urllib.parse.urlencode({**base, "page": pg - 1})}">'
                             f'‹ Previous</a>')
            if pg * PER_PAGE < total:
                links.append(f'<a href="/search?{urllib.parse.urlencode({**base, "page": pg + 1})}">'
                             f'Next ›</a>')
            nav = f'<nav class="more">{" ".join(links)}</nav>'
        results = (f'<p class="count">{total:,} page{"s" if total != 1 else ""} '
                   f'match{"es" if total == 1 else ""}</p><ol class="hits" start="'
                   f'{(pg - 1) * PER_PAGE + 1}">{"".join(items)}</ol>{nav}')
    return layout(p, f"Search: {q}" if q else "Search", form + results,
                  crumbs=[("Search", None)])


# --- static assets ---------------------------------------------------------

CSS = """
:root{--bg:#faf8f4;--fg:#1d1b18;--muted:#6b665d;--line:#e2ddd3;--card:#fff;--accent:#8a3b12;
--mark:#ffe08a;--box:rgba(138,59,18,.55);--boxfill:rgba(138,59,18,.06);--sel:rgba(30,110,200,.9)}
@media (prefers-color-scheme:dark){:root{--bg:#171614;--fg:#ece8e1;--muted:#a39d92;--line:#33302b;
--card:#211f1c;--accent:#e08a5a;--mark:#7a5d00;--box:rgba(224,138,90,.6);--boxfill:rgba(224,138,90,.08)}}
*{box-sizing:border-box}html,body{margin:0}
body{background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,sans-serif}
a{color:var(--accent)}main{padding:16px 24px 48px;max-width:1500px;margin:0 auto}
h1,h2,h3{font-family:Georgia,"Times New Roman",serif;font-weight:600}
h1{font-size:1.9rem;margin:.4em 0}.meta,.count,.issue-meta{color:var(--muted)}
.top{display:flex;gap:16px;align-items:center;justify-content:space-between;padding:10px 24px;
border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
.crumbs{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.crumbs .home{font-weight:600}
.sep{color:var(--muted);margin:0 2px}.searchbox input{width:min(40vw,320px)}
input,select,button{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:6px;
background:var(--card);color:var(--fg)}button{cursor:pointer}
.grid{display:grid;gap:16px}.titles{grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
.issues,.pages{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
.card{display:flex;flex-direction:column;gap:8px;background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:10px;color:inherit;text-decoration:none}.card:hover{border-color:var(--accent)}
.title-card{flex-direction:row}.title-card .thumb{width:120px}.title-card h2{margin:0 0 4px}
.issue-card span,.page-card span{display:block;font-size:.9rem}
.thumb{width:100%;aspect-ratio:3/4;object-fit:cover;object-position:top;background:var(--line);border-radius:4px}
.years a{margin-right:10px}.note{background:var(--card);border-left:3px solid var(--accent);padding:8px 12px}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 16px}.filters input[type=search]{flex:1 1 280px}
.hits li{margin:0 0 18px}.snippet{margin:4px 0 0;color:var(--muted)}mark{background:var(--mark);color:inherit}
.more a{margin-right:16px}
.toc{list-style:none;padding:0;columns:2 380px;column-gap:32px}.toc li{break-inside:avoid;margin:0 0 8px}
.toc .by{font-style:italic}.small{font-size:.85rem}details{margin:8px 0}
.reader{display:grid;grid-template-columns:minmax(0,3fr) minmax(320px,2fr);gap:16px;height:calc(100vh - 90px)}
.viewer{display:flex;flex-direction:column;min-height:0}
.tools{display:flex;gap:6px;align-items:center;flex-wrap:wrap;padding-bottom:8px}
.pager{margin-left:auto;display:flex;gap:12px}
.scroller{flex:1;overflow:auto;border:1px solid var(--line);background:#777;border-radius:6px}
.canvas{position:relative;width:100%;margin:0 auto}.canvas img{display:block;width:100%;height:auto}
.canvas svg{position:absolute;inset:0;width:100%;height:100%}
rect.r{fill:transparent;stroke:var(--box);stroke-width:3;vector-effect:non-scaling-stroke;cursor:pointer}
rect.r:hover{fill:var(--boxfill)}rect.r.hit{fill:rgba(255,200,0,.18)}
rect.r.sel{stroke:var(--sel);stroke-width:3;fill:rgba(30,110,200,.12)}
.noboxes rect.r:not(.sel){stroke:transparent}
.textpane{position:relative;overflow:auto;padding:0 8px 24px;font-family:Georgia,serif;font-size:1.02rem;line-height:1.55}
.textpane .cite{font-family:system-ui,sans-serif;font-size:.9rem;color:var(--muted)}
.textpane .copy{font-size:.8rem;padding:2px 8px;margin-left:6px}
.textpane p.r,.textpane h3.r{padding:4px 8px;margin:0 0 10px;border-left:3px solid transparent;cursor:pointer}
.textpane h3.r{font-size:1.1rem}.textpane .r.sel{border-left-color:var(--sel);background:var(--card)}
.textpane .lab-abandon,.textpane .lab-figure,.textpane .lab-figure_caption{color:var(--muted);font-size:.92rem}
.flag{font:.75rem system-ui;color:#fff;background:#a33;border-radius:3px;padding:0 4px;margin-left:6px}
@media (max-width:820px){main{padding:12px 16px}.reader{grid-template-columns:1fr;height:auto}
.scroller{height:70vh}.textpane{overflow:visible}.top{flex-wrap:wrap}.searchbox input{width:100%}}
"""

JS = """
(() => {
  const reader = document.querySelector('.reader');
  document.addEventListener('click', ev => {
    const c = ev.target.closest('[data-copy]');
    if (c) { navigator.clipboard?.writeText(c.dataset.copy);
             c.textContent = 'Copied'; setTimeout(() => c.textContent = 'Copy citation', 1500); }
  });
  document.addEventListener('keydown', ev => {
    if (ev.target.matches('input,select,textarea')) return;
    const rel = {ArrowLeft: 'prev', ArrowRight: 'next'}[ev.key];
    const a = rel && document.querySelector(`a[rel=${rel}]`);
    if (a) location.href = a.href;
  });
  if (!reader) return;
  const scroller = reader.querySelector('.scroller'), canvas = reader.querySelector('.canvas');
  const pageW = +reader.dataset.w, pageH = +reader.dataset.h;
  let zoom = 1;
  const setZoom = z => {
    const keepX = (scroller.scrollLeft + scroller.clientWidth / 2) / canvas.offsetWidth;
    const keepY = (scroller.scrollTop + scroller.clientHeight / 2) / canvas.offsetHeight;
    zoom = Math.min(8, Math.max(1, z));
    canvas.style.width = (zoom * 100) + '%';
    scroller.scrollLeft = keepX * canvas.offsetWidth - scroller.clientWidth / 2;
    scroller.scrollTop = keepY * canvas.offsetHeight - scroller.clientHeight / 2;
  };
  reader.querySelectorAll('[data-zoom]').forEach(b => b.addEventListener('click', () => {
    const d = +b.dataset.zoom; setZoom(d === 0 ? 1 : zoom * (d > 0 ? 1.5 : 1 / 1.5));
  }));
  document.getElementById('boxes').addEventListener('change', ev =>
    reader.classList.toggle('noboxes', !ev.target.checked));
  // drag to pan
  let drag = null;
  scroller.addEventListener('mousedown', ev => {
    if (ev.target.closest('rect')) return;
    drag = {x: ev.clientX, y: ev.clientY, l: scroller.scrollLeft, t: scroller.scrollTop};
  });
  addEventListener('mousemove', ev => { if (!drag) return;
    scroller.scrollLeft = drag.l - (ev.clientX - drag.x); scroller.scrollTop = drag.t - (ev.clientY - drag.y); });
  addEventListener('mouseup', () => drag = null);

  const textPane = reader.querySelector('.textpane');
  const showBox = (id, scrollText, smooth = true) => {
    const behavior = smooth ? 'smooth' : 'auto';
    reader.querySelectorAll('.sel').forEach(n => n.classList.remove('sel'));
    const rect = reader.querySelector(`rect[data-r="${id}"]`);
    const para = reader.querySelector(`.textpane [data-r="${id}"]`);
    rect?.classList.add('sel'); para?.classList.add('sel');
    if (scrollText && para) {
      // set scrollTop directly: scrollIntoView would also try to scroll the
      // image pane and the window, and the two animations fight
      const top = para.getBoundingClientRect().top - textPane.getBoundingClientRect().top;
      textPane.scrollTo({top: textPane.scrollTop + top - textPane.clientHeight / 4, behavior});
    }
    if (rect) {
      if (zoom < 2) setZoom(2);
      const s = canvas.offsetWidth / pageW;
      const x = +rect.getAttribute('x'), y = +rect.getAttribute('y');
      const w = +rect.getAttribute('width'), h = +rect.getAttribute('height');
      // centre the box; tall boxes are shown from their top
      const cy = h * s > scroller.clientHeight ? y * s + scroller.clientHeight / 2 - 30
                                               : (y + h / 2) * s;
      scroller.scrollTo({left: (x + w / 2) * s - scroller.clientWidth / 2,
                         top: cy - scroller.clientHeight / 2, behavior});
    }
  };
  reader.addEventListener('click', ev => {
    const t = ev.target.closest('[data-r]');
    if (t) showBox(t.dataset.r, t.tagName === 'rect');
  });
  // jump to the requested region (?r=, from a contents link) or the first search
  // hit once the page has fully laid out (before the scan loads the canvas has
  // no height, so scroll positions get clamped). Instant, not smooth: smooth
  // scrolls started during page load get cut short.
  const firstHit = reader.querySelector('.textpane .hit');
  const target = reader.dataset.select || firstHit?.dataset.r;
  const jump = () => target && requestAnimationFrame(() => showBox(target, true, false));
  if (document.readyState === 'complete') jump(); else addEventListener('load', jump, {once: true});
})();
"""


# --- server ----------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    catalog: Catalog                     # set on the subclass made by make_server
    thumb_lock = threading.Lock()

    def log_message(self, fmt, *args):   # quiet: the terminal is for the user
        pass

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        params = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        path = urllib.parse.unquote(url.path)
        try:
            self.route(path, params)
        except BrokenPipeError:
            pass
        except Exception as exc:          # show the error rather than a dead tab
            self.send_html(f"<h1>Something went wrong</h1><pre>{e(exc)}</pre>",
                           HTTPStatus.INTERNAL_SERVER_ERROR, wrap=True)

    def route(self, path: str, params: dict):
        cat = self.catalog.refresh()
        if path == "/":
            return self.send_html(page_home(cat))
        if path == "/search":
            return self.send_html(page_search(cat, params))
        if path == "/static/style.css":
            return self.send_bytes(CSS.encode(), "text/css; charset=utf-8")
        if path == "/static/app.js":
            return self.send_bytes(JS.encode(), "text/javascript; charset=utf-8")
        if m := re.fullmatch(r"/t/([a-z0-9-]+)/?", path):
            if m[1] in cat.project.titles:
                return self.send_html(page_title(cat, m[1]))
        if m := re.fullmatch(r"/t/([a-z0-9-]+)/(.+?)/(?:p/(\d+))?", path):
            issue = cat.issue(m[1], m[2])
            if issue and not m[3]:
                return self.send_html(page_issue(cat, issue))
            if issue and self._has_page(issue, int(m[3])):
                return self.send_html(page_reader(cat, issue, int(m[3]), params.get("q", ""),
                                                  params.get("r", "")))
        if m := re.fullmatch(r"/(img|thumb)/([a-z0-9-]+)/(.+)/(\d+)", path):
            issue = cat.issue(m[2], m[3])
            if issue and self._has_page(issue, int(m[4])):
                return self.send_image(issue, int(m[4]), thumb=m[1] == "thumb")
        self.send_html("<h1>Not found</h1>", HTTPStatus.NOT_FOUND, wrap=True)

    @staticmethod
    def _has_page(issue: Issue, n: int) -> bool:
        return any(pg["page"] == n for pg in issue.rec["pages"])

    def send_image(self, issue: Issue, n: int, *, thumb: bool):
        meta = next(pg for pg in issue.rec["pages"] if pg["page"] == n)
        src = issue.dir / meta["image"]
        if not thumb:
            return self.send_bytes(src.read_bytes(), "image/jpeg", cache=True)
        cached = (self.catalog.project.root / ".paperpress" / "thumbs" / issue.slug /
                  issue.key / f"{page_name(n)}.jpg")
        if not cached.exists() or cached.stat().st_mtime < src.stat().st_mtime:
            from PIL import Image

            with self.thumb_lock:
                with Image.open(src) as im:
                    im.draft("L" if im.mode == "L" else "RGB", (THUMB_WIDTH, THUMB_WIDTH * 2))
                    im.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 2))
                    buf = io.BytesIO()
                    im.convert("L" if im.mode == "L" else "RGB").save(buf, "JPEG", quality=80)
                cached.parent.mkdir(parents=True, exist_ok=True)
                tmp = cached.with_suffix(".tmp")
                tmp.write_bytes(buf.getvalue())
                tmp.replace(cached)
        self.send_bytes(cached.read_bytes(), "image/jpeg", cache=True)

    def send_html(self, body: str, status=HTTPStatus.OK, wrap: bool = False):
        if wrap:
            body = layout(self.catalog.project, "paperpress", body)
        self.send_bytes(body.encode(), "text/html; charset=utf-8", status=status)

    def send_bytes(self, data: bytes, ctype: str, *, status=HTTPStatus.OK, cache=False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-cache")
        self.end_headers()
        self.wfile.write(data)


def make_server(project: Project, port: int = 8000, tries: int = 20) -> ThreadingHTTPServer:
    """A server for `project` on 127.0.0.1, on `port` or the next free one."""
    handler = type("ProjectHandler", (Handler,), {"catalog": Catalog(project)})
    last = None
    for p in range(port, port + tries):
        try:
            return ThreadingHTTPServer(("127.0.0.1", p), handler)
        except OSError as exc:
            last = exc
    raise OSError(f"no free port in {port}–{port + tries - 1}: {last}")
