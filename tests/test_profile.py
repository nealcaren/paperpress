"""`paperpress profile`, with a fake LLM."""
import json

import pytest

from paperpress import enrich as en
from paperpress.profile import (ProfileError, draft_profile, issue_digest, sample_issues,
                                write_profile)
from paperpress.project import Project, init_project, write_issue


def make_issue(p, date, regions):
    d = p.title_dir("wj") / date
    d.mkdir(parents=True)
    (d / "page_01.json").write_text(json.dumps({"page": 1, "regions": [
        {"id": f"r{i}", "label": lab, "text": t, "status": "ok"}
        for i, (lab, t) in enumerate(regions)]}))
    (d / "full_text.json").write_text("{}")
    write_issue(d, {"title": "wj", "date": date, "pages": [{"page": 1}]})
    return d


@pytest.fixture
def project(tmp_path):
    cfg = init_project(tmp_path)
    cfg.write_text(cfg.read_text() + '[[titles]]\nslug = "wj"\nname = "The Woman\'s Journal"\n')
    p = Project.load(tmp_path)
    long = "The meeting opened at noon. " * 20 + "ALICE STONE BLACKWELL."
    make_issue(p, "1912-02-03", [("title", "EDITORIAL NOTES"), ("plain_text", long),
                                 ("abandon", "42")])
    make_issue(p, "1912-02-10", [("title", "EDITORIAL NOTES"), ("plain_text", "Short.")])
    return p


def test_sample_spreads_across_the_run(tmp_path):
    dirs = [tmp_path / f"1912-01-{n:02d}" for n in range(1, 11)]
    assert [d.name[-2:] for d in sample_issues(dirs, 3)] == ["01", "05", "10"]
    assert sample_issues(dirs[:2], 6) == dirs[:2]


def test_digest_keeps_headlines_and_signatures(project):
    digest = issue_digest(project.title_dir("wj") / "1912-02-03")
    assert "# EDITORIAL NOTES" in digest
    assert "ALICE STONE BLACKWELL." in digest and " … " in digest      # head … tail
    assert "42" not in digest                                           # page furniture


class FakeLLM:
    def __init__(self):
        self.prompts = []

    def __call__(self, prompt, model, temperature=0):
        self.prompts.append(prompt)
        if prompt.startswith("Below are notes"):
            return json.dumps({
                "sections": [{"name": "Editorial Notes", "type": "editorial", "era": "1912"}],
                "contributors": [{"name": "Alice Stone Blackwell", "aka": ["A. S. B.",
                                  "Alice Stone Blackwell"], "era": 1912}],
                "ocr_fixes": {"Blackwcll": "Blackwell", "same": "same"},
                "organizations": "not a list"})
        return json.dumps({"sections": [{"name": "Editorial Notes"}, "junk"]})


def test_draft_and_write(project):
    llm = FakeLLM()
    prof = draft_profile(project, "wj", llm, "m", sample=6)
    assert len(llm.prompts) == 3                                        # 2 issues + merge
    assert '"issue": "1912-02-03"' in llm.prompts[-1]
    assert prof["sections"] == [{"name": "Editorial Notes", "type": "editorial", "era": "1912"}]
    assert prof["contributors"] == [{"name": "Alice Stone Blackwell", "aka": ["A. S. B."]}]
    assert prof["ocr_fixes"] == {"Blackwcll": "Blackwell"}
    assert prof["organizations"] == []
    assert prof["drafted"]["issues"] == ["1912-02-03", "1912-02-10"]
    path = write_profile(project, "wj", prof)
    assert en.load_profile(project, "wj")["title"] == "The Woman's Journal"
    assert "Alice Stone Blackwell (OCR'd as A. S. B.)" in en.profile_block(prof, None)
    assert path.name == "profile.json"


def test_needs_ocr(tmp_path):
    cfg = init_project(tmp_path)
    cfg.write_text(cfg.read_text() + '[[titles]]\nslug = "wj"\nname = "WJ"\n')
    with pytest.raises(ProfileError, match="OCR"):
        draft_profile(Project.load(tmp_path), "wj", FakeLLM(), "m")
