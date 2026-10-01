import csv
import json

import pytest

from paperpress.export import citation, export, human_date, page_span
from paperpress.project import Project, init_project, write_issue


def make_issue(project, slug, folder, rec, texts):
    d = project.title_dir(slug) / folder
    d.mkdir(parents=True)
    write_issue(d, {"title": slug, **rec,
                    "pages": [{"page": n, "image": f"images/page_{n:02d}.jpg",
                               "source_leaf": n - 1} for n in range(1, len(texts) + 1)]})
    if texts:
        (d / "full_text.json").write_text(json.dumps({
            "ocr": {"engine": "newspaper-ocr 0.10.0"},
            "pages": [{"page": n, "text": t} for n, t in enumerate(texts, start=1)]}))
    return d


@pytest.fixture
def project(tmp_path):
    cfg = init_project(tmp_path)
    cfg.write_text(cfg.read_text() + '[[titles]]\nslug = "wj"\nname = "The Woman\'s Journal"\n'
                   '[[titles]]\nslug = "dth"\nname = "The Daily Tar Heel"\n')
    p = Project.load(tmp_path)
    make_issue(p, "wj", "1912-02-03",
               {"date": "1912-02-03", "date_precision": "day", "volume": "43", "number": "5",
                "source": {"type": "internet_archive", "id": "sim_wj",
                           "url": "https://archive.org/details/sim_wj", "rights": "PD"}},
               ["Votes for women, now.", "Page two, with \"quotes\", and\ncommas"])
    make_issue(p, "dth", "1960-03-04",
               {"date": "1960-03-04", "date_precision": "day",
                "source": {"type": "pdf", "id": "abc", "path": "/scans/dth_03041960.pdf"}},
               ["Duff Withdraws"])
    make_issue(p, "dth", "1960-03-11", {"date": "1960-03-11", "source": {"id": "def"}}, [])
    return p


def test_human_date_and_citation():
    assert human_date("1960-03-04", "day") == "March 4, 1960"
    assert human_date("1915-05-01", "month") == "May 1915"
    rec = {"date": "1912-02-03", "date_precision": "day", "volume": "43", "number": "5"}
    assert citation("The Woman's Journal", rec, 2) == \
        "The Woman's Journal, vol. 43, no. 5, February 3, 1912, p. 2"
    assert citation("The Suffragist", {"date": None, "volume": "1"}, 3) == \
        "The Suffragist, vol. 1, undated, p. 3"


def test_export(project, tmp_path):
    s = export(project, ["wj", "dth"], tmp_path / "export", txt=True)
    assert (s["issues"], s["pages"], s["words"]) == (2, 3, 12)
    assert s["skipped"] == ["titles/dth/1960-03-11"]          # not OCR'd yet
    out = tmp_path / "export"

    rows = [json.loads(l) for l in (out / "pages.jsonl").read_text().splitlines()]
    assert [r["id"] for r in rows] == ["wj_1912-02-03_p01", "wj_1912-02-03_p02",
                                       "dth_1960-03-04_p01"]
    wj2 = rows[1]
    assert wj2["citation"] == "The Woman's Journal, vol. 43, no. 5, February 3, 1912, p. 2"
    assert wj2["source_url"] == "https://archive.org/details/sim_wj/page/n1"
    assert wj2["image"] == "titles/wj/1912-02-03/images/page_02.jpg"
    assert wj2["ocr_engine"] == "newspaper-ocr 0.10.0"
    assert rows[2]["source_file"] == "/scans/dth_03041960.pdf" and rows[2]["source_url"] is None

    with (out / "pages.csv").open(encoding="utf-8-sig", newline="") as fh:
        csv_rows = list(csv.DictReader(fh))
    assert csv_rows[1]["text"] == "Page two, with \"quotes\", and\ncommas"   # survives CSV
    with (out / "issues.csv").open(encoding="utf-8-sig", newline="") as fh:
        issues = list(csv.DictReader(fh))
    assert [(i["title"], i["pages"], i["words"], i["rights"]) for i in issues] == \
        [("wj", "2", "10", "PD"), ("dth", "1", "2", "")]
    assert (out / "txt" / "dth_1960-03-04_p01.txt").read_text() == "Duff Withdraws\n"
    assert "3 pages" in (out / "README.txt").read_text()


def test_reexport_replaces_and_failure_leaves_old(project, tmp_path, monkeypatch):
    dest = tmp_path / "export"
    export(project, ["wj"], dest, txt=True)
    export(project, ["wj"], dest)                     # no txt this time
    assert not (dest / "txt").exists()

    import paperpress.export as ex
    monkeypatch.setattr(ex, "_readme", lambda *a: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        export(project, ["wj"], dest)
    assert (dest / "pages.jsonl").exists() and not dest.with_name("export.partial").exists()


def test_page_span():
    assert page_span(3) == "p. 3" and page_span([3]) == "p. 3"
    assert page_span([5, 6, 7]) == "pp. 5–7"
    assert page_span([1, 6]) == "pp. 1, 6"
    assert page_span([6, 1, 2, 9, 10]) == "pp. 1–2, 6, 9–10"
