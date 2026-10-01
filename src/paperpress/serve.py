"""`paperpress serve`: browse, read and search the archive on your own machine.

A small standard-library web server bound to 127.0.0.1, so nothing leaves the
laptop. The pages come from site.py; this module only routes requests and
hands out images and thumbnails:

    /img/<slug>/<issue>/<n>         full page image
    /thumb/<slug>/<issue>/<n>       cached thumbnail

Every path is resolved against the catalog, never joined from the URL, so the
server can only hand out files in the project.
"""
from __future__ import annotations

import io
import re
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .project import Project, page_name
from .site import (CSS, JS, Catalog, Issue, e, layout, page_home, page_issue, page_reader,
                   page_search, page_title)

THUMB_WIDTH = 360


def make_thumbnail(src: Path, dest: Path, width: int = THUMB_WIDTH) -> None:
    """A small JPEG of a page image, written atomically (build reuses it)."""
    from PIL import Image

    with Image.open(src) as im:
        im.draft("L" if im.mode == "L" else "RGB", (width, width * 2))
        im.thumbnail((width, width * 2))
        buf = io.BytesIO()
        im.convert("L" if im.mode == "L" else "RGB").save(buf, "JPEG", quality=80)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(buf.getvalue())
    tmp.replace(dest)


def thumb_path(project: Project, issue: Issue, n: int) -> Path:
    """The cached thumbnail for a page, (re)made if missing or older than the scan."""
    meta = next(pg for pg in issue.rec["pages"] if pg["page"] == n)
    src = issue.dir / meta["image"]
    cached = project.root / ".paperpress" / "thumbs" / issue.slug / issue.key / f"{page_name(n)}.jpg"
    if not cached.exists() or cached.stat().st_mtime < src.stat().st_mtime:
        make_thumbnail(src, cached)
    return cached


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
        if path in ("/search", "/search/"):
            return self.send_html(page_search(cat, params))
        if path == "/static/style.css":
            return self.send_bytes(CSS.encode(), "text/css; charset=utf-8")
        if path == "/static/app.js":
            return self.send_bytes(JS.encode(), "text/javascript; charset=utf-8")
        if m := re.fullmatch(r"/t/([a-z0-9-]+)/?", path):
            if m[1] in cat.project.titles:
                return self.send_html(page_title(cat, m[1]))
        if m := re.fullmatch(r"/t/([a-z0-9-]+)/(.+?)/(?:p/(\d+)/?)?", path):
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
        if thumb:
            with self.thumb_lock:
                path = thumb_path(self.catalog.project, issue, n)
        else:
            meta = next(pg for pg in issue.rec["pages"] if pg["page"] == n)
            path = issue.dir / meta["image"]
        self.send_bytes(path.read_bytes(), "image/jpeg", cache=True)

    def send_html(self, body: str, status=HTTPStatus.OK, wrap: bool = False):
        if wrap:
            body = layout(self.catalog, "paperpress", body)
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
