"""The static site: every link resolves, images and search are in place."""
import re
import urllib.parse

import pytest
from PIL import Image

from paperpress.build import BuildError, build
from paperpress.project import Project, init_project, read_issue
from paperpress.sources import ia
from test_serve import make_issue


@pytest.fixture
def project(tmp_path):
    cfg = init_project(tmp_path / "proj", "Suffrage Press")
    cfg.write_text(cfg.read_text() + '[[titles]]\nslug = "wj"\nname = "The Woman\'s Journal"\n')
    p = Project.load(tmp_path / "proj")
    make_issue(p, "wj", "1912-02-03", "1912-02-03",
               ["Votes for women in Boston.", "The suffragists marched."])
    make_issue(p, "wj", "_undated/mystery", None, ["Undated voting news."])
    return p


def broken_links(root, base):
    bad = []
    for f in root.rglob("*.html"):
        for url in re.findall(r'(?:href|src)="([^"#]+)"', f.read_text()):
            if url.startswith("http"):
                continue
            assert url.startswith(base), url
            path = root / urllib.parse.unquote(url.split("?")[0])[len(base):]
            if not (path.is_file() or (path / "index.html").is_file()):
                bad.append(url)
    return bad


def test_build_with_base_path(project, tmp_path):
    out = tmp_path / "site"
    s = build(project, out, base="suffrage-press", image_width=200)
    assert (s["issues"], s["pages"]) == (2, 3)
    assert broken_links(out, "/suffrage-press/") == []
    assert (out / ".nojekyll").exists()                         # keeps _undated/ on GH Pages
    assert (out / "pagefind" / "pagefind.js").exists()
    reader = (out / "t/wj/1912-02-03/p/2/index.html").read_text()
    assert "data-pagefind-body" in reader and 'data-pagefind-filter="title:The Woman' in reader
    assert 'data-pagefind-sort="date:1912-02-03:0002"' in reader
    with Image.open(out / "img/wj/1912-02-03/page_01.jpg") as im:
        assert im.width == 200                                  # resized for the web
    assert not out.with_name("site.partial").exists()


def test_build_without_pagefind_and_rebuild(project, tmp_path):
    out = tmp_path / "site"
    build(project, out, pagefind=False)
    assert not (out / "pagefind").exists()
    (out / "stale.html").write_text("old")
    build(project, out, pagefind=False)
    assert not (out / "stale.html").exists()                    # a rebuild replaces the site


def test_ia_images_link_to_ia_and_record_sizes(project, tmp_path, monkeypatch):
    d = project.title_dir("wj") / "1912-02-03"
    rec = read_issue(d)
    for p in rec["pages"]:
        p["iiif_service"] = f"https://iiif.example/{p['page']}"
    rec["source"]["type"] = "internet_archive"
    from paperpress.project import write_issue
    write_issue(d, rec)
    monkeypatch.setattr(ia, "working_size", lambda page: "max" if page["page"] == 1 else None)
    out = tmp_path / "site"
    build(project, out, images="ia", pagefind=False)
    p1 = (out / "t/wj/1912-02-03/p/1/index.html").read_text()
    assert 'src="https://iiif.example/1/full/max/0/default.jpg"' in p1
    assert not (out / "img/wj/1912-02-03/page_01.jpg").exists()
    assert (out / "img/wj/1912-02-03/page_02.jpg").exists()      # no working size: copied
    assert read_issue(d)["pages"][0]["iiif_size"] == "max"       # remembered for next time


def test_bad_images_option(project, tmp_path):
    with pytest.raises(BuildError):
        build(project, tmp_path / "site", images="nope")
