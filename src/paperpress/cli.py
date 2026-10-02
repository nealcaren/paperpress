from __future__ import annotations

from collections import Counter
from pathlib import Path

import click

from . import __version__
from .dates import parse_date
from .project import (DATE_DIR, Project, ProjectError, add_title, init_project, plural,
                      read_issue)


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
    click.echo(f"created {cfg}\nnext: add each periodical with `paperpress title add`")


@main.group()
def title():
    """Add periodicals (titles) to the project."""


@title.command("add")
@click.argument("slug")
@click.argument("name")
@click.option("--ia", "ia_ref", help="The periodical on the Internet Archive: its collection, "
              "or any one issue (an archive.org URL or identifier).")
@click.option("--ia-query", help="An Internet Archive search that finds every issue, "
              "e.g. 'identifier:revolution-18*' (instead of --ia).")
@click.option("--kind", type=click.Choice(["newspaper", "magazine"]), default="newspaper",
              show_default=True)
@click.option("--date-format", help="How dates are written in your PDFs' file names, "
              "if they're ambiguous (e.g. MMDDYYYY).")
def title_add(slug, name, ia_ref, ia_query, kind, date_format):
    """Add a periodical to paper.toml.

    SLUG is a short name for folders and commands (lowercase, hyphens), NAME is
    the title as it should be cited.

    \b
      paperpress title add suffragist "The Suffragist" \\
          --ia https://archive.org/details/pub_the-suffragist
      paperpress title add dth "The Daily Tar Heel"         # for your own PDFs
    """
    project = _project()
    if ia_ref and ia_query:
        raise click.UsageError("give --ia or --ia-query, not both")
    how = None
    if ia_ref:
        from .sources import ia
        try:
            ia_query, how = ia.query_for(ia_ref)
        except ia.IAError as e:
            raise click.ClickException(str(e))
    try:
        add_title(project, slug, name, kind=kind, ia_query=ia_query, date_format=date_format)
    except ProjectError as e:
        raise click.ClickException(str(e))
    click.echo(f"added {name} ({slug}) to paper.toml")
    if ia_query:
        from .sources import ia
        try:
            found = f": {ia.count(ia_query):,} item(s) on the Internet Archive"
        except ia.IAError:
            found = ""
        click.echo(f"  ia_query = {ia_query!r}" + (f" ({how})" if how else "") + found)
        click.echo(f"next: paperpress add ia {slug} --limit 2 --dry-run")
    else:
        click.echo(f"next: paperpress add pdf {slug} <folder of PDFs> --dry-run")


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
        click.echo("dry run: nothing downloaded; run the same command without --dry-run")
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
        click.echo("dry run: nothing added; run the same command without --dry-run")
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
@click.argument("titles", nargs=-1)
def refresh(titles):
    """Update Internet Archive issues' metadata (page numbers, rights) without
    downloading the images again, e.g. after upgrading paperpress."""
    from .sources import ia

    project = _project()
    try:
        slugs = [project.title(t).slug for t in titles] or list(project.titles)
    except ProjectError as e:
        raise click.ClickException(str(e))
    done = failed = 0
    for slug in slugs:
        for d in project.issue_dirs(slug, include_undated=True):
            if read_issue(d)["source"].get("type") != "internet_archive":
                continue
            try:
                ia.refresh_issue(d)
                done += 1
            except ia.IAError as e:
                failed += 1
                click.echo(f"FAILED {d.relative_to(project.root)}: {e}", err=True)
    click.echo(f"refreshed {done} issue(s)" + (f", {failed} failed" if failed else ""))


@main.command()
@click.argument("titles", nargs=-1)
@click.option("--from", "date_from", help="Only issues on or after this date (YYYY-MM-DD).")
@click.option("--to", "date_to", help="Only issues on or before this date (YYYY-MM-DD).")
@click.option("--limit", type=int, help="OCR at most this many issues this run.")
@click.option("--force", is_flag=True, help="Redo pages that were already OCR'd.")
def ocr(titles, date_from, date_to, limit, force):
    """OCR every issue (or just TITLES) with newspaper-ocr.

    Finished pages are skipped, so you can stop at any time and re-run the same
    command to pick up where it left off. The engine is set in paper.toml's
    [ocr] table (default: DocLayout-YOLO layout + Tesseract).
    """
    from . import ocr as ocr_stage

    project = _project()
    try:
        slugs = [project.title(t).slug for t in titles] or list(project.titles)
    except ProjectError as e:
        raise click.ClickException(str(e))
    todo = []
    for slug in slugs:
        for d in project.issue_dirs(slug, include_undated=True):
            date = DATE_DIR.match(d.name)
            if (date_from or date_to) and not date:
                continue
            if date and ((date_from and date[1] < date_from) or (date_to and date[1] > date_to)):
                continue
            if force or not ocr_stage.is_done(d):
                todo.append(d)
    if limit:
        todo = todo[:limit]
    if not todo:
        click.echo("nothing to OCR (every issue is done; --force to redo)")
        return

    pages_left = sum(len(read_issue(d)["pages"]) for d in todo)
    click.echo(f"OCR: {len(todo)} issue(s), {pages_left} page(s)")
    try:
        engine = ocr_stage.NewspaperOCR(project.ocr)
    except (ValueError, ImportError) as e:
        raise click.ClickException(f"can't start the OCR engine: {e}")
    about = engine.describe()
    engine_name = about.pop("engine", "OCR")
    click.echo(f"engine: {engine_name} ("
               + ", ".join(f"{k}={v}" for k, v in about.items()) + ")")

    total_pages = total_secs = 0.0
    failed = []
    for n, d in enumerate(todo, start=1):
        rel = d.relative_to(project.root / "titles")
        try:
            s = ocr_stage.ocr_issue(d, engine, force=force,
                                    log=lambda m: click.echo(f"\r  {rel} {m} ", nl=False))
        except Exception as e:                     # one bad page shouldn't stop the run
            failed.append(d)
            click.echo(f"[{n}/{len(todo)}] FAILED {rel}: {e}", err=True)
            continue
        total_pages += s["done"]
        total_secs += s["seconds"]
        pages_left -= s["pages"]
        eta = ""
        if total_pages and pages_left:
            eta = f", ~{pages_left * total_secs / total_pages / 60:.0f} min left"
        flagged = f", {s['flagged']} flagged" if s["flagged"] else ""
        click.echo(f"\r[{n}/{len(todo)}] {rel}: {s['pages']} pages, {s['regions']} regions"
                   f"{flagged} ({s['seconds']:.0f}s{eta})")
    if failed:
        raise click.ClickException(f"{len(failed)} issue(s) failed; re-run to retry")


@main.command()
@click.argument("titles", nargs=-1)
@click.option("--from", "date_from", help="Only issues on or after this date (YYYY-MM-DD).")
@click.option("--to", "date_to", help="Only issues on or before this date (YYYY-MM-DD).")
@click.option("--limit", type=int, help="Enrich at most this many issues this run.")
@click.option("--model", help="Model for the per-page pass (overrides paper.toml).")
@click.option("--stitch-model", help="Model for linking continued stories (overrides paper.toml).")
@click.option("--force", is_flag=True, help="Redo issues (and pages) already enriched.")
def enrich(titles, date_from, date_to, limit, model, stitch_model, force):
    """Optional: build each issue's table of contents with an LLM.

    Groups each page's OCR regions into articles (headline, author, type,
    section, language, ad or not) and links stories continued on other pages.
    Needs an API key (OPENROUTER_API_KEY by default) and costs money: roughly
    a few cents per issue with the default models. Issues must be OCR'd first.
    """
    from . import enrich as en

    project = _project()
    try:
        slugs = [project.title(t).slug for t in titles] or list(project.titles)
    except ProjectError as e:
        raise click.ClickException(str(e))
    settings = {**en.DEFAULT_ENRICH, **project.enrich}
    if model:
        settings["model"] = model
    if stitch_model:
        settings["stitch_model"] = stitch_model
    todo, not_ocrd = [], 0
    for slug in slugs:
        for d in project.issue_dirs(slug, include_undated=True):
            date = DATE_DIR.match(d.name)
            if (date_from or date_to) and not date:
                continue
            if date and ((date_from and date[1] < date_from) or (date_to and date[1] > date_to)):
                continue
            if not (d / "full_text.json").exists():
                not_ocrd += 1
            elif force or not en.is_done(d):
                todo.append(d)
    if limit:
        todo = todo[:limit]
    if not_ocrd:
        click.echo(f"skipping {not_ocrd} issue(s) not OCR'd yet")
    if not todo:
        click.echo("nothing to enrich (every OCR'd issue has a toc.json; --force to redo)")
        return
    try:
        llm = en.make_llm(settings)
    except en.EnrichError as e:
        raise click.ClickException(str(e))
    click.echo(f"enrich: {len(todo)} issue(s) with {settings['model']} "
               f"(stitch: {settings['stitch_model']})")
    failed = []
    for n, d in enumerate(todo, start=1):
        rel = d.relative_to(project.root / "titles")
        before = llm.cost
        try:
            s = en.enrich_issue(project, d, llm, settings, force=force)
        except (en.EnrichError, OSError, ValueError) as e:
            failed.append(d)
            click.echo(f"[{n}/{len(todo)}] FAILED {rel}: {e}", err=True)
            continue
        cost = f", ${llm.cost - before:.3f}" if llm.cost else ""
        found = ", ".join(f"{k} {v}" for k, v in s["filled"].items())
        click.echo(f"[{n}/{len(todo)}] {rel}: {s['toc']} articles, {s['ads']} ads, "
                   f"{s['continued']} continued{cost}"
                   + (f"; read {found} from the masthead" if found else ""))
    if llm.cost:
        click.echo(f"total cost ${llm.cost:.2f} ({llm.tokens:,} tokens)")
    if failed:
        raise click.ClickException(f"{len(failed)} issue(s) failed; re-run to retry "
                                   f"(finished pages are cached)")


@main.command()
@click.argument("title")
@click.option("--sample", type=int, default=6, show_default=True,
              help="How many issues to read, spread across the run.")
@click.option("--model", help="Model to use (default: the [enrich] stitch_model).")
@click.option("--force", is_flag=True, help="Replace an existing profile.json.")
def profile(title, sample, model, force):
    """Optional: draft a profile of TITLE for `enrich` from a sample of issues.

    An LLM reads a few OCR'd issues and lists the paper's regular sections and
    columns, contributors (with the ways OCR misspells them), organizations,
    advertisers and languages. `enrich` uses the profile to recognize sections
    and spell names. The draft is written to titles/<title>/profile.json for you
    to check and edit. Costs 5–20 cents with the default model.
    """
    from . import enrich as en
    from .profile import ProfileError, draft_profile, write_profile

    project = _project()
    try:
        slug = project.title(title).slug
    except ProjectError as e:
        raise click.ClickException(str(e))
    path = project.title_dir(slug) / "profile.json"
    if path.exists() and not force:
        raise click.ClickException(f"{path.relative_to(project.root)} already exists "
                                   f"(edit it, or --force to draft a new one)")
    settings = {**en.DEFAULT_ENRICH, **project.enrich}
    model = model or settings["stitch_model"]
    try:
        llm = en.make_llm(settings)
        click.echo(f"drafting a profile of {project.titles[slug].name} with {model}")
        prof = draft_profile(project, slug, llm, model, sample=sample, log=click.echo)
    except (ProfileError, en.EnrichError) as e:
        raise click.ClickException(str(e))
    path = write_profile(project, slug, prof)
    counts = ", ".join(f"{len(prof[k])} {k.replace('_', ' ')}" for k in
                       ("sections", "columns", "contributors", "organizations", "ocr_fixes")
                       if prof.get(k))
    click.echo(f"wrote {path.relative_to(project.root)}: {counts or 'nothing found'}"
               + (f" (${llm.cost:.3f})" if llm.cost else ""))
    click.echo("check it and fix what's wrong, then `paperpress enrich --force "
               f"{slug}` to use it on issues already enriched")


@main.command("export")
@click.argument("titles", nargs=-1)
@click.option("--out", type=click.Path(file_okay=False, path_type=Path),
              help="Folder to write (default: export/ in the project).")
@click.option("--txt", is_flag=True, help="Also write one plain-text file per page.")
def export_cmd(titles, out, txt):
    """Write the OCR'd text as a research corpus (JSONL + CSV, optional .txt).

    One row per page with title, date, volume/number, page, word count, a
    ready-made citation, a link back to the source, and the text in reading
    order. Re-running replaces the previous export.
    """
    from .export import export

    project = _project()
    try:
        slugs = [project.title(t).slug for t in titles] or list(project.titles)
    except ProjectError as e:
        raise click.ClickException(str(e))
    dest = (out or project.root / "export").resolve()
    s = export(project, slugs, dest, txt=txt)
    if s["skipped"]:
        click.echo(f"skipped {len(s['skipped'])} issue(s) not OCR'd yet "
                   f"(run `paperpress ocr`)")
    if not s["pages"]:
        raise click.ClickException("nothing to export yet: no issue has been OCR'd")
    click.echo(f"exported {plural(s['issues'], 'issue')}, {plural(s['pages'], 'page')}, "
               f"{plural(s['words'], 'word')} "
               f"-> {dest}")


@main.command()
@click.argument("titles", nargs=-1)
@click.option("--out", type=click.Path(file_okay=False, path_type=Path),
              help="Folder for the bag (default: <project>-bag/ beside the project).")
@click.option("--organization", help="Source-Organization for bag-info.txt, e.g. your "
              "university or department.")
@click.option("--contact-name", help="Contact-Name for bag-info.txt.")
@click.option("--contact-email", help="Contact-Email for bag-info.txt.")
@click.option("--check", "check_path", type=click.Path(exists=True, file_okay=False,
              path_type=Path), help="Instead of making a bag, check that this bag is intact.")
def bag(titles, out, organization, contact_name, contact_email, check_path):
    """Package the project as a BagIt bag, for depositing with a library or data
    repository (Dataverse, Zenodo, an institutional repository, ...).

    The bag holds paper.toml, every issue folder (scans, OCR, contents, source
    files), and a fresh export with Dublin Core records, plus SHA-256 and SHA-512
    checksums for every file so the receiver can confirm nothing was lost or
    changed. The cache and the website are left out; both can be rebuilt.
    """
    from .bag import BagError, check_bag, make_bag

    if check_path:
        problems = check_bag(check_path)
        for p in problems[:50]:
            click.echo(p, err=True)
        if problems:
            raise click.ClickException(f"{len(problems)} problem(s): the bag is not intact")
        click.echo(f"{check_path}: valid, every file matches its checksum")
        return
    project = _project()
    try:
        slugs = [project.title(t).slug for t in titles] or list(project.titles)
    except ProjectError as e:
        raise click.ClickException(str(e))
    dest = out or project.root.parent / f"{project.root.name}-bag"
    info = {"Source-Organization": organization, "Contact-Name": contact_name,
            "Contact-Email": contact_email}
    try:
        s = make_bag(project, dest, slugs, info=info, log=click.echo)
    except BagError as e:
        raise click.ClickException(str(e))
    click.echo(f"bagged {plural(s['issues'], 'issue')}, {plural(s['files'], 'file')} "
               f"({s['bytes'] / 1e6:,.0f} MB) -> {s['path']}")


@main.command()
@click.option("--out", type=click.Path(file_okay=False, path_type=Path),
              help="Folder to write (default: site/ in the project).")
@click.option("--url", help="The address the site will be published at, e.g. "
              "https://you.github.io/suffrage-press/. Adds IIIF manifests (which need full "
              "addresses) and sets --base for you.")
@click.option("--base", help="URL path the site will live under, e.g. /suffrage-press/ for "
              "a GitHub Pages project site (default: /, or the path of --url).")
@click.option("--images", type=click.Choice(["copy", "ia"]), default="copy", show_default=True,
              help="copy: put resized scans in the site. ia: show Internet Archive pages "
                   "from IA's image server (smaller site, depends on IA).")
@click.option("--image-width", type=int, default=1800, show_default=True,
              help="Width of copied page images, in pixels.")
def build(out, url, base, images, image_width):
    """Write the archive as a static website (search included) for any web host.

    The result is a folder of plain files: put it on GitHub Pages, Netlify, or
    any web server. Only what you've added is published, so check you have the
    right to share the scans first.
    """
    from .build import BuildError, build as build_site

    project = _project()
    dest = (out or project.root / "site").resolve()
    try:
        s = build_site(project, dest, base=base, url=url, images=images,
                       image_width=image_width, log=click.echo)
    except BuildError as e:
        raise click.ClickException(str(e))
    click.echo(f"built {plural(s['issues'], 'issue')}, {plural(s['pages'], 'page')} "
               f"({s['bytes'] / 1e6:.0f} MB) -> {dest}")
    if s["iiif"]:
        click.echo(f"IIIF: {s['iiif']} (once published)")
    else:
        click.echo("add --url <the site's address> to include IIIF manifests")
    if s["base"] == "/":
        click.echo(f"preview: python3 -m http.server -d {dest} 8001   then open "
                   f"http://127.0.0.1:8001/")
    else:
        click.echo(f"to preview, the folder must be served at {s['base']}; or build again "
                   f"without --url/--base and preview that")


@main.command()
@click.option("--port", type=int, default=8000, show_default=True,
              help="Port to listen on (the next free one is used if it's taken).")
@click.option("--no-open", is_flag=True, help="Don't open a browser window.")
def serve(port, no_open):
    """Browse, read and search the archive in your web browser.

    Runs only on this computer (127.0.0.1); nothing is shared. Stop with Ctrl-C.
    """
    import webbrowser

    from . import search
    from .serve import make_server

    project = _project()
    _, rebuilt = search.ensure_index(project)
    if rebuilt:
        click.echo("built the search index")
    try:
        server = make_server(project, port)
    except OSError as e:
        raise click.ClickException(str(e))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    click.echo(f"serving {project.name} at {url}  (Ctrl-C to stop)")
    if not no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("\nstopped")
    finally:
        server.server_close()


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
        ocrd = sum(1 for d in dirs if (d / "full_text.json").exists())
        tocs = sum(1 for d in dirs if (d / "toc.json").exists())
        click.echo(f"{t.name} ({slug}): {plural(len(dirs), 'issue')}, "
                   f"{plural(pages, 'page')}, {span}; "
                   f"OCR'd {ocrd}/{len(dirs)}" + (f", enriched {tocs}" if tocs else ""))
        if undated:
            click.echo(f"  {len(undated)} undated: " + ", ".join(d.name for d in undated))
        dup = [d for d, c in dates.items() if c > 1]
        if dup:
            click.echo(f"  {len(dup)} date(s) with more than one issue: " + ", ".join(dup))


if __name__ == "__main__":
    main()
