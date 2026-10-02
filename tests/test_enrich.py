"""The enrich stage, with a fake LLM that answers like the real one."""
import json
import re

import pytest

from paperpress import enrich as en
from paperpress.export import export
from paperpress.project import Project, init_project, write_issue

PAGES = {
    1: [("r0", "masthead", "THE SUFFRAGIST"), ("r1", "title", "NEWPORT CONFERENCE"),
        ("r2", "plain_text", "The conference opened. Continued on page 2"),
        ("r3", "plain_text", "Buy Hats at Smith's")],
    2: [("r0", "plain_text", "(Continued from page 1) and closed with a vote."),
        ("r1", "title", "Nevada"), ("r2", "plain_text", "News from Nevada. MABEL VERNON")],
}


class FakeLLM:
    """Answers page prompts from PAGES and the stitch prompt with one merge."""

    def __init__(self, bad_first=False, masthead=None):
        self.calls, self.bad_first, self.prompts = [], bad_first, []
        self.masthead = masthead or {"volume": None, "number": None, "evidence": ""}

    def __call__(self, prompt, model, temperature=0):
        self.calls.append((model, temperature))
        self.prompts.append(prompt)
        if prompt.startswith("Find the volume"):
            return json.dumps(self.masthead)
        if self.bad_first and len(self.calls) == 1:
            return "not json at all"
        if prompt.startswith("These are the articles"):
            return json.dumps({"merges": [["p1a2", "p2a1"], ["p1a2", "nope"]]})
        page = int(re.search(r"for page (\d+)", prompt)[1])
        if page == 1:
            arts = [{"title": "The Suffragist", "type": "masthead", "region_ids": ["r0"]},
                    {"title": "Newport Conference", "type": "report",
                     "region_ids": ["r1", "r2", "r99"], "continued_to": 2},
                    {"title": "Smith's Hats", "type": "advertisement", "is_advertisement": True,
                     "region_ids": ["r3", "r2"]}]          # r2 already taken; r99 unknown
        else:
            arts = [{"title": "[Continuation]", "type": "report", "region_ids": ["r0"]},
                    {"title": "Nevada", "type": "report", "author": "Mabel Vernon",
                     "author_confidence": "low", "region_ids": ["r1", "r2"]},
                    {"title": "Ghost", "region_ids": []}]
        return json.dumps({"articles": arts})


@pytest.fixture
def project(tmp_path):
    cfg = init_project(tmp_path)
    cfg.write_text(cfg.read_text() + '[[titles]]\nslug = "suff"\nname = "The Suffragist"\n'
                   '[enrich]\nmodel = "cheap/model"\n')
    p = Project.load(tmp_path)
    d = p.title_dir("suff") / "1914-09-05"
    (d / "images").mkdir(parents=True)
    for n, regions in PAGES.items():
        (d / f"page_0{n}.json").write_text(json.dumps({"page": n, "width": 100, "height": 100,
            "regions": [{"id": i, "label": lab, "text": t, "status": "ok",
                         "bbox": {"x0": 0, "y0": 0, "x1": 10, "y1": 10}}
                        for i, lab, t in regions]}))
    write_issue(d, {"title": "suff", "date": "1914-09-05", "date_precision": "day",
                    "volume": "2", "number": "36", "source": {"id": "x"},
                    "pages": [{"page": n, "image": f"images/page_0{n}.jpg"} for n in PAGES]})
    (d / "full_text.json").write_text(json.dumps({"date": "1914-09-05", "pages": [
        {"page": n, "text": " ".join(t for _, _, t in r)} for n, r in PAGES.items()]}))
    return p


SETTINGS = {**en.DEFAULT_ENRICH, "model": "cheap/model", "stitch_model": "strong/model"}


def test_enrich_issue_builds_toc(project):
    d = project.title_dir("suff") / "1914-09-05"
    llm = FakeLLM()
    s = en.enrich_issue(project, d, llm, SETTINGS)
    assert s == {"articles": 4, "toc": 2, "ads": 1, "continued": 1, "filled": {}}
    assert not any(m for m in llm.prompts if m.startswith("Find the volume"))  # has vol./no.
    toc = json.loads((d / "toc.json").read_text())
    arts = {a["id"]: a for a in toc["articles"]}
    newport = arts["p1a2"]
    assert newport["pages"] == [1, 2] and newport["continued"] is True
    assert newport["regions"] == [{"page": 1, "ids": ["r1", "r2"]}, {"page": 2, "ids": ["r0"]}]
    assert "p2a1" not in arts                          # merged into p1a2
    assert arts["p1a3"]["regions"] == [{"page": 1, "ids": ["r3"]}]   # r2 went to p1a2
    assert arts["p2a2"]["author"] == "Mabel Vernon"
    assert [m for m, _ in llm.calls].count("strong/model") == 1   # stitch uses its own model
    assert toc["enrich"]["model"] == "cheap/model"
    assert [a["title"] for a in en.toc_entries(toc)] == ["Newport Conference", "Nevada"]
    text = en.article_text(d, newport)
    assert text.startswith("NEWPORT CONFERENCE") and text.endswith("closed with a vote.")


def test_page_results_are_cached(project):
    d = project.title_dir("suff") / "1914-09-05"
    en.enrich_issue(project, d, FakeLLM(), SETTINGS)
    llm = FakeLLM()
    en.enrich_issue(project, d, llm, SETTINGS)
    assert [m for m, _ in llm.calls] == ["strong/model"]          # only the stitch re-ran
    llm = FakeLLM()
    en.enrich_issue(project, d, llm, {**SETTINGS, "model": "other/model"})
    assert [m for m, _ in llm.calls].count("other/model") == 2     # new model: no cache hit
    (project.title_dir("suff") / "profile.json").write_text('{"sections": ["Nevada"]}')
    llm = FakeLLM()
    en.enrich_issue(project, d, llm, SETTINGS)
    assert [m for m, _ in llm.calls].count("cheap/model") == 2     # new profile: no cache hit


def test_bad_reply_retries_at_higher_temperature(project):
    d = project.title_dir("suff") / "1914-09-05"
    llm = FakeLLM(bad_first=True)
    en.enrich_issue(project, d, llm, {**SETTINGS})
    temps = [t for m, t in llm.calls if m == "cheap/model"]
    assert 0.3 in temps


def test_needs_ocr_first(project):
    d = project.title_dir("suff") / "1914-09-05"
    (d / "page_02.json").unlink()
    with pytest.raises(en.EnrichError, match="isn't OCR'd"):
        en.enrich_issue(project, d, FakeLLM(), SETTINGS)


def test_missing_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(en.EnrichError, match="OPENROUTER_API_KEY"):
        en.make_llm(en.DEFAULT_ENRICH)


def test_profile_block_reads_both_formats():
    new = {"publisher": "Congressional Union", "contributors": [{"name": "Alice Paul",
           "aka": ["Alice Panl"], "era": "1913-1921"}], "sections": ["Notes of the Week"],
           "ocr_fixes": {"Wilsou": "Wilson"}}
    block = en.profile_block(new, "1914-09-05")
    assert "Alice Paul (OCR'd as Alice Panl) [1913-1921]" in block
    assert "Notes of the Week" in block and "Wilsou -> Wilson" in block
    nw = {"standing_sections": [{"name": "Editorial", "era": "1921"}],
          "contributor_roster": [{"canonical": "Marcus Garvey", "role": "editor"}],
          "ocr_normalization": {"name_variants": {"Bruce Grit": "John E. Bruce"}}}
    block = en.profile_block(nw, None)
    assert "Marcus Garvey, editor" in block and "Editorial [1921]" in block
    assert "Bruce Grit -> John E. Bruce" in block
    assert en.profile_block(None, None) == ""


def test_export_writes_articles(project, tmp_path):
    d = project.title_dir("suff") / "1914-09-05"
    en.enrich_issue(project, d, FakeLLM(), SETTINGS)
    s = export(project, ["suff"], tmp_path / "out")
    assert s["articles"] == 3                                      # masthead left out
    rows = [json.loads(l) for l in (tmp_path / "out" / "articles.jsonl").read_text().splitlines()]
    newport = next(r for r in rows if r["headline"] == "Newport Conference")
    assert newport["citation"] == \
        '"Newport Conference," The Suffragist, vol. 2, no. 36, September 5, 1914, pp. 1–2'
    assert newport["pages"] == [1, 2] and newport["words"] == 18
    ad = next(r for r in rows if r["is_advertisement"])
    assert ad["citation"].startswith('"Smith\'s Hats," The Suffragist')


def test_export_without_toc_has_no_article_files(project, tmp_path):
    export(project, ["suff"], tmp_path / "out")
    assert not (tmp_path / "out" / "articles.jsonl").exists()


@pytest.mark.parametrize("answer,expected", [
    ({"volume": "68", "number": "111", "evidence": "VOLUME LXVIII, NO. 111"},
     {"volume": "68", "number": "111"}),                       # Roman volume, from the source
    ({"volume": "2", "number": "36", "evidence": "Vol. II  No. 36"},
     {"volume": "2", "number": "36"}),                         # whitespace differences are fine
    ({"volume": "68", "number": "112", "evidence": "VOLUME LXVIII, NO. 111"},
     {"volume": "68"}),                                        # 112 isn't in the evidence
    ({"volume": "68", "number": "111", "evidence": "VOLUME 68, NO. 111"}, {}),  # not in text
    ({"volume": "LXVIII", "number": None, "evidence": "VOLUME LXVIII"}, {}),   # not digits
])
def test_volume_number_must_match_the_evidence(answer, expected):
    text = "THE DAILY TAR HEEL\nVOLUME LXVIII, NO. 111 CHAPEL HILL\nVol. II No. 36"
    found = en.check_volume_number(answer, text)
    assert {k: v for k, v in found.items() if k != "evidence"} == expected


def test_enrich_fills_missing_volume_and_number(project):
    d = project.title_dir("suff") / "1914-09-05"
    rec = json.loads((d / "issue.json").read_text())
    rec.update(volume=None, number=None)
    rec["source"]["ocr"] = "source/pdf_text.txt"
    (d / "source").mkdir()
    (d / "source" / "pdf_text.txt").write_text("THE SUFFRAGIST\nVOL. II, No. 36 WASHINGTON\n")
    write_issue(d, rec)
    llm = FakeLLM(masthead={"volume": "2", "number": "36", "evidence": "VOL. II, No. 36"})
    s = en.enrich_issue(project, d, llm, SETTINGS)
    assert s["filled"] == {"volume": "2", "number": "36"}
    prompt = next(m for m in llm.prompts if m.startswith("Find the volume"))
    assert "THE SUFFRAGIST" in prompt and "VOL. II, No. 36" in prompt   # page top and source
    rec = json.loads((d / "issue.json").read_text())
    assert (rec["volume"], rec["number"]) == ("2", "36")
    assert rec["enrich_filled"]["fields"] == ["number", "volume"]
    assert rec["enrich_filled"]["evidence"] == "VOL. II, No. 36"

    # a hand correction is never replaced, even with --force
    rec["volume"] = "3"
    rec["enrich_filled"]["fields"] = ["number"]
    write_issue(d, rec)
    llm = FakeLLM(masthead={"volume": "2", "number": "36", "evidence": "VOL. II, No. 36"})
    s = en.enrich_issue(project, d, llm, SETTINGS, force=True)
    assert s["filled"] == {"number": "36"}
    assert json.loads((d / "issue.json").read_text())["volume"] == "3"
