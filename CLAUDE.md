# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

paperpress turns runs of dated newspaper or magazine issues into a readable, searchable,
citable archive and research corpus. The intended user is a grad student on a laptop. It is the
generalized version of the Negro World pipeline (`../negro-world`, see its `docs/PIPELINE.md`).
OCR is **not** done here; it's delegated to `newspaper-ocr` (`../newspaper-ocr`). Keep page-level
logic (layout, recognition, region repair) there and issue/collection logic here.

## Commands

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest                                   # all tests (no network; IA calls are monkeypatched)
.venv/bin/pytest tests/test_ia.py -k undated       # one test
cd examples/suffrage && ../../.venv/bin/paperpress status   # live sample project
```

## Architecture

- `project.py` defines the **project format**, which is the contract between all stages:
  `paper.toml` plus `titles/<slug>/<YYYY-MM-DD>/{issue.json, images/page_NN.jpg, source/}`.
  `issue.json` is written last and atomically, and it marks the folder complete. Downloads
  build in `<dir>.partial` and are renamed into place. Same-date collisions go to
  `<date>__<source-id>`; undated issues go to `_undated/<source-id>`. Later stages should
  write their outputs into the same issue folder (`page_NN.json`, `full_text.json`,
  `toc.json`, matching the newspaper-ocr / Negro World layout).
- `dates.py` parses issue dates from messy source strings. Day or month precision; a bare
  year is not a date.
- `sources/ia.py` is the Internet Archive adapter (one IA item = one issue). It takes pages
  from IA's IIIF v3 manifest and images from IA's IIIF image service (Cantaloupe). It keeps
  IA's `_djvu.txt` OCR as a baseline.
- `sources/pdf.py` handles bring-your-own PDFs (one PDF = one issue). `plan()` dates every
  file before any are added; the date format is inferred from **all** file names, including
  0-byte ones, because the names are still evidence. Page images are copied out as the
  original JPEGs when possible, otherwise rendered at ≤300 ppi (never upsampled). Issues are
  identified by sha256. It uses pypdfium2, not PyMuPDF (AGPL).
- `cli.py` is the Click entry point (`paperpress init | add ia | add pdf | status`).
- Source OCR (IA djvu text, PDF text layers) is a comparison baseline only. The archive's text
  will come from the `ocr` stage (newspaper-ocr, with reading order). LLM TOC enrichment
  (`profile`/`enrich`, ported from Negro World) is planned and optional, so nothing
  downstream may require `toc.json`.

## IA image-server quirks (learned the hard way)

- `full/max` returns HTTP 500 on large scans. Request an explicit width.
- Some exact page/width combinations 500 **deterministically**, while ±1 px works. So a 500 means
  "try a neighbouring width" (`_fetch_image`), not "retry the same URL". Backoff retries
  apply only to 429/502/503/504.
- Requests above the native width return 400.
- Default to 300 ppi via the item's `ppi` metadata (some periodicals are 800 ppi), and use 2
  parallel downloads.
