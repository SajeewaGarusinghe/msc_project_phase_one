#!/usr/bin/env python3
"""Parse every YAML file in the repo and report failures.

Helm values files are plain YAML, so this catches the most common authoring
error (a bad indent) without needing a cluster or the Helm binary.
"""
import glob
import sys

import yaml

PATTERNS = [
    "platform/**/*.yaml",
    "infra/local/*.yaml",
    ".github/workflows/*.yml",
]

failed = 0
checked = 0
for pattern in PATTERNS:
    for path in sorted(glob.glob(pattern, recursive=True)):
        try:
            list(yaml.safe_load_all(open(path)))
            checked += 1
        except Exception as exc:  # noqa: BLE001 - we want every failure reported
            print(f"  FAIL {path}: {exc}")
            failed += 1

print(f"  {checked} YAML files OK" + (f", {failed} FAILED" if failed else ""))
sys.exit(1 if failed else 0)
