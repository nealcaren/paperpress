"""IIIF manifests from the static build."""
import json

import pytest

from paperpress.build import BuildError, build
from paperpress.project import Project, init_project, read_issue, write_issue
from test_serve import make_issue

URL = "https://example.org/press/"


@pytest.fixture
def project(tmp_path):
    cfg = init_project(tmp_path / "proj", "Suffrage Press")
    cfg.write_text(cfg.read_text() + '[[titles]]\nslug = "wj"\nname = "The Woman\'s Journal"\n')
    p = Project.load(tmp_path / "proj")
    make_issue(p, "wj", "1912-02-03", "1912-02-03", ["Votes for women.", "Marching."])
    make_issue(p, "wj", "_undated/mystery", None, ["Undated news."])
    d = p.title_dir("wj") / "1912-02-03"
    (d / "toc.json").write_text(json.dumps({"articles": [
        {"id": "p1a1", "title": "Votes", "type": "news", "author": "A. S. B.", "pages": [1, 2],
         "regions": [{"page": 1, "ids": ["r1"]}]},
        {"id": "p2a1", "title": "Buy hats", "type": "advertisement", "pages": [2],
         "regions": [{"page": 2, "ids": ["r0"]}]}]}))
    return p


def load(out, path):
    return json.loads((out / "iiif" / path).read_text())


def test_manifest(project, tmp_path):
    out = tmp_path / "site"
    s = build(project, out, url=URL, image_width=200, pagefind=False)
    assert s["base"] == "/press/" and s["iiif"] == URL + "iiif/collection.json"
    m = load(out, "wj/1912-02-03/manifest.json")
    assert m["id"] == URL + "iiif/wj/1912-02-03/manifest.json"
    assert m["label"] == {"en": ["The Woman's Journal, February 3, 1912"]}
    assert m["navDate"] == "1912-02-03T00:00:00Z"
    c = m["items"][0]
    assert (c["width"], c["height"]) == (400, 600)
    body = c["items"][0]["items"][0]["body"]
    assert body["id"] == "https://example.org/press/img/wj/1912-02-03/page_01.jpg"
    assert (body["width"], body["height"]) == (200, 300)          # the resized copy
    assert "service" not in body
    assert c["homepage"][0]["id"] == URL + "t/wj/1912-02-03/p/1/"
    # contents as ranges: articles only, ads and mastheads left out
    [toc] = m["structures"]
    assert [r["label"]["en"][0] for r in toc["items"]] == ["Votes"]
    assert [i["id"].rsplit("/", 1)[1] for i in toc["items"][0]["items"]] == ["p1", "p2"]
    # OCR as annotations on the page, boxes in canvas units
    text = load(out, "wj/1912-02-03/text/p1.json")
    assert text["items"][1]["body"]["value"] == "Votes for women."
    assert text["items"][1]["target"].endswith("/canvas/p1#xywh=10,70,380,520")
    assert (out / "iiif/wj/1912-02-03/full_text.txt").read_text().startswith("--- page 1")
    # linked from the issue page
    assert "/press/iiif/wj/1912-02-03/manifest.json" in \
        (out / "t/wj/1912-02-03/index.html").read_text()


def test_collections(project, tmp_path):
    out = tmp_path / "site"
    build(project, out, url=URL, pagefind=False)
    top = load(out, "collection.json")
    assert top["items"][0]["id"] == URL + "iiif/wj/collection.json"
    issues = load(out, "wj/collection.json")["items"]
    assert [i["id"].split("/iiif/")[1] for i in issues] == [
        "wj/1912-02-03/manifest.json", "wj/_undated/mystery/manifest.json"]
    assert "navDate" not in issues[1]


def test_ia_pages_use_the_full_scan(project, tmp_path):
    d = project.title_dir("wj") / "1912-02-03"
    rec = read_issue(d)
    for p in rec["pages"]:
        p.update(iiif_service=f"https://iiif.example/{p['page']}", iiif_size="400,",
                 iiif_width=1000, iiif_height=1500)
    write_issue(d, rec)
    out = tmp_path / "site"
    build(project, out, url=URL, pagefind=False)
    c = load(out, "wj/1912-02-03/manifest.json")["items"][0]
    assert (c["width"], c["height"]) == (1000, 1500)
    assert c["items"][0]["items"][0]["body"]["service"][0]["id"] == "https://iiif.example/1"
    target = load(out, "wj/1912-02-03/text/p1.json")["items"][1]["target"]
    assert target.endswith("#xywh=25,175,950,1300")              # boxes scaled 2.5x


def test_no_iiif_without_url(project, tmp_path):
    out = tmp_path / "site"
    assert build(project, out, pagefind=False)["iiif"] is None
    assert not (out / "iiif").exists()
    assert "manifest" not in (out / "t/wj/1912-02-03/index.html").read_text()


def test_url_and_base_must_agree(project, tmp_path):
    with pytest.raises(BuildError, match="base"):
        build(project, tmp_path / "site", url=URL, base="/other/", pagefind=False)
    with pytest.raises(BuildError, match="full address"):
        build(project, tmp_path / "site", url="example.org/press", pagefind=False)
