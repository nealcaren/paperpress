import pytest

from paperpress.project import Project, ProjectError, init_project, write_issue

TITLES = '''
[[titles]]
slug = "revolution"
name = "The Revolution"
ia_query = "identifier:revolution-18*"
'''


@pytest.fixture
def project(tmp_path):
    cfg = init_project(tmp_path, "Suffrage Press")
    cfg.write_text(cfg.read_text() + TITLES)
    return Project.load(tmp_path)


def test_init_and_load(project, tmp_path):
    assert project.name == "Suffrage Press"
    assert project.title("revolution").ia_query == "identifier:revolution-18*"
    assert project.title("revolution").kind == "newspaper"
    with pytest.raises(ProjectError):
        init_project(tmp_path)


def test_load_from_subdirectory(project, tmp_path):
    sub = tmp_path / "titles" / "deep"
    sub.mkdir(parents=True)
    assert Project.load(sub).root == tmp_path


def test_unknown_title(project):
    with pytest.raises(ProjectError, match="unknown title"):
        project.title("suffragist")


def test_bad_slug(tmp_path):
    (tmp_path / "paper.toml").write_text('[[titles]]\nslug = "Bad Slug"\nname = "x"\n')
    with pytest.raises(ProjectError, match="slug"):
        Project.load(tmp_path)


def _make_issue(project, path, source_id):
    path.mkdir(parents=True)
    write_issue(path, {"source": {"id": source_id}, "pages": []})


def test_issue_placement(project):
    first = project.new_issue_dir("revolution", "1870-04-07", "a")
    assert first.name == "1870-04-07"
    _make_issue(project, first, "a")
    # same date, different source -> side-by-side folder, never an overwrite
    second = project.new_issue_dir("revolution", "1870-04-07", "b/c")
    assert second.name == "1870-04-07__b_c"
    assert project.new_issue_dir("revolution", None, "x").parts[-2:] == ("_undated", "x")


def test_issue_dirs_and_find(project):
    base = project.title_dir("revolution")
    _make_issue(project, base / "1870-04-14", "b")
    _make_issue(project, base / "1870-04-07", "a")
    _make_issue(project, base / "_undated" / "u", "u")
    (base / "1870-04-21.partial").mkdir()        # interrupted fetch: ignored
    assert [d.name for d in project.issue_dirs("revolution")] == ["1870-04-07", "1870-04-14"]
    assert len(project.issue_dirs("revolution", include_undated=True)) == 3
    assert project.find_issue("revolution", "u").name == "u"
    assert project.find_issue("revolution", "zzz") is None
