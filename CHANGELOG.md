# Changelog

## 0.2.0 (2026-10-02)

First release on PyPI: `uv tool install paperpress`.

- `paperpress title add`: add a periodical without editing paper.toml; `--ia` finds an
  Internet Archive run from its collection or any one issue.
- `paperpress profile`: an LLM drafts a profile of a paper (sections, contributors and
  their OCR misspellings, organizations) that `enrich` uses.
- `enrich` reads a missing volume and number from the masthead, keeping them only when the
  quoted masthead text is on the page.
- Library formats: IIIF Presentation 3 manifests (`build --url`), Dublin Core records
  (`export/dublin_core.csv`), and BagIt packages for deposit (`paperpress bag`).
- Smaller website images (1,800 px, about 0.6 MB a page).
- Safety: export, build and bag never replace a folder paperpress didn't make; exports and
  bags record a PDF's file name, not its path on your computer.
- Clearer messages throughout, from a usability test with Daily Tar Heel PDFs.

## 0.1.0

Internet Archive and PDF sources, OCR with newspaper-ocr, export, local archive, static
website with search, optional LLM tables of contents, printed page numbers.
