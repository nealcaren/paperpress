# paperpress

Turn a run of dated newspaper or magazine issues into an archive you can read,
search and cite, plus a text corpus for research. It's designed to run on a laptop.

paperpress handles everything above the page. OCR itself is done by
[newspaper-ocr](https://github.com/nealcaren/newspaper-ocr). paperpress organizes
issues by title and date, keeps provenance, and runs the issue-level stages. It
generalizes the pipeline built for [The Negro World Archive](https://negroworldarchive.org).

> **Status: early.** Working so far: the project format, fetching from the Internet
> Archive, and `status`. Next: OCR (via newspaper-ocr), duplicate-page removal,
> table-of-contents enrichment, a static site with search, and corpus export.

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

## Development

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest
```

`examples/suffrage/` is a sample project with three U.S. suffrage papers on the
Internet Archive: *The Revolution* (1868–72), *The Woman's Journal* (1870–1917) and
*The Suffragist* (1913–21).
