"""The translate stage, with a fake LLM that 'translates' by upper-casing."""
import json
import re

import pytest

from paperpress import translate as tr
from paperpress.export import export
from paperpress.project import Project
from paperpress.site import Catalog, page_reader

from test_enrich import project  # noqa: F401  (the same two-page fixture)

SETTINGS = {**tr.DEFAULT_TRANSLATE, "model": "cheap/model", "target": "English"}


class FakeLLM:
    def __init__(self, drop_first=False):
        self.calls, self.drop_first = [], drop_first

    def __call__(self, prompt, model, temperature=0):
        self.calls.append((model, temperature))
        regions = re.findall(r"^\[(r\d+)\] \(\w+\) (.*)$", prompt, re.M)
        out = {rid: text.upper() for rid, text in regions}
        if self.drop_first and len(self.calls) == 1:
            out.pop(regions[0][0])                      # a reply missing a region
        return json.dumps(out)


def issue(project):  # noqa: F811
    return project.title_dir("suff") / "1914-09-05"


def test_translate_issue_writes_translation(project):  # noqa: F811
    d = issue(project)
    s = tr.translate_issue(project, d, FakeLLM(), SETTINGS)
    assert s == {"pages": 2, "regions": 7, "calls": 2}
    doc = json.loads((d / "translation.json").read_text())
    assert doc["target"] == "English" and doc["translate"]["model"] == "cheap/model"
    p1 = tr.page_translation(doc, 1)
    assert p1["regions"]["r1"] == "NEWPORT CONFERENCE"
    assert p1["text"].split("\n\n")[1] == "NEWPORT CONFERENCE"


def test_chunks_split_long_pages():
    regions = [(f"r{i}", "plain_text", "x" * 400) for i in range(10)]
    assert [len(c) for c in tr.chunks(regions, limit=1000)] == [2, 2, 2, 2, 2]
    assert tr.chunks([]) == []


def test_chunks_are_cached(project):  # noqa: F811
    d = issue(project)
    tr.translate_issue(project, d, FakeLLM(), SETTINGS)
    llm = FakeLLM()
    tr.translate_issue(project, d, llm, SETTINGS)
    assert llm.calls == []
    tr.translate_issue(project, d, llm, {**SETTINGS, "target": "French"})
    assert len(llm.calls) == 2                          # new target: no cache hit


def test_missing_region_retries(project):  # noqa: F811
    llm = FakeLLM(drop_first=True)
    tr.translate_issue(project, issue(project), llm, SETTINGS)
    assert [t for _, t in llm.calls].count(0.3) == 1


def test_reader_and_export_show_translation(project, tmp_path):  # noqa: F811
    d = issue(project)
    cat = Catalog(Project.load(project.root)).refresh()
    plain = page_reader(cat, cat.issue("suff", "1914-09-05"), 1)
    assert 'class="langs"' not in plain and " orig" not in plain
    tr.translate_issue(project, d, FakeLLM(), SETTINGS)
    cat = Catalog(Project.load(project.root)).refresh(max_age=0)
    html = page_reader(cat, cat.issue("suff", "1914-09-05"), 1, q="newport")
    assert 'class="langs"' in html
    assert 'class="r lab-title hit tr" data-r="r1" lang="en"><mark>NEWPORT</mark> CONFERENCE' \
        in html
    assert 'class="r lab-title hit orig" data-r="r1"' in html
    export(project, ["suff"], project.root / "export")
    rows = [json.loads(l) for l in (project.root / "export" / "pages.jsonl").open()]
    assert rows[1]["translation"].startswith("(CONTINUED FROM PAGE 1)")


def test_cli_needs_key(project, monkeypatch):  # noqa: F811
    from click.testing import CliRunner

    from paperpress.cli import main

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.chdir(project.root)
    res = CliRunner().invoke(main, ["translate"])
    assert res.exit_code != 0 and "OPENROUTER_API_KEY to use translate" in res.output
