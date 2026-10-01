# paperpress

Turn a run of dated newspaper or magazine issues into an archive you can read,
search and cite, plus a text corpus for research. It's designed to run on a laptop.

paperpress handles everything above the page. OCR itself is done by
[newspaper-ocr](https://github.com/nealcaren/newspaper-ocr). paperpress organizes
issues by title and date, keeps provenance, and runs the issue-level stages. It
generalizes the pipeline built for [The Negro World Archive](https://negroworldarchive.org).

> **Status: early.** Working so far: the project format, bringing in issues from the
> Internet Archive or your own PDFs, OCR, and `status`.

## Pipeline

| Stage | Command | Status |
|---|---|---|
| Bring in issues | `paperpress add ia` / `paperpress add pdf` | working |
| OCR, with newspaper reading order | `paperpress ocr` (via newspaper-ocr) | working |
| Remove duplicate page scans | `paperpress dedup` | planned |
| Table of contents (LLM, **optional**) | `paperpress profile`, `paperpress enrich` | planned |
| Browse and search on your own machine | `paperpress serve` | planned |
| Text corpus for research | `paperpress export` | planned |
| Public static site with search | `paperpress build` | planned |

The archive's text always comes from paperpress's own OCR. Any OCR that came with
the source (IA's text, a PDF's text layer) is kept in `source/` only as a baseline
to compare against. The LLM table of contents is an extra: search, reading and
export all work without it.

## Quick start

```bash
uv tool install git+https://github.com/nealcaren/paperpress   # not on PyPI yet

paperpress init suffrage-press
cd suffrage-press
# add a [[titles]] block to paper.toml (see examples/suffrage/paper.toml), then:
paperpress add ia revolution --query 'identifier:revolution-18*' --dry-run
paperpress add ia revolution --query 'identifier:revolution-18*' --from 1870-01-01 --limit 10
paperpress status
```

## A project

```
paper.toml                      project name + one [[titles]] block per periodical
titles/<slug>/<YYYY-MM-DD>/
    issue.json                  date, volume/number, source + rights, page list
    images/page_NN.jpg          page images, in order
    source/ia_ocr.txt           the source's own OCR, kept as a baseline
titles/<slug>/_undated/<id>/    issues whose date couldn't be determined yet
```

`issue.json` is written last, so a folder that has one is complete. Interrupted
downloads leave nothing behind, and re-running a command skips issues that are
already there. If two issues share a title and date (a second edition, a duplicate
upload), the second one goes in `<date>__<source-id>/` and nothing is overwritten.

## Internet Archive

```bash
paperpress add ia <title> [IDENTIFIERS...] [--query Q] [--from DATE] [--to DATE] [--limit N]
```

- One IA item is treated as one issue. The issue date comes from the item's `date`
  field, or failing that its title. Items with neither go in `_undated/`.
- Scans made above 300 ppi are downsampled to 300 ppi (`--ppi` to change it, `--ppi 0`
  to keep native size). Higher resolution doesn't improve OCR on microfilm, and some
  IA periodicals were scanned at 800 ppi.
- Each page records its IA IIIF image-service URL, so a published site can serve
  IA's images instead of hosting its own.

## OCR

```bash
paperpress ocr [TITLES...] [--from DATE] [--to DATE] [--limit N] [--force]
```

This runs [newspaper-ocr](https://github.com/nealcaren/newspaper-ocr) 0.10 on every
page. Layout detection finds the articles, headlines and columns and puts them in
newspaper reading order, and then each region is recognized. By default it uses
DocLayout-YOLO for layout and Tesseract with newspaper-ocr's fine-tuned
`news_combo_fast` model. That runs on any laptop with Tesseract installed
(`brew install tesseract`, `apt install tesseract-ocr`), at about 20 s per page on an
M-series Mac. You can change the engine in `paper.toml`:

```toml
[ocr]
detector = "doclayout_yolo"
recognizer = "tesseract"
recognizer_model = "news_combo_fast"
```

Each issue folder gets one `page_NN.json` per page, which is newspaper-ocr's output
unchanged: regions in reading order with boxes, labels, text and status. It also gets
a `full_text.json` with the issue's text page by page, with hyphenated line breaks
rejoined. Finished pages are skipped, so stopping and re-running picks up where it
left off.

## Your own PDFs

```bash
paperpress add pdf <title> <files or folders...> [--date-format MMDDYYYY] [--dry-run]
```

- One PDF is one issue, and the date comes from the file name. The date's digit order
  (`19600304`, `03041960`, `04031960`) is worked out from all the file names together.
  If every day in the batch is 12 or under, MM/DD and DD/MM can't be told apart, and
  you'll be asked for `--date-format`. You can also set `date_format` on the title in
  `paper.toml`.
- If a page is a single JPEG scan at or below 300 ppi, the original image is copied
  out untouched. Otherwise the page is rendered at 300 ppi, and never above the scan's
  own resolution.
- Each PDF is identified by its checksum, so adding the same file again (even renamed
  or moved) is skipped. Empty (0-byte) files are reported and skipped. Use `--copy` to
  keep the original PDFs inside the project.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest
```

Two sample projects:

- `examples/suffrage/` uses the Internet Archive: *The Revolution* (1868–72),
  *The Woman's Journal* (1870–1917) and *The Suffragist* (1913–21).
- `examples/dth/` uses your own PDFs: *The Daily Tar Heel*, from files named
  `dth_<LCCN>_<MMDDYYYY>.pdf`.
