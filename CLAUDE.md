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
- `ocr.py` is the OCR stage. `NewspaperOCR` wraps `newspaper_ocr.Pipeline` (pinned to 0.10.0;
  default DocLayout-YOLO + Tesseract `news_combo_fast`, overridable through the `[ocr]` table
  in paper.toml). It writes `page_NN.json` per page, which is newspaper-ocr's JSON
  **unmodified** plus `page`/`image`/`ocr` keys. `full_text.json` is written last and marks the
  issue done. Text clean-up (hyphen rejoin, line joining, dropping timeout/error placeholders)
  happens only when building `full_text.json`. Tests use a fake engine (anything with
  `describe()` and `page(image)`).
- `export.py` turns `full_text.json` + `issue.json` into `export/` (pages.jsonl/csv, issues.csv,
  dublin_core.csv, articles, optional txt/, README.txt). `source_file` is the PDF's name, never
  its path (paths reveal the user's folders), built in `export.partial` and swapped in. Page ids are
  `<slug>_<issue-folder-name>_pNN`. IA page links are `<details-url>/page/n<source_leaf>`.
- `search.py` is the SQLite FTS5 index at `.paperpress/search.db` (porter stemming). It's a
  cache keyed by a signature of every `full_text.json` (size+mtime), so `ensure_index()`
  rebuilds when OCR changes. `fts_query()` quotes every user term, so FTS syntax can't
  break a query.
- `site.py` renders every page (HTML, CSS and JS as inline strings) for both sites. A
  `Catalog` holds the differences: `base` path, `static` (Pagefind vs SQLite search), and
  the image and thumbnail URL functions. Reader URLs are directory-style (`t/<slug>/<issue>/p/<n>/`).
  Pagefind attributes (`data-pagefind-body/meta/filter/sort`) sit on the reader's text
  pane. `?q=` highlighting happens server-side in serve and in app.js on the static site,
  both as the same prefix approximation of stemming.
- `serve.py` is the standard-library `ThreadingHTTPServer` on 127.0.0.1: routing,
  `/img/` and `/thumb/` (thumbnails cached in `.paperpress/thumbs/`). URLs are resolved
  through the `Catalog`, never joined onto disk paths. The server keeps its code in memory,
  so restart `serve` after editing it.
- `build.py` is the static site: it copies resized scans (`--images copy`, 1,800 px, q72
  progressive JPEG) or links IA's IIIF
  server (`--images ia`, using each page's recorded `iiif_size`, probing and saving it via
  `ia.working_size` for older issues and falling back to copying), writes `.nojekyll` (GitHub
  Pages drops `_undated/` otherwise), then runs `python -m pagefind` from the `pagefind[bin]`
  dependency. It's built in `site.partial` and swapped in.
- `iiif.py` writes IIIF Presentation 3 into the static site when `build` gets `--url`
  (IIIF ids must be absolute): a manifest per issue, external AnnotationPages of OCR regions
  (`supplementing`, `#xywh=` targets), toc.json as Ranges, and per-title and top-level
  Collections. Canvas = our page image's pixel space, **except** for IA pages with
  `iiif_width`: there the canvas is IA's full scan size, and boxes are scaled up, because
  Mirador draws a service-backed image at the service's size whatever the canvas says. (A
  mismatch makes boxes land in the wrong place.) `refresh` fills `iiif_width/height` for
  old issues. Mirador 3 ignores `supplementing` annotations; Mirador 4 shows them. Checked
  with presentation-validator.iiif.io and Mirador on the demo.
- `bag.py` (`paperpress bag`) writes a BagIt 1.0 bag (sha256 and sha512 manifests and
  tagmanifests, Payload-Oxum) of paper.toml, issue folders, profiles and a fresh export.
  `check_bag` verifies; bagit-python validates our bags.
- **Outputs never replace foreign folders.** export/, site/ and bags are built in
  `<dest>.partial` and swapped in, deleting the old dest. `project.check_output` refuses
  the project itself, its parents, titles/, and any non-empty folder without the
  `.paperpress-output` marker (or the layout of a pre-marker output). Use
  `replace_output()` for any new output stage.
- `enrich.py` is the optional LLM table of contents, ported from Negro World's
  `analyze_issue.py --per-page`: one call per page groups region ids (`r0`...) into
  articles, then a stitch call links cross-page continuations, kept conservative (merges
  must cross pages). `clean_page_result` drops unknown or duplicate region ids. It writes
  `toc.json` with `articles[].regions = [{page, ids}]`, and `article_text()` rebuilds an
  article's text from the page JSON. Per-page results are cached in
  `.paperpress/enrich/<slug>/<issue>/`, keyed by model and a hash of the profile. The LLM is an injectable callable
  `(prompt, model, temperature) -> str`; the tests use a fake. `profile_block` also reads
  Negro World's profile keys.
- `profile.py` (`paperpress profile <title>`) drafts `titles/<slug>/profile.json`: one LLM call
  per sampled issue (spread across the run) over a digest of every headline plus the head and
  tail of each text block (bylines and signatures sit at the ends), then one merge call that
  keeps what recurs and adds `era`. `clean_profile` keeps only the keys `profile_block` reads.
  It defaults to the stitch model.
- Region labels become CSS classes in the reader with a `lab-` prefix. DocLayout has a
  label called `text`, which once collided with the page's own `.text` pane.
- `folios.py` works out printed page numbers: printed = position + one offset per issue,
  voted on by folios in our OCR (short regions in the top 9% or bottom 7% of the page,
  skipping the issue's year and day) plus IA's `source_page_number` when `pageProb` ≥ 80. It
  needs ≥ 2 votes and twice the runner-up, otherwise there's no numbering. It's computed on
  demand (lru_cache keyed on page-JSON mtimes), never stored; `issue.json` `printed_offset`
  overrides it. Citations and contents lists use printed numbers.
- IA page records carry `source_index` (position at IA, used for `/page/n<index>` links) and
  `source_leaf` (scan leaf, from the canvas label, which `page_numbers.json` keys on). Issues
  fetched by 0.1 stored the position as `source_leaf`; `paperpress refresh` repairs them.
- `cli.py` is the Click entry point (`paperpress init | title add | add ia | add pdf | refresh | ocr | profile | enrich | export | serve | build | bag | status`).
  `title add` appends a `[[titles]]` block as text (`project.add_title`), so comments in
  paper.toml survive. `--ia` turns a collection or any one issue's URL into
  `collection:<pub_…>` (`ia.query_for`). `ia.count` uses advancedsearch's `numFound`,
  because the scrape API's `total` is wrong.
- Source OCR (IA djvu text, PDF text layers) is a comparison baseline only. The archive's text
  comes from the `ocr` stage (newspaper-ocr, with reading order). LLM enrichment is
  optional, so nothing downstream may require `toc.json`.

## IA image-server quirks (learned the hard way)

- `full/max` returns HTTP 500 on large scans. Request an explicit width.
- Some exact page/width combinations 500 **deterministically**, while ±1 px works. So a 500 means
  "try a neighbouring width" (`_fetch_image`), not "retry the same URL". Backoff retries
  apply only to 429/502/503/504.
- Requests above the native width return 400.
- Default to 300 ppi via the item's `ppi` metadata (some periodicals are 800 ppi), and use 2
  parallel downloads.
