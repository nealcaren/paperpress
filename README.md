# paperpress

Turn a run of dated newspaper or magazine issues into an archive you can read,
search and cite, plus a text corpus for research. It's designed to run on a laptop.

paperpress handles everything above the page. OCR itself is done by
[newspaper-ocr](https://github.com/nealcaren/newspaper-ocr). paperpress organizes
issues by title and date, keeps provenance, and runs the issue-level stages. It
generalizes the pipeline built for [The Negro World Archive](https://negroworldarchive.org).

> **Status: early.** Working so far: the project format, bringing in issues from the
> Internet Archive or your own PDFs, OCR, an optional LLM table of contents, export,
> a local reading/search site, and `status`.

## Pipeline

| Stage | Command | Status |
|---|---|---|
| Bring in issues | `paperpress add ia` / `paperpress add pdf` | working |
| OCR, with newspaper reading order | `paperpress ocr` (via newspaper-ocr) | working |
| Remove duplicate page scans | `paperpress dedup` | planned |
| Table of contents (LLM, **optional**) | `paperpress enrich` | working (`profile` drafting planned) |
| Browse and search on your own machine | `paperpress serve` | working |
| Text corpus for research | `paperpress export` | working |
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

## Table of contents (optional)

```bash
export OPENROUTER_API_KEY=...       # https://openrouter.ai/keys
paperpress enrich [TITLES...] [--from DATE] [--to DATE] [--limit N] [--force]
```

An LLM reads each OCR'd page and groups its regions into articles: headline, author
(only from a byline or signature, never guessed), type (news, editorial, letter,
poem, advertisement, ...), section, and language. A second, small pass links stories
continued on another page ("Continued on page 6"). The result is `toc.json` in each
issue folder. Nothing else needs it. When it's there, the site shows a contents list
for each issue that links straight to each article, and `export` adds
`articles.jsonl`/`articles.csv` with each article's text and a citation like
`"GAGGED AND BOUND," The Woman's Journal, vol. 43, no. 6, February 10, 1912, pp. 1, 6`.

The defaults are `openai/gpt-5.6-luna` for the pages and `google/gemini-3.8-flash` for
linking, chosen by a bake-off on *The Negro World*. They cost about 1–3 cents per issue;
each run prints the actual cost. Any OpenAI-compatible endpoint works:

```toml
[enrich]
model = "openai/gpt-5.6-luna"
stitch_model = "google/gemini-3.8-flash"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
```

An optional `titles/<slug>/profile.json` tells the model about a paper: its regular
sections and columns, contributors (with common OCR misspellings), organizations and
languages. The keys are `sections`, `columns`, `contributors`
(`{"name", "aka", "role", "era"}`), `organizations`, `ad_categories`, `languages`,
`ocr_fixes` and `notes`. Each page's result is cached in `.paperpress/enrich/`, so
re-running after an interruption doesn't pay twice.

## Export

```bash
paperpress export [TITLES...] [--out DIR] [--txt]
```

This writes the corpus to `export/`:
- `pages.jsonl` and `pages.csv`: one row per page, with title, date, volume and
  number, page, word count, a ready-made `citation` ("The Woman's Journal, vol. 43,
  no. 5, February 3, 1912, p. 2"), a `source_url` that opens the page on the Internet
  Archive (or `source_file` for your own PDFs), the page image, the OCR engine, and
  the text in reading order.
- `issues.csv`: one row per issue, without the text.
- `txt/` (with `--txt`): one plain-text file per page.

CSVs are UTF-8 with a byte-order mark so Excel opens them correctly. Issues that
haven't been OCR'd yet are skipped and counted. Re-running replaces the export.

## Browse and search

```bash
paperpress serve            # opens http://127.0.0.1:8000/ in your browser
```

This runs a small website on your own computer. It listens on 127.0.0.1 only, so
nothing is shared. It has titles, issues by year with cover thumbnails, and a reader
that shows each scan with its OCR regions boxed beside the text in reading order.
Clicking a box finds its paragraph, and clicking a paragraph finds its box. There's
a "Copy citation" button, and the arrow keys turn pages.

Search covers every OCR'd page and supports `"exact phrases"` and `suffrag*` prefixes.
English stemming is on, so `voting` also finds "vote". You can filter by title and
date and sort by relevance or date. A result opens the page with the hits highlighted
and scrolled into view. The search index (`.paperpress/search.db`) updates itself
when you OCR more issues.

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
