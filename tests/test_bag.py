"""BagIt packaging, and the guard that keeps outputs from replacing other folders."""
import hashlib

import pytest

from paperpress.bag import BagError, check_bag, make_bag
from paperpress.build import BuildError, build
from paperpress.export import export
from paperpress.project import OUTPUT_MARKER, Project, ProjectError, init_project
from test_serve import make_issue


@pytest.fixture
def project(tmp_path):
    cfg = init_project(tmp_path / "proj", "Suffrage Press")
    cfg.write_text(cfg.read_text() + '[[titles]]\nslug = "wj"\nname = "The Woman\'s Journal"\n')
    p = Project.load(tmp_path / "proj")
    make_issue(p, "wj", "1912-02-03", "1912-02-03", ["Votes for women.", "Marching."])
    make_issue(p, "wj", "_undated/mystery", None, ["Undated news."])
    (p.title_dir("wj") / "profile.json").write_text("{}")
    (p.title_dir("wj") / "1912-02-03" / "issue.json.tmp").write_text("half-written")
    (p.root / ".paperpress").mkdir()
    (p.root / ".paperpress" / "search.db").write_text("cache")
    return p


def test_bag(project, tmp_path):
    s = make_bag(project, tmp_path / "bag", info={"Source-Organization": "UNC",
                                                  "Contact-Name": None})
    bag = tmp_path / "bag"
    data = bag / "data"
    assert (data / "paper.toml").exists() and (data / "README.txt").exists()
    assert (data / "titles/wj/1912-02-03/images/page_01.jpg").exists()
    assert (data / "titles/wj/_undated/mystery/issue.json").exists()
    assert (data / "titles/wj/profile.json").exists()
    assert (data / "export/dublin_core.csv").exists()
    assert not (data / "export" / OUTPUT_MARKER).exists()
    assert not list(data.rglob("*.tmp")) and not (data / ".paperpress").exists()
    info = (bag / "bag-info.txt").read_text()
    assert "Source-Organization: UNC\n" in info and "Contact-Name" not in info
    assert f"Payload-Oxum: {s['bytes']}.{s['files']}\n" in info
    assert (bag / "bagit.txt").read_text().startswith("BagIt-Version: 1.0\n")
    line = next(l for l in (bag / "manifest-sha256.txt").read_text().splitlines()
                if l.endswith("data/paper.toml"))
    assert line.split()[0] == hashlib.sha256((data / "paper.toml").read_bytes()).hexdigest()
    assert check_bag(bag) == []

    (data / "titles/wj/1912-02-03/page_01.json").write_text("edited")
    (data / "extra.txt").write_text("x")
    (data / "titles/wj/1912-02-03/images/page_02.jpg").unlink()
    problems = check_bag(bag)
    assert "changed (sha256): data/titles/wj/1912-02-03/page_01.json" in problems
    assert "missing: data/titles/wj/1912-02-03/images/page_02.jpg" in problems
    assert "not in manifest-sha256.txt: data/extra.txt" in problems

    make_bag(project, bag)                      # a paperpress bag can be remade in place
    assert check_bag(bag) == []


def test_outputs_never_replace_other_folders(project, tmp_path):
    mine = tmp_path / "Documents"
    mine.mkdir()
    (mine / "thesis.docx").write_text("precious")
    with pytest.raises(ProjectError, match="wasn't made by paperpress"):
        export(project, ["wj"], mine)
    with pytest.raises(BuildError, match="wasn't made by paperpress"):
        build(project, mine, pagefind=False)
    with pytest.raises(BagError, match="wasn't made by paperpress"):
        make_bag(project, mine)
    assert (mine / "thesis.docx").read_text() == "precious"
    with pytest.raises(ProjectError, match="holds the project"):
        export(project, ["wj"], project.root)
    with pytest.raises(ProjectError, match="holds the project"):
        export(project, ["wj"], tmp_path)
    with pytest.raises(ProjectError, match="titles"):
        export(project, ["wj"], project.root / "titles" / "out")
    empty = tmp_path / "empty"
    empty.mkdir()
    export(project, ["wj"], empty)              # an empty folder is fine
    export(project, ["wj"], empty)              # and so is our own output, again


def test_outputs_from_before_the_marker_are_still_replaced(project, tmp_path):
    old = tmp_path / "export"
    export(project, ["wj"], old)
    (old / OUTPUT_MARKER).unlink()              # as written by paperpress 0.1
    export(project, ["wj"], old)
