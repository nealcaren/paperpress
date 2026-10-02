"""`paperpress bag`: package the project as a BagIt bag (RFC 8493) for deposit.

BagIt is how libraries, archives and data repositories (Dataverse, Zenodo,
institutional repositories, APTrust, ...) take in a collection: the files go in
data/ unchanged, and checksum manifests beside them let the receiver prove that
every file arrived intact, now and years later.

    <bag>/
      bagit.txt                 BagIt-Version: 1.0
      bag-info.txt              who made it, when, how big, what it is
      manifest-sha256.txt       a checksum for every file in data/
      manifest-sha512.txt
      tagmanifest-sha256.txt    checksums for the files above
      tagmanifest-sha512.txt
      data/
        README.txt              what's in the bag
        paper.toml
        titles/<slug>/...       issue folders: scans, OCR, contents, source files
        export/                 the research corpus and Dublin Core records

Caches (.paperpress/) and the website (site/) are left out: both can be rebuilt
from what's in the bag. Built in <bag>.partial and swapped in at the end.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date
from pathlib import Path
from typing import Callable

from . import __version__
from .export import export
from .project import CONFIG_NAME, OUTPUT_MARKER, Project, ProjectError, check_output

ALGORITHMS = ("sha256", "sha512")
SKIP_NAMES = {".DS_Store", "Thumbs.db"}


class BagError(Exception):
    pass


def _hashes(path: Path) -> dict[str, str]:
    hs = {a: hashlib.new(a) for a in ALGORITHMS}
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            for h in hs.values():
                h.update(chunk)
    return {a: h.hexdigest() for a, h in hs.items()}


def _encode(rel: str) -> str:
    """A manifest path, percent-encoding the characters BagIt reserves."""
    return rel.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _decode(rel: str) -> str:
    return rel.replace("%0A", "\n").replace("%0D", "\r").replace("%25", "%")


def _skip(path: Path) -> bool:
    return (path.name in SKIP_NAMES or path.name.endswith((".tmp", ".partial"))
            or any(p.endswith(".partial") for p in path.parts))


def _copy_tree(src: Path, dest: Path) -> int:
    n = 0
    for f in sorted(src.rglob("*")):
        if f.is_file() and not _skip(f.relative_to(src)):
            target = dest / f.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            n += 1
    return n


def _drop_local_path(issue_json: Path) -> None:
    """A PDF issue records where its file was on this computer (source.path), which
    names the user's own folders; a deposit keeps only the file name."""
    rec = json.loads(issue_json.read_text())
    src = rec.get("source", {})
    if src.get("path"):
        src.setdefault("filename", Path(src["path"]).name)
        del src["path"]
        issue_json.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")


def _manifests(bag: Path, files: list[Path], prefix: str) -> None:
    lines = {a: [] for a in ALGORITHMS}
    for f in files:
        rel = _encode(f.relative_to(bag).as_posix())
        for a, digest in _hashes(f).items():
            lines[a].append(f"{digest}  {rel}\n")
    for a in ALGORITHMS:
        (bag / f"{prefix}-{a}.txt").write_text("".join(lines[a]), encoding="utf-8")


def make_bag(project: Project, dest: Path, slugs: list[str] | None = None, *,
             info: dict[str, str] | None = None,
             log: Callable[[str], None] = lambda m: None) -> dict:
    slugs = slugs or list(project.titles)
    dest = dest.resolve()
    try:
        check_output(project, dest, _is_our_bag)
    except ProjectError as e:
        raise BagError(str(e)) from e
    tmp = dest.with_name(dest.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp)
    data = tmp / "data"
    data.mkdir(parents=True)
    try:
        shutil.copy2(project.root / CONFIG_NAME, data / CONFIG_NAME)
        n_issues = 0
        for slug in slugs:
            issues = project.issue_dirs(slug, include_undated=True)
            log(f"copying {len(issues)} issue(s) of {project.titles[slug].name}")
            for d in issues:
                _copy_tree(d, data / d.relative_to(project.root))
                _drop_local_path(data / d.relative_to(project.root) / "issue.json")
                n_issues += 1
            for extra in ("profile.json",):
                f = project.title_dir(slug) / extra
                if f.exists():
                    shutil.copy2(f, data / f.relative_to(project.root))
        log("exporting the corpus")
        s = export(project, slugs, data / "export")
        (data / "export" / OUTPUT_MARKER).unlink()
        (data / "README.txt").write_text(_readme(project, slugs, n_issues, s), encoding="utf-8")

        payload = sorted(f for f in data.rglob("*") if f.is_file())
        log(f"checksumming {len(payload):,} files")
        _manifests(tmp, payload, "manifest")
        size = sum(f.stat().st_size for f in payload)
        (tmp / "bagit.txt").write_text("BagIt-Version: 1.0\n"
                                       "Tag-File-Character-Encoding: UTF-8\n", encoding="utf-8")
        fields = {
            **{k: v for k, v in (info or {}).items() if v},
            "External-Description": f"{project.name}: {n_issues} issues of "
                                    + ", ".join(project.titles[s].name for s in slugs)
                                    + ". Page images, OCR text and metadata, made with "
                                      "paperpress.",
            "Bagging-Date": date.today().isoformat(),
            "Bag-Software-Agent": f"paperpress {__version__} "
                                  f"(https://github.com/nealcaren/paperpress)",
            "Payload-Oxum": f"{size}.{len(payload)}",
        }
        (tmp / "bag-info.txt").write_text(
            "".join(f"{k}: {' '.join(str(v).split())}\n" for k, v in fields.items()),
            encoding="utf-8")
        tags = sorted(f for f in tmp.iterdir()
                      if f.is_file() and not f.name.startswith("tagmanifest-"))
        _manifests(tmp, tags, "tagmanifest")
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)
    return {"issues": n_issues, "files": len(payload), "bytes": size, "path": dest}


def _is_our_bag(d: Path) -> bool:
    info = d / "bag-info.txt"
    return (d / "bagit.txt").exists() and info.exists() and "paperpress" in info.read_text()


def check_bag(bag: Path) -> list[str]:
    """Problems with a bag: missing, extra or changed files. [] means it's valid."""
    problems = []
    if not (bag / "bagit.txt").exists():
        return [f"{bag} is not a bag (no bagit.txt)"]
    for prefix in ("manifest", "tagmanifest"):
        for a in ALGORITHMS:
            m = bag / f"{prefix}-{a}.txt"
            if not m.exists():
                continue
            listed = {}
            for line in m.read_text(encoding="utf-8").splitlines():
                digest, _, rel = line.partition("  ")
                listed[_decode(rel)] = digest
            for rel, digest in listed.items():
                f = bag / rel
                if not f.is_file():
                    problems.append(f"missing: {rel}")
                elif _hashes(f)[a] != digest:
                    problems.append(f"changed ({a}): {rel}")
            if prefix == "manifest":
                on_disk = {f.relative_to(bag).as_posix() for f in (bag / "data").rglob("*")
                           if f.is_file()}
                problems += [f"not in {m.name}: {rel}" for rel in sorted(on_disk - set(listed))]
    oxum = next((l.split(":", 1)[1].strip() for l in
                 (bag / "bag-info.txt").read_text(encoding="utf-8").splitlines()
                 if l.startswith("Payload-Oxum:")), None) if (bag / "bag-info.txt").exists() \
        else None
    if oxum:
        files = [f for f in (bag / "data").rglob("*") if f.is_file()]
        actual = f"{sum(f.stat().st_size for f in files)}.{len(files)}"
        if actual != oxum:
            problems.append(f"Payload-Oxum is {oxum}, the data is {actual}")
    return sorted(set(problems))


def _readme(project: Project, slugs: list[str], n_issues: int, s: dict) -> str:
    titles = "\n".join(f"  {project.titles[x].name} ({x})" for x in slugs)
    return f"""{project.name}
Packaged {date.today().isoformat()} by paperpress {__version__}
(https://github.com/nealcaren/paperpress).

{n_issues} issues, {s['pages']:,} OCR'd pages, {s['words']:,} words, from:
{titles}

paper.toml          the project's settings: titles, OCR engine, LLM settings
titles/<title>/<date>/
    issue.json      the issue's date, volume and number, where it came from (source
                    URL or file name, rights), and its pages
    images/         the page scans, page_01.jpg, page_02.jpg, ...
    page_NN.json    each page's OCR: text regions in reading order with their
                    positions on the scan (newspaper-ocr JSON)
    full_text.json  the issue's text, page by page
    toc.json        a table of contents drafted by an LLM (if made)
    source/         what the source supplied, e.g. its own OCR, kept for comparison
titles/<title>/_undated/<id>/   issues whose date couldn't be determined
titles/<title>/profile.json     notes on the periodical used to draft contents
export/             the corpus as CSV and JSONL, and dublin_core.csv with one
                    Dublin Core record per issue; see export/README.txt

All text was produced by machine OCR and contains errors; check quotations against
the page images. Tables of contents and their headlines and authors were drafted by
an LLM from the OCR.
"""
