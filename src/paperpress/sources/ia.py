"""Internet Archive source: fetch periodical issues (one IA item = one issue).

For each item we read its metadata, take the page list from IA's IIIF
Presentation 3 manifest, and download every page through IA's IIIF image
service. IA's own OCR (`_djvu.txt`) is kept in source/ as a baseline to compare
our OCR against. The image-service URLs are recorded per page, so a reader can
later point at IA's tiles instead of hosting images itself.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .. import __version__
from ..dates import parse_date
from ..project import Project, page_name, staged_issue, write_issue

USER_AGENT = f"paperpress/{__version__} (+https://github.com/nealcaren/paperpress)"
META_URL = "https://archive.org/metadata/{id}"
MANIFEST_URL = "https://iiif.archive.org/iiif/3/{id}/manifest.json"
DETAILS_URL = "https://archive.org/details/{id}"
DEFAULT_PPI = 300
SCRAPE_URL = "https://archive.org/services/search/v1/scrape"


class IAError(Exception):
    pass


RETRY_STATUSES = (429, 500, 502, 503, 504)


def _get(url: str, *, retries: int = 4, timeout: int = 60,
         retry_statuses: tuple[int, ...] = RETRY_STATUSES) -> bytes:
    """GET with a polite User-Agent and backoff on throttling/server errors."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in retry_statuses or attempt == retries:
                raise IAError(f"HTTP {e.code} for {url}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries:
                raise IAError(f"{e} for {url}") from e
        time.sleep(2 ** attempt * 2)
    raise AssertionError("unreachable")


def _get_json(url: str) -> dict:
    return json.loads(_get(url))


def search(query: str, limit: int | None = None) -> Iterator[dict]:
    """Yield {identifier, date, title} for every item matching an IA query."""
    cursor, n = None, 0
    while True:
        params = {"q": query, "fields": "identifier,date,title", "count": 1000}
        if cursor:
            params["cursor"] = cursor
        data = _get_json(f"{SCRAPE_URL}?{urllib.parse.urlencode(params)}")
        for item in data.get("items", []):
            yield item
            n += 1
            if limit and n >= limit:
                return
        cursor = data.get("cursor")
        if not cursor:
            return


@dataclass
class Page:
    index: int            # position among the item's pages: IA's /page/n<index> URLs
    leaf: int             # scan leaf number (scandata), which page_numbers.json keys on
    width: int
    height: int
    service: str          # IIIF image service base URL


def manifest_pages(manifest: dict) -> list[Page]:
    """Pages from a IIIF v3 manifest. Each canvas is labelled with its leaf
    number, which is not always its position (cover leaves can be skipped)."""
    pages = []
    for index, canvas in enumerate(manifest.get("items", [])):
        body = canvas["items"][0]["items"][0]["body"]
        services = body.get("service") or []
        service = services[0]["id"] if services else body["id"].rsplit("/full/", 1)[0]
        label = _first((canvas.get("label") or {}).get("none"))
        leaf = _int(label)
        pages.append(Page(index=index, leaf=index if leaf is None else leaf,
                          width=canvas["width"], height=canvas["height"], service=service))
    return pages


def page_numbers(identifier: str, files: list[dict]) -> dict[int, tuple[str, int | None]]:
    """IA's printed-page-number guesses, {leaf: (number, probability 0-100)}.
    Best effort: an item without the file, or a failed fetch, gives {}."""
    name = next((f["name"] for f in files if f["name"].endswith("_page_numbers.json")), None)
    if not name:
        return {}
    try:
        data = json.loads(_get(f"https://archive.org/download/{identifier}/"
                               f"{urllib.parse.quote(name)}"))
    except (IAError, json.JSONDecodeError):
        return {}
    return {p["leafNum"]: (p["pageNumber"], p.get("pageProb"))
            for p in data.get("pages", []) if p.get("pageNumber")}


def _attach_page_numbers(records: list[dict], numbers: dict) -> None:
    for r in records:
        num, prob = numbers.get(r["source_leaf"], (None, None))
        r["source_page_number"], r["source_page_prob"] = num, prob


def _first(v):
    return v[0] if isinstance(v, list) and v else v


def issue_date(meta: dict) -> tuple[str, str, str] | None:
    """(date, precision, where-found) from the item's date field, else its title."""
    for field in ("date", "title"):
        found = parse_date(str(_first(meta.get(field)) or ""))
        if found:
            return (*found, field)
    return None


def _int(v) -> int | None:
    try:
        return int(float(_first(v)))
    except (TypeError, ValueError):
        return None


def target_width(page: Page, native_ppi: int | None, ppi: int | None,
                 max_width: int | None) -> int:
    scale = 1.0
    if native_ppi and ppi and native_ppi > ppi:
        scale = ppi / native_ppi
    if max_width:
        scale = min(scale, max_width / page.width)
    return round(page.width * scale)


def _fetch_image(page: Page, width: int) -> tuple[bytes, int]:
    """Download one page image at `width`, returning (jpeg bytes, actual width).

    IA's image server (Cantaloupe) fails with HTTP 500 on some exact
    page/width combinations, deterministically, while neighbouring widths
    work. So a 500 means "try a width a pixel or two away", not "retry the same
    URL". It also rejects full/max on very large scans, so "max" is only used
    when no downsampling is needed.
    """
    candidates = [width] + [w for d in (1, 2, 3, 5, 8) for w in (width - d, width + d)
                            if 0 < w <= page.width]
    last = None
    for w in candidates:
        size = "max" if w == page.width else f"{w},"
        try:
            return _get(f"{page.service}/full/{size}/0/default.jpg",
                        retry_statuses=(429, 502, 503, 504)), w
        except IAError as e:
            if "HTTP 500" not in str(e):
                raise
            last = e
    raise last


def _download_pages(tmp: Path, pages: list[Page], *, native_ppi, ppi, max_width,
                    workers) -> list[dict]:
    def one(n_page):
        n, p = n_page
        w = target_width(p, native_ppi, ppi, max_width)
        name = f"{page_name(n)}.jpg"
        data, w = _fetch_image(p, w)
        (tmp / "images" / name).write_bytes(data)
        return {"page": n, "image": f"images/{name}",
                "width": w, "height": round(p.height * w / p.width),
                "ppi": round(native_ppi * w / p.width) if native_ppi else None,
                "source_index": p.index, "source_leaf": p.leaf, "iiif_service": p.service}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, enumerate(pages, start=1)))


def build_issue_record(identifier: str, meta: dict, title_slug: str,
                       pages: list[dict], ocr_file: str | None) -> dict:
    found = issue_date(meta)
    m = {k: _first(meta.get(k)) for k in
         ("title", "volume", "issue", "publisher", "licenseurl", "rights",
          "possible-copyright-status")}
    # some periodical items (e.g. sim_ microfilm) put the copyright status in `publisher`
    if m["publisher"] and "copyright" in str(m["publisher"]).lower():
        m["possible-copyright-status"] = m["possible-copyright-status"] or m["publisher"]
        m["publisher"] = None
    collections = meta.get("collection") or []
    return {
        "title": title_slug,
        "date": found[0] if found else None,
        "date_precision": found[1] if found else None,
        "date_from": found[2] if found else None,
        "volume": m["volume"],
        "number": m["issue"],
        "source": {
            "type": "internet_archive",
            "id": identifier,
            "url": DETAILS_URL.format(id=identifier),
            "label": m["title"],
            "publisher": m["publisher"],
            "collection": collections if isinstance(collections, list) else [collections],
            "rights": m["licenseurl"] or m["rights"] or m["possible-copyright-status"],
            "ocr": ocr_file,
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fetched_by": f"paperpress {__version__}",
        "pages": pages,
    }


def fetch_issue(project: Project, title_slug: str, identifier: str, *,
                ppi: int | None = DEFAULT_PPI, max_width: int | None = None,
                force: bool = False, workers: int = 2) -> tuple[str, Path]:
    """Fetch one IA item into the project. Returns (status, issue_dir).

    Pages scanned above `ppi` are downsampled to it (None keeps native size);
    `max_width` caps the pixel width as well.

    status is "skipped" (already fetched), "fetched", or "fetched-undated".
    """
    existing = project.find_issue(title_slug, identifier)
    if existing and not force:
        return "skipped", existing

    meta_all = _get_json(META_URL.format(id=identifier))
    meta = meta_all.get("metadata")
    if not meta:
        raise IAError(f"{identifier}: no such item (or it is dark/restricted)")
    pages = manifest_pages(_get_json(MANIFEST_URL.format(id=identifier)))
    if not pages:
        raise IAError(f"{identifier}: IIIF manifest has no pages")

    found = issue_date(meta)
    dest = existing or project.new_issue_dir(title_slug, found[0] if found else None, identifier)
    with staged_issue(dest) as tmp:
        records = _download_pages(tmp, pages, native_ppi=_int(meta.get("ppi")),
                                  ppi=ppi, max_width=max_width, workers=workers)
        _attach_page_numbers(records, page_numbers(identifier, meta_all.get("files", [])))
        ocr_file = None
        djvu = next((f["name"] for f in meta_all.get("files", [])
                     if f["name"].endswith("_djvu.txt")), None)
        if djvu:
            url = (f"https://archive.org/download/{identifier}/"
                   f"{urllib.parse.quote(djvu)}")
            (tmp / "source" / "ia_ocr.txt").write_bytes(_get(url))
            ocr_file = "source/ia_ocr.txt"
        write_issue(tmp, build_issue_record(identifier, meta, title_slug, records, ocr_file))
    return ("fetched" if found else "fetched-undated"), dest


def refresh_issue(issue_dir: Path) -> None:
    """Update an already-fetched issue's page and rights metadata from IA without
    downloading the images again (for issues fetched by older versions)."""
    from ..project import read_issue

    rec = read_issue(issue_dir)
    identifier = rec["source"]["id"]
    meta_all = _get_json(META_URL.format(id=identifier))
    pages = manifest_pages(_get_json(MANIFEST_URL.format(id=identifier)))
    if len(pages) != len(rec["pages"]):
        raise IAError(f"{identifier}: IA now has {len(pages)} pages, the issue has "
                      f"{len(rec['pages'])}; re-fetch it with --force")
    for r, p in zip(rec["pages"], pages):
        r["source_index"], r["source_leaf"] = p.index, p.leaf
    _attach_page_numbers(rec["pages"], page_numbers(identifier, meta_all.get("files", [])))
    fresh = build_issue_record(identifier, meta_all.get("metadata", {}), rec["title"],
                               rec["pages"], rec["source"].get("ocr"))
    rec["source"]["rights"] = fresh["source"]["rights"]
    rec["source"]["publisher"] = fresh["source"]["publisher"]
    write_issue(issue_dir, rec)
