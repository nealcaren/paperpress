"""The project format: paper.toml plus the titles/<slug>/<date>/ folder layout.

Every paperpress stage reads and writes this layout, so it is the contract
between them:

    paper.toml
    titles/<slug>/<YYYY-MM-DD>/
        issue.json          issue metadata + provenance + page list (written last;
                            its presence means the issue was fetched completely)
        images/page_NN.jpg  page images, 1-based, in reading order
        source/             anything carried over from the source (e.g. its OCR)
    titles/<slug>/_undated/<source-id>/
                            issues whose date could not be determined yet

Two issues of the same title on the same date (a reprint, a second edition, a
duplicate upload) get `<date>__<source-id>` so neither overwrites the other.
"""
from __future__ import annotations

import json
import re
import shutil
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

CONFIG_NAME = "paper.toml"
UNDATED = "_undated"
DATE_DIR = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:__(.+))?$")

CONFIG_TEMPLATE = '''\
# paperpress project file. One [[titles]] block per periodical.

[project]
name = "{name}"

# [[titles]]
# slug = "womans-journal"              # folder name under titles/
# name = "The Woman's Journal"
# kind = "newspaper"                   # newspaper | magazine
# ia_query = "collection:pub_the-womans-journal"   # optional default for `paperpress add ia`
'''


class ProjectError(Exception):
    pass


@dataclass
class Title:
    slug: str
    name: str
    kind: str = "newspaper"
    ia_query: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class Project:
    root: Path
    name: str
    titles: dict[str, Title]

    @classmethod
    def load(cls, start: Path | str = ".") -> "Project":
        """Find paper.toml in `start` or any parent directory and load it."""
        start = Path(start).resolve()
        for d in (start, *start.parents):
            cfg = d / CONFIG_NAME
            if cfg.exists():
                return cls._from_file(cfg)
        raise ProjectError(f"no {CONFIG_NAME} found in {start} or its parents "
                           f"(run `paperpress init` first)")

    @classmethod
    def _from_file(cls, cfg: Path) -> "Project":
        try:
            data = tomllib.loads(cfg.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ProjectError(f"{cfg}: {e}") from e
        titles = {}
        for t in data.get("titles", []):
            if "slug" not in t or "name" not in t:
                raise ProjectError(f"{cfg}: every [[titles]] entry needs slug and name")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", t["slug"]):
                raise ProjectError(f"{cfg}: slug {t['slug']!r} must be lowercase letters, "
                                   f"digits and hyphens")
            known = {"slug", "name", "kind", "ia_query"}
            titles[t["slug"]] = Title(
                slug=t["slug"], name=t["name"], kind=t.get("kind", "newspaper"),
                ia_query=t.get("ia_query"),
                extra={k: v for k, v in t.items() if k not in known})
        return cls(root=cfg.parent, name=data.get("project", {}).get("name", cfg.parent.name),
                   titles=titles)

    def title(self, slug: str) -> Title:
        if slug not in self.titles:
            have = ", ".join(self.titles) or "none defined"
            raise ProjectError(f"unknown title {slug!r} (paper.toml has: {have})")
        return self.titles[slug]

    def title_dir(self, slug: str) -> Path:
        return self.root / "titles" / slug

    def issue_dirs(self, slug: str, *, include_undated: bool = False) -> list[Path]:
        """Completed issue folders for a title, in date order."""
        base = self.title_dir(slug)
        if not base.exists():
            return []
        out = sorted(d for d in base.iterdir()
                     if d.is_dir() and DATE_DIR.match(d.name) and (d / "issue.json").exists())
        if include_undated and (base / UNDATED).exists():
            out += sorted(d for d in (base / UNDATED).iterdir()
                          if d.is_dir() and not d.name.endswith(".partial")
                          and (d / "issue.json").exists())
        return out

    def find_issue(self, slug: str, source_id: str) -> Path | None:
        """The folder already holding `source_id`, if this source was fetched before."""
        for d in self.issue_dirs(slug, include_undated=True):
            if read_issue(d).get("source", {}).get("id") == source_id:
                return d
        return None

    def new_issue_dir(self, slug: str, date: str | None, source_id: str) -> Path:
        """Where a newly fetched issue should go (never an occupied folder)."""
        base = self.title_dir(slug)
        if date is None:
            return base / UNDATED / safe_name(source_id)
        d = base / date
        if d.exists() and (d / "issue.json").exists():
            return base / f"{date}__{safe_name(source_id)}"
        return d


def init_project(root: Path, name: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    cfg = root / CONFIG_NAME
    if cfg.exists():
        raise ProjectError(f"{cfg} already exists")
    cfg.write_text(CONFIG_TEMPLATE.format(name=(name or root.resolve().name).replace('"', "'")))
    (root / "titles").mkdir(exist_ok=True)
    return cfg


def read_issue(issue_dir: Path) -> dict:
    return json.loads((issue_dir / "issue.json").read_text())


def write_issue(issue_dir: Path, data: dict) -> None:
    """Write issue.json atomically; it is the completion marker for the folder."""
    tmp = issue_dir / "issue.json.tmp"
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(issue_dir / "issue.json")


@contextmanager
def staged_issue(dest: Path) -> Iterator[Path]:
    """Build an issue in `<dest>.partial`, then swap it into `dest` on success.

    The body must call write_issue() on the yielded folder. On any error the
    partial folder is removed, so a failed fetch never leaves a half-issue.
    """
    tmp = dest.with_name(dest.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "images").mkdir(parents=True)
    (tmp / "source").mkdir()
    try:
        yield tmp
        if not (tmp / "issue.json").exists():
            raise ProjectError(f"{dest.name}: issue.json was never written")
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)


def page_name(n: int) -> str:
    return f"page_{n:02d}"
