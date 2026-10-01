from __future__ import annotations

from collections import Counter
from pathlib import Path

import click

from . import __version__
from .dates import parse_date
from .project import DATE_DIR, Project, ProjectError, init_project, read_issue


def _project() -> Project:
    try:
        return Project.load()
    except ProjectError as e:
        raise click.ClickException(str(e))


@click.group()
@click.version_option(__version__)
def main():
    """Turn dated newspaper and magazine issues into a searchable archive."""


@main.command()
@click.argument("directory", default=".", type=click.Path(file_okay=False, path_type=Path))
@click.option("--name", help="Project name (default: the folder name).")
def init(directory: Path, name: str | None):
    """Start a project: writes paper.toml and a titles/ folder."""
    try:
        cfg = init_project(directory, name)
    except ProjectError as e:
        raise click.ClickException(str(e))
    click.echo(f"created {cfg}\nnext: add a [[titles]] block to it for each periodical")


@main.group()
def add():
    """Bring issues into the project from a source."""


@add.command("ia")
@click.argument("title")
@click.argument("identifiers", nargs=-1)
@click.option("--query", "-q", help="Internet Archive search query "
              "(default: the title's ia_query in paper.toml).")
@click.option("--from", "date_from", help="Only issues on or after this date (YYYY-MM-DD).")
@click.option("--to", "date_to", help="Only issues on or before this date (YYYY-MM-DD).")
@click.option("--limit", type=int, help="Fetch at most this many issues.")
@click.option("--ppi", type=int, default=300, show_default=True,
              help="Downsample scans made at a higher resolution to this (0 keeps native size; "
              "300 is plenty for OCR).")
@click.option("--max-width", type=int, help="Also cap page images at this width in pixels.")
@click.option("--force", is_flag=True, help="Re-download issues already in the project.")
@click.option("--dry-run", is_flag=True, help="List matching items without downloading.")
def add_ia(title, identifiers, query, date_from, date_to, limit, ppi, max_width, force,
           dry_run):
    """Fetch issues of TITLE from the Internet Archive.

    Give IA item IDENTIFIERS directly, or a --query (one IA item per issue).
    Re-running is safe: issues already fetched are skipped.

    \b
      paperpress add ia revolution --query 'identifier:revolution-18*' --limit 5
      paperpress add ia suffragist per_the-suffragist_the-suffragist_1914-09-05_2_36
    """
    from .sources import ia

    project = _project()
    try:
        t = project.title(title)
    except ProjectError as e:
        raise click.ClickException(str(e))
    query = query or (None if identifiers else t.ia_query)
    if not identifiers and not query:
        raise click.UsageError("give identifiers, --query, or set ia_query for this title")

    if identifiers:
        items = [{"identifier": i} for i in identifiers]
    else:
        items = []
        for item in ia.search(query):
            found = parse_date(str(item.get("date") or item.get("title") or ""))
            if date_from or date_to:
                if not found:
                    continue
                if (date_from and found[0] < date_from) or (date_to and found[0] > date_to):
                    continue
            items.append({**item, "_sort": found[0] if found else "9999"})
        items.sort(key=lambda i: (i["_sort"], i["identifier"]))
        if limit:
            items = items[:limit]
    click.echo(f"{len(items)} item(s) for {t.name}")

    if dry_run:
        for i in items:
            click.echo(f"  {i['identifier']}  {i.get('date', '')}  {i.get('title', '')}")
        return

    counts, failed = Counter(), []
    for n, item in enumerate(items, start=1):
        ident = item["identifier"]
        try:
            status, dest = ia.fetch_issue(project, title, ident, ppi=ppi or None,
                                          max_width=max_width, force=force)
        except ia.IAError as e:
            failed.append(ident)
            click.echo(f"[{n}/{len(items)}] FAILED {ident}: {e}", err=True)
            continue
        counts[status] += 1
        click.echo(f"[{n}/{len(items)}] {status:15} {ident} -> {dest.relative_to(project.root)}")

    click.echo(", ".join(f"{v} {k}" for k, v in counts.items()) or "nothing fetched")
    if counts["fetched-undated"]:
        click.echo(f"note: undated issues are in titles/{title}/_undated/ until they get a date")
    if failed:
        raise click.ClickException(f"{len(failed)} item(s) failed; re-run to retry them")


@add.command("pdf")
@click.argument("title")
@click.argument("paths", nargs=-1, required=True,
                type=click.Path(exists=True, path_type=Path))
@click.option("--date-format", help="How dates are written in the file names, e.g. MMDDYYYY, "
              "YYYYMMDD, DD-MM-YYYY (default: worked out from the names, or the title's "
              "date_format in paper.toml).")
@click.option("--ppi", type=int, default=300, show_default=True,
              help="Downsample pages scanned at a higher resolution to this (0 keeps native).")
@click.option("--copy", is_flag=True, help="Also keep a copy of each PDF in the issue's source/.")
@click.option("--force", is_flag=True, help="Re-add PDFs that are already in the project.")
@click.option("--dry-run", is_flag=True, help="Show the date each file would get, add nothing.")
def add_pdf(title, paths, date_format, ppi, copy, force, dry_run):
    """Add your own PDFs (files or folders) as issues of TITLE.

    Each PDF is one issue, dated from its file name. The order of the date's
    digits is worked out from all the names together; if it's ambiguous (every
    day is 12 or under), you'll be asked for --date-format.

    \b
      paperpress add pdf dth ~/scans/dth --dry-run
      paperpress add pdf dth ~/scans/dth --date-format MMDDYYYY
    """
    from .dates import DateFormatError
    from .sources import pdf

    project = _project()
    try:
        t = project.title(title)
        fmt, planned = pdf.plan(list(paths), date_format or t.extra.get("date_format"))
    except (ProjectError, DateFormatError, pdf.PDFError) as e:
        raise click.ClickException(str(e))
    ok = [p for p in planned if not p.problem]
    bad = [p for p in planned if p.problem]
    click.echo(f"{len(planned)} PDF(s) for {t.name}; dates read as {fmt}")
    if bad:
        reasons = Counter(p.problem for p in bad)
        click.echo(f"  skipping {len(bad)}: " + "; ".join(f"{n} {r}" for r, n in reasons.items()))
    if dry_run:
        for p in planned:
            click.echo(f"  {p.date or '----------'}  {p.path.name}"
                       + (f"   ({p.problem})" if p.problem else ""))
        return

    counts, failed = Counter(), []
    for n, p in enumerate(ok, start=1):
        try:
            status, dest = pdf.add_pdf(project, title, p.path, p.date, ppi=ppi or None,
                                       copy=copy, force=force)
        except (pdf.PDFError, OSError) as e:
            failed.append(p.path)
            click.echo(f"[{n}/{len(ok)}] FAILED {p.path.name}: {e}", err=True)
            continue
        counts[status] += 1
        click.echo(f"[{n}/{len(ok)}] {status:13} {p.path.name} -> {dest.relative_to(project.root)}")
    click.echo(", ".join(f"{v} {k}" for k, v in counts.items()) or "nothing added")
    if failed:
        raise click.ClickException(f"{len(failed)} PDF(s) failed")


@main.command()
def status():
    """Summarize what's in the project, title by title."""
    project = _project()
    if not project.titles:
        click.echo("no titles in paper.toml yet")
        return
    for slug, t in project.titles.items():
        dirs = project.issue_dirs(slug, include_undated=True)
        dated = [d for d in dirs if DATE_DIR.match(d.name)]
        undated = [d for d in dirs if not DATE_DIR.match(d.name)]
        pages = sum(len(read_issue(d)["pages"]) for d in dirs)
        dates = Counter(DATE_DIR.match(d.name).group(1) for d in dated)
        span = f"{min(dates)} to {max(dates)}" if dates else "no dated issues"
        click.echo(f"{t.name} ({slug}): {len(dirs)} issues, {pages} pages, {span}")
        if undated:
            click.echo(f"  {len(undated)} undated: " + ", ".join(d.name for d in undated))
        dup = [d for d, c in dates.items() if c > 1]
        if dup:
            click.echo(f"  {len(dup)} date(s) with more than one issue: " + ", ".join(dup))


if __name__ == "__main__":
    main()
