"""The OCR stage, with a fake engine standing in for newspaper-ocr."""
import json

import pytest

from paperpress import ocr
from paperpress.project import Project, init_project, write_issue


class FakeEngine:
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def describe(self):
        return {"engine": "fake 1.0", "detector": "d", "recognizer": "r"}

    def page(self, image):
        self.calls.append(image.name)
        if image.name == self.fail_on:
            raise RuntimeError("engine blew up")
        return {"width": 100, "height": 200, "regions": [
            {"id": "r0", "label": "title", "text": "Duff Withdraws", "status": "ok"},
            {"id": "r1", "label": "text", "text": "an-\nticipated support\nfrom the", "status": "ok"},
            {"id": "r2", "label": "text", "text": "[OCR timeout]", "status": "timeout"},
        ]}


@pytest.fixture
def issue(tmp_path):
    init_project(tmp_path)
    d = tmp_path / "titles" / "dth" / "1960-03-04"
    (d / "images").mkdir(parents=True)
    for n in (1, 2):
        (d / "images" / f"page_0{n}.jpg").write_bytes(b"jpeg")
    write_issue(d, {"title": "dth", "date": "1960-03-04", "source": {"id": "x"},
                    "pages": [{"page": n, "image": f"images/page_0{n}.jpg"} for n in (1, 2)]})
    return d


def test_region_text_rejoins_hyphenation_and_lines():
    assert ocr.region_text("an-\nticipated support\nfrom the") == "anticipated support from the"
    # a capitalized word after the break is a real hyphenated compound
    assert ocr.region_text("Anglo-\nSaxon") == "Anglo- Saxon"


def test_ocr_issue_writes_pages_then_full_text(issue):
    engine = FakeEngine()
    s = ocr.ocr_issue(issue, engine)
    assert (s["pages"], s["done"], s["regions"], s["flagged"]) == (2, 2, 6, 2)
    page = json.loads((issue / "page_01.json").read_text())
    assert page["page"] == 1 and page["image"] == "images/page_01.jpg"
    assert page["regions"][1]["text"] == "an-\nticipated support\nfrom the"   # raw, untouched
    assert page["ocr"]["engine"] == "fake 1.0" and "seconds" in page["ocr"]
    full = json.loads((issue / "full_text.json").read_text())
    assert full["date"] == "1960-03-04" and full["ocr"]["recognizer"] == "r"
    # timeout placeholder text is left out of the issue text
    assert full["pages"][0]["text"] == "Duff Withdraws\n\nanticipated support from the"
    assert full["pages"][0]["flagged"] == 1
    assert ocr.is_done(issue)


def test_resume_skips_finished_pages(issue):
    with pytest.raises(RuntimeError):
        ocr.ocr_issue(issue, FakeEngine(fail_on="page_02.jpg"))
    assert (issue / "page_01.json").exists() and not ocr.is_done(issue)
    engine = FakeEngine()
    s = ocr.ocr_issue(issue, engine)
    assert engine.calls == ["page_02.jpg"] and s["done"] == 1 and ocr.is_done(issue)
    engine = FakeEngine()
    ocr.ocr_issue(issue, engine, force=True)
    assert engine.calls == ["page_01.jpg", "page_02.jpg"]


def test_unknown_engine_setting():
    with pytest.raises(ValueError, match="unknown"):
        ocr.NewspaperOCR({"detecter": "x"})


def test_ocr_settings_come_from_paper_toml(tmp_path):
    cfg = init_project(tmp_path)
    cfg.write_text(cfg.read_text() + '[ocr]\nrecognizer = "glm-ocr"\n')
    assert Project.load(tmp_path).ocr == {"recognizer": "glm-ocr"}
