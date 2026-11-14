"""The split manifest and the test lock (build-guide P1 item 3).

Two jobs, and the second is the one that matters:

1. Write `manifest/split.json`: every template's split, its case ids, and the SHA-256 of every
   file in every case. Test P1-T1 re-hashes and compares, and checks that no template appears in
   two splits.
2. Refuse to open a test case before the G3 freeze. Test P1-T2 requires the runner to *exit with
   an error* on any test case id until the freeze tag exists -- not to warn, and not to skip
   quietly, because a silent skip looks the same as a case that was never requested.

The freeze is a git tag (default `g3-freeze`) rather than a file, so that lifting the lock is a
recorded, dated, pushed act rather than a `touch`.

The hygiene rule this enforces is the one in force after K10: a test incident may sit on disk, in
an archive or an export, but is never opened, inspected, built, run or summarised before G3.
Hashing a file is not opening a case in that sense -- it is how the seal is taken, and it reads
bytes without parsing or retaining them.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

Split = Literal["dev", "S1", "S2", "S3"]
TEST_SPLITS: frozenset[str] = frozenset({"S1", "S2", "S3"})
BLOCK = 1 << 20
DEFAULT_FREEZE_TAG = "g3-freeze"


class TestLockError(RuntimeError):
    """Raised when a test case is requested before the G3 freeze tag exists."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while block := f.read(BLOCK):
            h.update(block)
    return h.hexdigest()


def hash_case(case_dir: Path) -> dict[str, str]:
    """`{file name: sha256}` for one case directory, sorted for a stable manifest."""
    return {p.name: sha256_file(p) for p in sorted(Path(case_dir).iterdir()) if p.is_file()}


@dataclass
class TemplateEntry:
    template: str
    split: str
    cases: dict[str, dict[str, str]]       # case id -> {file: sha256}

    @property
    def case_ids(self) -> list[str]:
        return sorted(self.cases)


def discover(root: Path, split: str, templates: Iterable[str] | None = None) -> list[TemplateEntry]:
    """Walk `<root>/<template>/<n>/` and hash every file."""
    root = Path(root)
    wanted = set(templates) if templates is not None else None
    out = []
    for template_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if wanted is not None and template_dir.name not in wanted:
            continue
        cases = {}
        for case_dir in sorted(p for p in template_dir.iterdir() if p.is_dir()):
            # RCAEval's layout is <template>/<repetition>/, and the repetition is a number.
            # The extracted RE2-OB tree also carries a `multi-source-data` directory beside the
            # repetitions; counting it made the development split 37 cases instead of 36.
            if not case_dir.name.isdigit():
                continue
            cases[f"{template_dir.name}/{case_dir.name}"] = hash_case(case_dir)
        if cases:
            out.append(TemplateEntry(template_dir.name, split, cases))
    return out


#: the case counts the study design fixes (section 11). Production builds assert against these;
#: tests pass `expected_counts=None` because their fixtures are deliberately tiny.
DESIGN_CASE_COUNTS = {"dev": 36, "S1": 54, "S2": 90, "S3": 30}


def build(*, re2_ob: Path, re2_tt: Path, re3_ob: Path, dev_templates: set[str],
          out: Path, expected_counts: dict[str, int] | None = None) -> dict:
    """Build the manifest. dev and S1 both come from RE2-OB, split by the seeded draw."""
    entries: list[TemplateEntry] = []
    entries += discover(re2_ob, "dev", templates=dev_templates)
    all_ob = {p.name for p in Path(re2_ob).iterdir() if p.is_dir()}
    entries += discover(re2_ob, "S1", templates=all_ob - dev_templates)
    entries += discover(re2_tt, "S2")
    entries += discover(re3_ob, "S3")

    seen: dict[str, str] = {}
    for e in entries:
        if e.template in seen:
            raise ValueError(
                f"template {e.template!r} is in both {seen[e.template]} and {e.split}: a template "
                "may never appear in two splits (P1-T1)")
        seen[e.template] = e.split

    expected = expected_counts or {}
    for split, want in expected.items():
        got = sum(len(e.cases) for e in entries if e.split == split)
        if got != want:
            raise ValueError(
                f"split {split} has {got} cases but the design fixes {want}. Either the data set "
                "carries a directory that is not a repetition, or the split is wrong; both must "
                "be resolved rather than recorded (P1-T1).")

    manifest = {
        "schema": 1,
        "expected_case_counts": expected or None,
        "sources": {"RE2-OB": str(re2_ob), "RE2-TT": str(re2_tt), "RE3-OB": str(re3_ob)},
        "counts": {s: sum(1 for e in entries if e.split == s) for s in ("dev", "S1", "S2", "S3")},
        "case_counts": {s: sum(len(e.cases) for e in entries if e.split == s)
                        for s in ("dev", "S1", "S2", "S3")},
        "templates": [
            {"template": e.template, "split": e.split, "cases": e.case_ids,
             "file_sha256": e.cases}
            for e in sorted(entries, key=lambda e: (e.split, e.template))
        ],
    }
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return manifest


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def split_of(manifest: dict, case_id: str) -> str:
    """The split a case id belongs to. `case_id` is `<template>/<n>` or a bare template."""
    template = case_id.split("/")[0]
    for entry in manifest["templates"]:
        if entry["template"] == template:
            return entry["split"]
    raise KeyError(f"{case_id} is not in the manifest")


def verify(manifest: dict, *, roots: dict[str, Path] | None = None) -> list[str]:
    """Re-hash every file and return the paths that differ (P1-T1). Empty means unchanged."""
    roots = roots or {k: Path(v) for k, v in manifest["sources"].items()}
    source_for = {"dev": "RE2-OB", "S1": "RE2-OB", "S2": "RE2-TT", "S3": "RE3-OB"}
    bad = []
    for entry in manifest["templates"]:
        root = Path(roots[source_for[entry["split"]]])
        for case_id, files in entry["file_sha256"].items():
            for name, digest in files.items():
                path = root / case_id / name
                if not path.exists() or sha256_file(path) != digest:
                    bad.append(str(path))
    return bad


def freeze_tag_exists(repo: Path = Path("."), tag: str = DEFAULT_FREEZE_TAG) -> bool:
    run = subprocess.run(["git", "-C", str(repo), "tag", "--list", tag],
                         capture_output=True, text=True)
    return run.returncode == 0 and run.stdout.strip() == tag


class TestLock:
    """Refuses a test case id until the G3 freeze tag exists (P1-T2)."""

    def __init__(self, manifest: dict, *, repo: Path = Path("."),
                 tag: str = DEFAULT_FREEZE_TAG) -> None:
        self.manifest = manifest
        self.repo = Path(repo)
        self.tag = tag

    @property
    def lifted(self) -> bool:
        return freeze_tag_exists(self.repo, self.tag)

    def assert_openable(self, case_id: str) -> str:
        """Return the split, or raise. Call this before a case directory is opened."""
        split = split_of(self.manifest, case_id)
        if split in TEST_SPLITS and not self.lifted:
            raise TestLockError(
                f"refused: {case_id} is in test stratum {split}, and the freeze tag "
                f"{self.tag!r} does not exist in {self.repo}. A test incident is never opened, "
                "inspected, built, run or summarised before G3.")
        return split
