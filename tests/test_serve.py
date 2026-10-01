"""Search index and the local web server, against a tiny two-issue project."""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest
from PIL import Image

from paperpress import search
from paperpress.project import Project, init_project, write_issue
from paperpress.serve import make_server


def make_issue(p, slug, folder, date, pages):
    d = p.title_dir(slug) / folder
    (d / "images").mkdir(parents=True)
    recs = []
    for n, text in enumerate(pages, start=1):
        Image.new("L", (400, 600), "white").save(d / "images" / f"page_{n:02d}.jpg")
        (d / f"page_{n:02d}.json").write_text(json.dumps({
            "page": n, "width": 400, "height": 600, "regions": [
                {"id": "r0", "label": "title", "text": "Headline", "status": "ok",
                 "bbox": {"x0": 10, "y0": 10, "x1": 390, "y1": 60}},
                {"id": "r1", "label": "plain_text", "text": text, "status": "ok",
                 "bbox": {"x0": 10, "y0": 70, "x1": 390, "y1": 590}}]}))
        recs.append({"page": n, "image": f"images/page_{n:02d}.jpg", "width": 400,
                     "height": 600})
    write_issue(d, {"title": slug, "date": date, "date_precision": "day" if date else None,
                    "source": {"id": folder}, "pages": recs})
    (d / "full_text.json").write_text(json.dumps({
        "date": date, "pages": [{"page": n, "text": f"Headline\n\n{t}"}
                                for n, t in enumerate(pages, start=1)]}))


@pytest.fixture
def project(tmp_path):
    cfg = init_project(tmp_path, "Suffrage Press")
    cfg.write_text(cfg.read_text() + '[[titles]]\nslug = "wj"\nname = "The Woman\'s Journal"\n')
    p = Project.load(tmp_path)
    make_issue(p, "wj", "1912-02-03", "1912-02-03",
               ["Votes for women in Boston.", "The suffragists marched."])
    make_issue(p, "wj", "1913-05-10", "1913-05-10", ["A suffrage parade on Fifth Avenue."])
    make_issue(p, "wj", "_undated/mystery", None, ["Undated voting news."])
    return p


def test_fts_query_is_always_safe():
    assert search.fts_query('votes "Fifth Avenue" suffrag*') == '"votes" "Fifth Avenue" "suffrag"*'
    assert search.fts_query('AND OR NOT ( " -') is not None    # FTS5 syntax is neutralised
    assert search.fts_query("   ") is None


def test_search_filters_and_stemming(project):
    total, hits = search.search(project, "voting")             # stems to "vote"
    assert {(h.folder, h.page) for h in hits} == {("1912-02-03", 1), ("_undated/mystery", 1)}
    total, _ = search.search(project, "voting", date_from="1912", date_to="1912")
    assert total == 1
    _, hits = search.search(project, '"Fifth Avenue"')
    assert [h.folder for h in hits] == ["1913-05-10"] and "<mark>Fifth Avenue</mark>" in hits[0].snippet
    assert search.search(project, "nothingmatches")[0] == 0


def test_index_rebuilds_when_ocr_changes(project):
    _, rebuilt = search.ensure_index(project)
    assert rebuilt
    assert search.ensure_index(project) == (search.index_path(project), False)
    ft = project.title_dir("wj") / "1913-05-10" / "full_text.json"
    time.sleep(0.01)
    ft.write_text(json.dumps({"date": "1913-05-10",
                              "pages": [{"page": 1, "text": "Zeppelins overhead"}]}))
    assert search.ensure_index(project)[1] is True
    assert search.search(project, "zeppelins")[0] == 1


@pytest.fixture
def server(project):
    srv = make_server(project, port=18700)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


def test_pages_render(server):
    _, _, home = get(server + "/")
    assert b"The Woman&#x27;s Journal" in home and b"3 issues" in home
    _, _, title = get(server + "/t/wj/")
    assert b"1912" in title and b"1913" in title and b"Undated" in title
    _, _, issue = get(server + "/t/wj/1912-02-03/")
    assert b"February 3, 1912" in issue and b"/t/wj/1912-02-03/p/2/" in issue
    _, _, reader = get(server + "/t/wj/1912-02-03/p/1?q=boston")
    assert b"<mark>Boston</mark>" in reader and b'class="r lab-plain_text hit"' in reader
    assert b"Page 1 of 2" in reader and b'rel="next"' in reader
    _, _, undated = get(server + "/t/wj/_undated/mystery/p/1")
    assert b"Undated voting news." in undated


def test_search_page(server):
    _, _, body = get(server + "/search?q=voting&sort=oldest")
    assert b"2 pages match" in body
    assert b"/t/wj/1912-02-03/p/1/?q=voting" in body


def test_images_and_thumbnails(server, project):
    status, ctype, data = get(server + "/img/wj/1912-02-03/2")
    assert ctype == "image/jpeg" and data[:2] == b"\xff\xd8"
    _, _, thumb = get(server + "/thumb/wj/1912-02-03/1")
    assert thumb[:2] == b"\xff\xd8"
    assert (project.root / ".paperpress" / "thumbs" / "wj" / "1912-02-03" / "page_01.jpg").exists()


@pytest.mark.parametrize("path", ["/t/wj/../../paper.toml/", "/img/wj/1912-02-03/9",
                                  "/img/wj/..%2F..%2Fpaper.toml/1", "/t/nope/",
                                  "/t/wj/1999-01-01/"])
def test_unknown_or_escaping_paths_404(server, path):
    with pytest.raises(urllib.error.HTTPError) as err:
        get(server + path)
    assert err.value.code == 404


def test_undated_sorts_last(project):
    for sort in ("oldest", "newest"):
        _, hits = search.search(project, "voting", sort=sort)
        assert hits[-1].folder == "_undated/mystery"


def test_issue_contents_and_region_link(server, project):
    d = project.title_dir("wj") / "1912-02-03"
    (d / "toc.json").write_text(json.dumps({"enrich": {"model": "m"}, "articles": [
        {"id": "p1a1", "title": "Masthead", "type": "masthead", "is_advertisement": False,
         "start_page": 1, "pages": [1], "regions": [{"page": 1, "ids": ["r0"]}]},
        {"id": "p2a1", "title": "Suffragists March", "author": "A. Writer", "type": "news",
         "is_advertisement": False, "start_page": 2, "pages": [2],
         "regions": [{"page": 2, "ids": ["r1"]}]},
        {"id": "p2a2", "title": "Hats", "type": "advertisement", "is_advertisement": True,
         "start_page": 2, "pages": [2], "regions": [{"page": 2, "ids": ["r0"]}]}]}))
    _, _, issue = get(server + "/t/wj/1912-02-03/")
    assert b"<h2>Contents</h2>" in issue and b"Suffragists March" in issue
    assert b'href="/t/wj/1912-02-03/p/2/?r=r1"' in issue and b"A. Writer" in issue
    assert b"1 advertisement<" in issue and b">Masthead<" not in issue
    _, _, reader = get(server + "/t/wj/1912-02-03/p/2/?r=r1")
    assert b'data-select="r1"' in reader
