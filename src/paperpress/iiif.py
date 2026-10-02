"""IIIF Presentation 3 for the static site: the archive in the format library
viewers and platforms read (Mirador, the Universal Viewer, Omeka S, ...).

    iiif/collection.json                     every title
    iiif/<slug>/collection.json              a title's issues, with dates (navDate)
    iiif/<slug>/<issue>/manifest.json        one issue: a canvas per page, its
                                             contents as ranges, links back to
                                             the reader and the source
    iiif/<slug>/<issue>/text/p<n>.json       a page's OCR as annotations, one per
                                             region, each targeting its box
    iiif/<slug>/<issue>/full_text.txt        the issue's text (the manifest's
                                             "rendering")

IIIF ids must be absolute URLs, so these are written only when the build is
given the site's public address. A canvas is the size of the page image the OCR
was run on, so region boxes need no conversion. Pages from the Internet Archive
instead get the size of IA's full-resolution scan, with IA's IIIF image service so
viewers can zoom into it, and the boxes are scaled up to match (viewers draw the
service's image at its own size, whatever the canvas says).
"""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Callable

from .enrich import toc_entries
from .export import citation, human_date
from .folios import printed, printed_pages
from .ocr import NO_TEXT_STATUSES, region_text
from .project import Project, page_name

CONTEXT = "http://iiif.io/api/presentation/3/context.json"
RIGHTS_PREFIXES = ("http://creativecommons.org/", "https://creativecommons.org/",
                   "http://rightsstatements.org/", "https://rightsstatements.org/")


def lang(value: str, code: str = "en") -> dict:
    return {code: [value]}


def pair(label: str, value: str) -> dict:
    return {"label": lang(label), "value": lang(value)}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def issue_label(title_name: str, rec: dict) -> str:
    when = human_date(rec.get("date"), rec.get("date_precision")) or "undated"
    return f"{title_name}, {when}"


class Writer:
    """Writes the IIIF files for a site built at `out`, published at `root`
    (an absolute URL ending in /). `site_url(path)` turns a site path, as the
    Catalog makes them (/base/t/...), into an absolute URL; `image(issue, n)`
    and `thumb(issue, n)` give the image URLs the site uses for a page."""

    def __init__(self, project: Project, out: Path, root: str, *,
                 site_url: Callable[[str], str], page_href: Callable,
                 issue_href: Callable, image: Callable, thumb: Callable,
                 image_size: Callable | None = None):
        self.project, self.out, self.root = project, out, root
        self.site_url, self.page_href, self.issue_href = site_url, page_href, issue_href
        self.image, self.thumb = image, thumb
        # (issue, n) -> (width, height) of the image at image(issue, n)
        self.image_size = image_size or (lambda issue, n: None)

    @staticmethod
    def scale(page: dict) -> float:
        """Canvas units per pixel of our page image: > 1 when the canvas is IA's
        full-resolution scan."""
        if page.get("iiif_service") and page.get("iiif_width"):
            return page["iiif_width"] / page["width"]
        return 1.0

    def id(self, path: str) -> str:
        return self.root + "iiif/" + path

    def issue_base(self, issue) -> str:
        return f"{issue.slug}/{urllib.parse.quote(issue.key)}/"

    def manifest_id(self, issue) -> str:
        return self.id(self.issue_base(issue) + "manifest.json")

    # --- one issue ---------------------------------------------------------

    def manifest(self, issue, full_text: dict | None) -> dict:
        title = self.project.titles[issue.slug]
        rec = issue.rec
        base = self.issue_base(issue)
        numbering = printed_pages(issue.dir, rec)
        src = rec.get("source", {})

        meta = [pair("Title", title.name)]
        if rec.get("date"):
            meta.append(pair("Date", human_date(rec["date"], rec.get("date_precision"))))
        for k in ("volume", "number"):
            if rec.get(k):
                meta.append(pair(k.title(), str(rec[k])))
        meta.append(pair("Pages", str(len(rec["pages"]))))
        if src.get("publisher"):
            meta.append(pair("Publisher", src["publisher"]))
        if src.get("url"):
            meta.append(pair("Source", f'<a href="{src["url"]}">{src["url"]}</a>'))
        elif src.get("filename"):
            meta.append(pair("Source", src["filename"]))
        if src.get("rights"):
            meta.append(pair("Rights", src["rights"]))
        meta.append(pair("Citation", citation(title.name, rec, 1).rsplit(", p. ", 1)[0]))

        m = {
            "@context": CONTEXT,
            "id": self.manifest_id(issue),
            "type": "Manifest",
            "label": lang(issue_label(title.name, rec)),
            "metadata": meta,
            "summary": lang(f"{len(rec['pages'])} pages. Text by OCR "
                            f"({(full_text or {}).get('ocr', {}).get('engine', 'not yet')}); "
                            f"it contains errors."),
            "viewingDirection": "left-to-right",
            "homepage": [{"id": self.site_url(self.issue_href(issue)), "type": "Text",
                          "label": lang(issue_label(title.name, rec)), "format": "text/html"}],
            "partOf": [{"id": self.id(f"{issue.slug}/collection.json"), "type": "Collection"}],
            "requiredStatement": pair("Attribution", _attribution(src)),
            "items": [self.canvas(issue, p, base, numbering) for p in rec["pages"]],
        }
        if rec.get("date") and rec.get("date_precision") == "day":
            m["navDate"] = f"{rec['date']}T00:00:00Z"
        rights = src.get("rights") or ""
        if rights.startswith(RIGHTS_PREFIXES):
            m["rights"] = rights.replace("https://", "http://", 1)
        if src.get("url"):
            m["seeAlso"] = [{"id": src["url"], "type": "Text", "format": "text/html",
                             "label": lang("Source")}]
        if full_text:
            m["rendering"] = [{"id": self.id(base + "full_text.txt"), "type": "Text",
                               "label": lang("Full text (OCR)"), "format": "text/plain"}]
        ranges = self.ranges(issue, base)
        if ranges:
            m["structures"] = ranges
        thumb = self.thumb(issue, rec["pages"][0]["page"]) if rec["pages"] else None
        if thumb:
            m["thumbnail"] = [{"id": thumb, "type": "Image", "format": "image/jpeg"}]
        return m

    def canvas_id(self, base: str, n: int) -> str:
        return self.id(f"{base}canvas/p{n}")

    def canvas(self, issue, page: dict, base: str, numbering: dict) -> dict:
        n = page["page"]
        cid = self.canvas_id(base, n)
        body = {"id": self.image(issue, n), "type": "Image", "format": "image/jpeg"}
        if page.get("iiif_service") and page.get("iiif_width"):
            w, h = page["iiif_width"], page["iiif_height"]
            body.update(width=w, height=h, service=[{
                "id": page["iiif_service"], "type": "ImageService3", "profile": "level2"}])
        else:
            w, h = page["width"], page["height"]
            size = self.image_size(issue, n)
            if size:
                body.update(width=size[0], height=size[1])
        c = {
            "id": cid, "type": "Canvas", "label": lang(f"p. {printed(numbering, n)}", "none"),
            "width": w, "height": h,
            "thumbnail": [{"id": self.thumb(issue, n), "type": "Image", "format": "image/jpeg"}],
            "homepage": [{"id": self.site_url(self.page_href(issue, n)), "type": "Text",
                          "label": lang("Read this page"), "format": "text/html"}],
            "items": [{"id": f"{cid}/page", "type": "AnnotationPage", "items": [{
                "id": f"{cid}/page/image", "type": "Annotation", "motivation": "painting",
                "body": body, "target": cid}]}],
        }
        if (issue.dir / f"{page_name(n)}.json").exists():
            c["annotations"] = [{"id": self.id(f"{base}text/p{n}.json"),
                                 "type": "AnnotationPage"}]
        return c

    def text_page(self, issue, page: dict, base: str) -> dict | None:
        """A page's OCR regions as supplementing annotations, in reading order."""
        n = page["page"]
        f = issue.dir / f"{page_name(n)}.json"
        if not f.exists():
            return None
        cid = self.canvas_id(base, n)
        k = self.scale(page)
        items = []
        for r in json.loads(f.read_text()).get("regions", []):
            text = region_text(r.get("text", ""))
            if r.get("status") in NO_TEXT_STATUSES or not text:
                continue
            b = {key: round(v * k) for key, v in r["bbox"].items()}
            items.append({
                "id": f"{cid}/text/{r['id']}", "type": "Annotation",
                "motivation": "supplementing",
                "body": {"type": "TextualBody", "value": text, "format": "text/plain"},
                "target": f"{cid}#xywh={b['x0']},{b['y0']},{b['x1'] - b['x0']},"
                          f"{b['y1'] - b['y0']}",
            })
        return {"@context": CONTEXT, "id": self.id(f"{base}text/p{n}.json"),
                "type": "AnnotationPage", "items": items}

    def ranges(self, issue, base: str) -> list[dict]:
        """The table of contents (paperpress enrich) as IIIF ranges, one per article."""
        f = issue.dir / "toc.json"
        if not f.exists():
            return []
        arts = toc_entries(json.loads(f.read_text()))
        if not arts:
            return []
        items = []
        for a in arts:
            r = {"id": self.id(f"{base}range/{a['id']}"), "type": "Range",
                 "label": lang(a["title"]),
                 "items": [{"id": self.canvas_id(base, p), "type": "Canvas"}
                           for p in a["pages"]]}
            if a.get("author"):
                r["metadata"] = [pair("Author", a["author"])]
            items.append(r)
        return [{"id": self.id(f"{base}range/contents"), "type": "Range",
                 "label": lang("Contents"), "items": items}]

    def write_issue(self, issue) -> None:
        base = self.issue_base(issue)
        folder = self.out / "iiif" / issue.slug / issue.key
        ft_file = issue.dir / "full_text.json"
        full = json.loads(ft_file.read_text()) if ft_file.exists() else None
        _write(folder / "manifest.json", self.manifest(issue, full))
        for p in issue.rec["pages"]:
            page = self.text_page(issue, p, base)
            if page:
                _write(folder / "text" / f"p{p['page']}.json", page)
        if full:
            text = "\n\n".join(f"--- page {pg['page']} ---\n{pg['text']}"
                               for pg in full["pages"])
            (folder / "full_text.txt").write_text(text + "\n", encoding="utf-8")

    # --- collections -------------------------------------------------------

    def write_collections(self, issues_by_title: dict[str, list]) -> None:
        titles = []
        for slug, issues in issues_by_title.items():
            title = self.project.titles[slug]
            items = []
            for issue in issues:
                ref = {"id": self.manifest_id(issue), "type": "Manifest",
                       "label": lang(issue_label(title.name, issue.rec))}
                if issue.rec.get("date") and issue.rec.get("date_precision") == "day":
                    ref["navDate"] = f"{issue.rec['date']}T00:00:00Z"
                if issue.rec["pages"]:
                    ref["thumbnail"] = [{"id": self.thumb(issue, issue.rec["pages"][0]["page"]),
                                         "type": "Image", "format": "image/jpeg"}]
                items.append(ref)
            cid = self.id(f"{slug}/collection.json")
            _write(self.out / "iiif" / slug / "collection.json", {
                "@context": CONTEXT, "id": cid, "type": "Collection",
                "label": lang(title.name), "items": items,
                "homepage": [{"id": self.site_url(f"t/{slug}/"), "type": "Text",
                              "label": lang(title.name), "format": "text/html"}],
                "partOf": [{"id": self.id("collection.json"), "type": "Collection"}]})
            titles.append({"id": cid, "type": "Collection", "label": lang(title.name)})
        _write(self.out / "iiif" / "collection.json", {
            "@context": CONTEXT, "id": self.id("collection.json"), "type": "Collection",
            "label": lang(self.project.name), "items": titles,
            "homepage": [{"id": self.root, "type": "Text", "label": lang(self.project.name),
                          "format": "text/html"}]})


def _attribution(src: dict) -> str:
    if src.get("type") == "internet_archive":
        return f'Scans from the Internet Archive: <a href="{src["url"]}">{src["url"]}</a>'
    return "Scans supplied by the archive's creator"
