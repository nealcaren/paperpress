"""Internet Archive source, with the network replaced by canned responses."""
import json

import pytest

from paperpress.project import Project, init_project, read_issue
from paperpress.sources import ia

SVC = "https://iiif.archive.org/image/iiif/3/item%2Fp{n}.jp2"


def manifest(n_pages):
    return {"items": [
        {"width": 2000, "height": 3000, "items": [{"items": [{"body": {
            "id": SVC.format(n=i) + "/full/max/0/default.jpg",
            "service": [{"id": SVC.format(n=i), "type": "ImageService3"}]}}]}]}
        for i in range(n_pages)]}


def metadata(**meta):
    return {"metadata": {"title": "The Revolution - April 7, 1870",
                         "collection": ["newspapers"], **meta},
            "files": [{"name": "x_djvu.txt"}, {"name": "x.pdf"}]}


@pytest.fixture
def fake_ia(monkeypatch):
    calls = []
    state = {"meta": metadata(date="1870-04-07")}

    def get_json(url):
        calls.append(url)
        return state["meta"] if "/metadata/" in url else manifest(3)

    def get(url, **kw):
        calls.append(url)
        return b"OCR TEXT" if url.endswith("_djvu.txt") else b"\xff\xd8jpeg"

    monkeypatch.setattr(ia, "_get_json", get_json)
    monkeypatch.setattr(ia, "_get", get)
    state["calls"] = calls
    return state


@pytest.fixture
def project(tmp_path):
    cfg = init_project(tmp_path)
    cfg.write_text(cfg.read_text() + '[[titles]]\nslug = "revolution"\nname = "The Revolution"\n')
    return Project.load(tmp_path)


def test_fetch_issue(project, fake_ia):
    status, dest = ia.fetch_issue(project, "revolution", "revolution-1870-04-07",
                                  max_width=1000)
    assert status == "fetched"
    assert dest == project.title_dir("revolution") / "1870-04-07"
    rec = read_issue(dest)
    assert rec["date"] == "1870-04-07" and rec["date_precision"] == "day"
    assert rec["source"]["id"] == "revolution-1870-04-07"
    assert rec["source"]["ocr"] == "source/ia_ocr.txt"
    assert [p["image"] for p in rec["pages"]] == [f"images/page_0{i}.jpg" for i in (1, 2, 3)]
    assert rec["pages"][0]["width"] == 1000 and rec["pages"][0]["height"] == 1500
    assert rec["pages"][2]["iiif_service"] == SVC.format(n=2)
    assert (dest / "images" / "page_03.jpg").exists()
    assert (dest / "source" / "ia_ocr.txt").read_text() == "OCR TEXT"
    assert any(u.endswith("/full/1000,/0/default.jpg") for u in fake_ia["calls"])
    assert not list(dest.parent.glob("*.partial"))


def test_refetch_is_skipped(project, fake_ia):
    ia.fetch_issue(project, "revolution", "revolution-1870-04-07")
    n = len(fake_ia["calls"])
    assert ia.fetch_issue(project, "revolution", "revolution-1870-04-07")[0] == "skipped"
    assert len(fake_ia["calls"]) == n


def test_date_from_title_when_date_field_missing(project, fake_ia):
    fake_ia["meta"] = metadata()
    status, dest = ia.fetch_issue(project, "revolution", "revolution-1870-04-07")
    assert status == "fetched" and dest.name == "1870-04-07"
    assert read_issue(dest)["date_from"] == "title"


def test_undated_issue(project, fake_ia):
    fake_ia["meta"] = metadata(title="The Suffragist", volume="1", issue="4")
    status, dest = ia.fetch_issue(project, "revolution", "suffragist01cong_2")
    assert status == "fetched-undated"
    assert dest.parts[-2:] == ("_undated", "suffragist01cong_2")
    rec = read_issue(dest)
    assert rec["date"] is None and rec["volume"] == "1" and rec["number"] == "4"


def test_missing_item(project, fake_ia):
    fake_ia["meta"] = {}
    with pytest.raises(ia.IAError, match="no such item"):
        ia.fetch_issue(project, "revolution", "nope")


def test_high_ppi_scans_are_downsampled(project, fake_ia):
    fake_ia["meta"] = metadata(date="1914-09-05", ppi="800")
    _, dest = ia.fetch_issue(project, "revolution", "s")
    page = read_issue(dest)["pages"][0]
    assert (page["width"], page["height"], page["ppi"]) == (750, 1125, 300)
    assert any(u.endswith("/full/750,/0/default.jpg") for u in fake_ia["calls"])


def test_native_size_when_already_at_or_below_target(project, fake_ia):
    fake_ia["meta"] = metadata(date="1870-04-07", ppi="300")
    _, dest = ia.fetch_issue(project, "revolution", "r")
    assert read_issue(dest)["pages"][0]["width"] == 2000
    assert any(u.endswith("/full/max/0/default.jpg") for u in fake_ia["calls"])
    _, dest = ia.fetch_issue(project, "revolution", "r", force=True, ppi=None)
    assert read_issue(dest)["pages"][0]["ppi"] == 300


def test_failed_download_leaves_nothing_behind(project, fake_ia, monkeypatch):
    def broken(url, **kw):
        raise ia.IAError("HTTP 500")
    monkeypatch.setattr(ia, "_get", broken)
    with pytest.raises(ia.IAError):
        ia.fetch_issue(project, "revolution", "revolution-1870-04-07")
    assert not any(project.title_dir("revolution").rglob("*"))


def test_image_server_500_tries_a_neighbouring_width(project, fake_ia, monkeypatch):
    real = ia._get

    def picky(url, **kw):
        if "/full/max/" in url:          # Cantaloupe 500s on this exact size
            raise ia.IAError(f"HTTP 500 for {url}")
        return real(url, **kw)
    monkeypatch.setattr(ia, "_get", picky)
    _, dest = ia.fetch_issue(project, "revolution", "r")
    assert read_issue(dest)["pages"][0]["width"] == 1999
    assert any(u.endswith("/full/1999,/0/default.jpg") for u in fake_ia["calls"])
