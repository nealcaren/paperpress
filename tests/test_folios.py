import json

import pytest

from paperpress.export import export
from paperpress.folios import folio_candidates, infer_offset, printed, printed_pages
from paperpress.project import Project, init_project, write_issue


def page(n, regions, h=1000):
    return {"page": n, "width": 800, "height": h, "regions": [
        {"id": f"r{i}", "text": t, "status": "ok",
         "bbox": {"x0": 0, "y0": y, "x1": 800, "y1": y + 30}} for i, (y, t) in enumerate(regions)]}


def test_folio_candidates_only_short_header_and_footer_text():
    p = page(2, [(10, "42 THE WOMAN'S JOURNAL, FEBRUARY 10, 1912"),   # header
                 (500, "Section 5 of the bill"),                         # body: ignored
                 (960, "Price 5"),                                       # footer
                 (20, "x" * 130 + " 7")])                                # too long
    assert folio_candidates(p, "1912-02-10") == {42, 5}                  # not 10, not 1912


@pytest.mark.parametrize("evidence,expected", [
    ({1: set(), 2: {42}, 3: {43}, 4: {44, 33}}, (40, 3)),   # consistent run wins
    ({1: set(), 2: {42}}, (None, 1)),                       # one vote isn't enough
    ({1: {9}, 2: {10}, 3: {50}, 4: {51}}, (None, 2)),       # a tie: offsets 8 and 47
    ({1: {1}, 2: {2}, 3: {3}}, (0, 3)),                     # printed == position
    ({1: set(), 2: set()}, (None, 0)),
])
def test_infer_offset(evidence, expected):
    assert infer_offset(evidence) == expected


@pytest.fixture
def project(tmp_path):
    cfg = init_project(tmp_path)
    cfg.write_text(cfg.read_text() + '[[titles]]\nslug = "wj"\nname = "The Woman\'s Journal"\n')
    p = Project.load(tmp_path)
    d = p.title_dir("wj") / "1912-02-10"
    d.mkdir(parents=True)
    for n in range(1, 5):
        folio = [] if n == 1 else [(10, f"{40 + n} THE WOMAN'S JOURNAL")]
        (d / f"page_0{n}.json").write_text(json.dumps(page(n, folio + [(400, f"text {n}")])))
    write_issue(d, {"title": "wj", "date": "1912-02-10", "date_precision": "day",
                    "volume": "43", "number": "6", "source": {"id": "x"},
                    "pages": [{"page": n, "image": f"images/page_0{n}.jpg"} for n in range(1, 5)]})
    (d / "full_text.json").write_text(json.dumps({"pages": [
        {"page": n, "text": f"text {n}"} for n in range(1, 5)]}))
    return p, d


def test_printed_pages_detected_and_overridden(project):
    p, d = project
    rec = json.loads((d / "issue.json").read_text())
    n = printed_pages(d, rec)
    assert (n["offset"], n["how"]) == (40, "detected")
    assert n["pages"] == {1: 41, 2: 42, 3: 43, 4: 44}
    assert printed(n, [1, 4]) == [41, 44] and printed(n, 2) == 42
    rec["printed_offset"] = 100
    assert printed_pages(d, rec)["pages"][1] == 101
    assert printed({"pages": {}}, 3) == 3                       # unknown: keep position


def test_source_page_numbers_count_when_confident(project):
    p, d = project
    rec = json.loads((d / "issue.json").read_text())
    for f in d.glob("page_0*.json"):                            # no folios in our OCR...
        data = json.loads(f.read_text())
        data["regions"] = [r for r in data["regions"] if r["bbox"]["y0"] > 100]
        f.write_text(json.dumps(data))
    rec["pages"][1].update(source_page_number="42", source_page_prob=95)
    rec["pages"][2].update(source_page_number="43", source_page_prob=90)
    rec["pages"][3].update(source_page_number="15", source_page_prob=40)   # unsure: ignored
    assert printed_pages(d, rec)["offset"] == 40


def test_export_cites_printed_pages(project, tmp_path):
    p, d = project
    export(p, ["wj"], tmp_path / "out")
    rows = [json.loads(l) for l in (tmp_path / "out" / "pages.jsonl").read_text().splitlines()]
    assert rows[1]["page"] == 2 and rows[1]["printed_page"] == 42
    assert rows[1]["citation"] == "The Woman's Journal, vol. 43, no. 6, February 10, 1912, p. 42"
