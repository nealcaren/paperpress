"""`paperpress build`: the archive as a static website for any web host.

Writes plain files (GitHub Pages, Netlify, a university web server, a USB
stick): the same pages as `paperpress serve`, with search done in the browser
by Pagefind instead of by a server.

    index.html, t/<slug>/..., search/index.html, static/   pages and assets
    img/<slug>/<issue>/page_NN.jpg      page scans, resized for the web
    thumbs/<slug>/<issue>/page_NN.jpg   cover and page thumbnails
    pagefind/                           the search index (loaded in pieces)

Images: by default every scan is copied in, resized to `image_width`, so the
site depends on nothing else. With images="ia", pages from the Internet Archive
are shown from IA's own image server instead, which keeps a large site small;
the IIIF request that works for each page is recorded in its issue.json.

Built in <out>.partial and swapped in at the end, so a failed build never
leaves a half-written site.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from .project import Project, page_name, write_issue
from .serve import thumb_path
from .site import (CSS, JS, Catalog, Issue, page_home, page_issue, page_reader,
                   page_search_static, page_title)


class BuildError(Exception):
    pass


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _web_image(src: Path, dest: Path, width: int) -> None:
    from PIL import Image

    with Image.open(src) as im:
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        dest.parent.mkdir(parents=True, exist_ok=True)
        im.convert("L" if im.mode in ("L", "1") else "RGB").save(dest, "JPEG", quality=72,
                                                                 optimize=True, progressive=True)


def _ia_sizes(issues: list[Issue], log) -> None:
    """Make sure every IA page has a working iiif_size, probing (and saving) where
    an older issue didn't record one."""
    from .sources import ia

    for issue in issues:
        pages = issue.rec["pages"]
        missing = [p for p in pages if p.get("iiif_service") and not p.get("iiif_size")]
        if not missing:
            continue
        log(f"  checking IA image sizes for {issue.slug}/{issue.key}")
        with ThreadPoolExecutor(max_workers=2) as pool:
            sizes = list(pool.map(ia.working_size, missing))
        for p, size in zip(missing, sizes):
            if size:
                p["iiif_size"] = size
        write_issue(issue.dir, issue.rec)


def build(project: Project, out: Path, *, base: str = "/", images: str = "copy",
          image_width: int = 1800, pagefind: bool = True,
          log: Callable[[str], None] = lambda m: None) -> dict:
    if images not in ("copy", "ia"):
        raise BuildError(f"images must be 'copy' or 'ia', not {images!r}")
    base = "/" + base.strip("/") + "/" if base.strip("/") else "/"
    tmp = out.with_name(out.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)

    def rel(path: str) -> str:
        return base + urllib.parse.quote(path)

    def image_src(issue: Issue, n: int) -> str:
        meta = next(p for p in issue.rec["pages"] if p["page"] == n)
        if images == "ia" and meta.get("iiif_service") and meta.get("iiif_size"):
            return f"{meta['iiif_service']}/full/{meta['iiif_size']}/0/default.jpg"
        return rel(f"img/{issue.slug}/{issue.key}/{page_name(n)}.jpg")

    def thumb_src(issue: Issue, n: int) -> str:
        return rel(f"thumbs/{issue.slug}/{issue.key}/{page_name(n)}.jpg")

    cat = Catalog(project, base=base, static=True, image_src=image_src,
                  thumb_src=thumb_src).refresh(max_age=0)
    issues = [i for iss in cat.issues.values() for i in iss]
    try:
        if images == "ia":
            _ia_sizes([i for i in issues if i.rec.get("source", {}).get("type")
                       == "internet_archive"], log)

        # images: a thumbnail for every page; full scans unless IA serves them
        jobs = []
        for issue in issues:
            for p in issue.rec["pages"]:
                n = p["page"]
                jobs.append(("thumb", issue, n))
                if not image_src(issue, n).startswith(("http://", "https://")):
                    jobs.append(("img", issue, n))
        log(f"preparing {len(jobs)} images")

        def do(job):
            kind, issue, n = job
            name = f"{issue.slug}/{issue.key}/{page_name(n)}.jpg"
            if kind == "thumb":
                dest = tmp / "thumbs" / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(thumb_path(project, issue, n), dest)
            else:
                meta = next(p for p in issue.rec["pages"] if p["page"] == n)
                _web_image(issue.dir / meta["image"], tmp / "img" / name, image_width)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(do, jobs))

        # pages
        _write(tmp / "index.html", page_home(cat))
        _write(tmp / "search" / "index.html", page_search_static(cat))
        _write(tmp / "static" / "style.css", CSS)
        _write(tmp / "static" / "app.js", JS)
        n_pages = 0
        for slug in project.titles:
            _write(tmp / "t" / slug / "index.html", page_title(cat, slug))
        for issue in issues:
            folder = tmp / "t" / issue.slug / issue.key
            _write(folder / "index.html", page_issue(cat, issue))
            for p in issue.rec["pages"]:
                _write(folder / "p" / str(p["page"]) / "index.html",
                       page_reader(cat, issue, p["page"]))
                n_pages += 1
        # GitHub Pages runs Jekyll by default, which drops folders starting with "_"
        # (like _undated/); this file turns that off
        (tmp / ".nojekyll").write_text("")

        if pagefind:
            log("indexing for search (Pagefind)")
            r = subprocess.run([sys.executable, "-m", "pagefind", "--site", str(tmp),
                                "--output-subdir", "pagefind"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise BuildError("Pagefind failed:\n" + (r.stderr or r.stdout)[-2000:])
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    if out.exists():
        shutil.rmtree(out)
    tmp.rename(out)
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    return {"issues": len(issues), "pages": n_pages, "bytes": size}
