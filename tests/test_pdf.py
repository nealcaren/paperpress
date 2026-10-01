"""Bring-your-own PDFs, using small synthetic PDFs built with Pillow."""
import pytest
from PIL import Image, ImageDraw

from paperpress.project import Project, init_project, read_issue
from paperpress.sources import pdf


def make_pdf(path, pages=2, size=(850, 1100), dpi=100, mode="L"):
    """A PDF of `pages` JPEG pages, size in pixels at `dpi` (Pillow embeds DCT)."""
    imgs = []
    for i in range(pages):
        im = Image.new(mode, size, "white")
        ImageDraw.Draw(im).text((50, 50), f"page {i + 1}", fill="black")
        imgs.append(im)
    path.parent.mkdir(parents=True, exist_ok=True)
    imgs[0].save(path, "PDF", resolution=dpi, save_all=True, append_images=imgs[1:])
    return path


@pytest.fixture
def project(tmp_path):
    cfg = init_project(tmp_path / "proj")
    cfg.write_text(cfg.read_text() + '[[titles]]\nslug = "dth"\nname = "The Daily Tar Heel"\n')
    return Project.load(tmp_path / "proj")


def test_plan_infers_format_and_flags_empty_files(tmp_path):
    src = tmp_path / "scans"
    make_pdf(src / "1960" / "dth_sn92073228_03041960.pdf")
    make_pdf(src / "1960" / "dth_sn92073228_09251960.pdf")
    (src / "1961").mkdir()
    (src / "1961" / "dth_sn92073228_05161961.pdf").write_bytes(b"")
    (src / ".hidden").mkdir()
    make_pdf(src / ".hidden" / "x_01011950.pdf")
    fmt, planned = pdf.plan([src])
    assert fmt == "MMDDYYYY"
    got = {p.path.name: (p.date, p.problem) for p in planned}
    assert got == {
        "dth_sn92073228_03041960.pdf": ("1960-03-04", None),
        "dth_sn92073228_09251960.pdf": ("1960-09-25", None),
        "dth_sn92073228_05161961.pdf": (None, "empty file (0 bytes)"),
    }


def test_low_res_jpeg_pages_are_copied_out_untouched(project, tmp_path):
    f = make_pdf(tmp_path / "dth_03041960.pdf", dpi=150)
    status, dest = pdf.add_pdf(project, "dth", f, "1960-03-04")
    assert status == "added" and dest.name == "1960-03-04"
    rec = read_issue(dest)
    assert [p["image"] for p in rec["pages"]] == ["images/page_01.jpg", "images/page_02.jpg"]
    p1 = rec["pages"][0]
    assert (p1["width"], p1["height"], p1["ppi"], p1["image_from"]) == (850, 1100, 150,
                                                                        "extracted")
    assert (dest / "images" / "page_01.jpg").read_bytes()[:2] == b"\xff\xd8"
    assert rec["source"]["type"] == "pdf" and rec["source"]["filename"] == f.name
    assert rec["source"]["copy"] is None


def test_high_res_pages_are_rendered_down(project, tmp_path):
    f = make_pdf(tmp_path / "x_03041960.pdf", pages=1, size=(1200, 1600), dpi=600)
    _, dest = pdf.add_pdf(project, "dth", f, "1960-03-04")
    p1 = read_issue(dest)["pages"][0]
    assert (p1["width"], p1["ppi"], p1["image_from"]) == (600, 300, "rendered")
    with Image.open(dest / "images" / "page_01.jpg") as im:
        assert im.mode == "L" and im.width == 600


def test_ppi_none_keeps_native(project, tmp_path):
    f = make_pdf(tmp_path / "x_03041960.pdf", pages=1, size=(1200, 1600), dpi=600)
    _, dest = pdf.add_pdf(project, "dth", f, "1960-03-04", ppi=None)
    assert read_issue(dest)["pages"][0]["image_from"] == "extracted"


def test_readding_same_file_is_skipped_even_if_renamed(project, tmp_path):
    f = make_pdf(tmp_path / "a_03041960.pdf")
    pdf.add_pdf(project, "dth", f, "1960-03-04")
    renamed = f.rename(tmp_path / "b_03041960.pdf")
    status, dest = pdf.add_pdf(project, "dth", renamed, "1960-03-04")
    assert status == "skipped" and dest.name == "1960-03-04"


def test_copy_and_undated(project, tmp_path):
    f = make_pdf(tmp_path / "mystery.pdf")
    status, dest = pdf.add_pdf(project, "dth", f, None, copy=True)
    assert status == "added-undated"
    assert dest.parts[-2:] == ("_undated", "mystery")
    assert (dest / "source" / "original.pdf").read_bytes() == f.read_bytes()


def test_unreadable_pdf(project, tmp_path):
    f = tmp_path / "bad_03041960.pdf"
    f.write_bytes(b"not a pdf at all")
    with pytest.raises(pdf.PDFError, match="not a readable PDF"):
        pdf.add_pdf(project, "dth", f, "1960-03-04")
    assert not project.title_dir("dth").exists() or not any(project.title_dir("dth").rglob("*"))


def test_empty_files_still_count_as_date_format_evidence(tmp_path):
    # the only real file is ambiguous (03/04); an empty sibling's 25 settles it
    make_pdf(tmp_path / "dth_03041960.pdf")
    (tmp_path / "dth_09251960.pdf").write_bytes(b"")
    fmt, planned = pdf.plan([tmp_path])
    assert fmt == "MMDDYYYY"
    assert [p.date for p in planned if not p.problem] == ["1960-03-04"]
