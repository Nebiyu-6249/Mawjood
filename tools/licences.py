#!/usr/bin/env python
"""Regenerate the open-source licence inventory.

    uv run python tools/licences.py            # markdown tables to stdout
    uv run python tools/licences.py --json     # machine-readable

An inventory written once is a snapshot that rots on the next `uv sync`. This
reads the installed distributions' own metadata, so re-running it after a
dependency change tells you what actually changed rather than what someone
remembered to update.

It separates **runtime** dependencies — the closure that ships inside the Docker
image, and therefore the only ones whose obligations reach a deployment — from
**development** tooling, which never leaves a developer's machine or CI. That
distinction is the whole point: a copyleft build tool is not a distribution
question, a copyleft runtime library might be.

Licence strings come from package metadata and are normalised to SPDX-ish
identifiers where the mapping is unambiguous. Metadata is self-declared and
occasionally wrong; for anything load-bearing, read the package's LICENSE file.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections import Counter
from importlib.metadata import distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Legacy classifier text and older free-text fields, mapped to the SPDX
# identifier they plainly mean. Anything not here is passed through untouched
# rather than guessed at.
NORMALISE = {
    "MIT License": "MIT",
    "BSD License": "BSD-3-Clause",
    "BSD": "BSD-3-Clause",
    "Apache Software License": "Apache-2.0",
    "Apache License, Version 2.0": "Apache-2.0",
    "Apache 2.0": "Apache-2.0",
    "Python Software Foundation License": "PSF-2.0",
    "PSFL": "PSF-2.0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "ISC License (ISCL)": "ISC",
    "The Unlicense (Unlicense)": "Unlicense",
}

# Licences that need a person to look before the next release, not a build to
# fail. None of these are blockers for Mawjood's use; see docs/LICENCES.md.
REVIEW = {"MPL-2.0", "GPL", "AGPL", "LGPL", "SSPL", "BUSL", "UNKNOWN"}


def _canonical(name: str) -> str:
    return name.lower().replace("_", "-")


def _licence_of(metadata: object) -> str:
    """Best available licence string for one distribution."""
    get = getattr(metadata, "get", lambda *_: None)
    get_all = getattr(metadata, "get_all", lambda *_: None)

    # Modern, unambiguous, and preferred when present.
    expression = get("License-Expression")
    if expression:
        return str(expression).strip()

    # Some packages put the full licence text in License:. A short value is an
    # identifier; a long one is the licence itself and useless in a table.
    declared = str(get("License") or "").strip()
    if declared and len(declared) <= 60:
        return NORMALISE.get(declared, declared)

    classifiers = [c for c in (get_all("Classifier") or []) if c.startswith("License ::")]
    if classifiers:
        tails = [c.split(" :: ")[-1] for c in classifiers]
        return "; ".join(NORMALISE.get(t, t) for t in tails)

    return "UNKNOWN"


def installed() -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for dist in distributions():
        name = dist.metadata["Name"]
        if not name:
            continue
        found[_canonical(name)] = {
            "name": name,
            "version": dist.version,
            "licence": _licence_of(dist.metadata),
        }
    return found


def runtime_closure() -> set[str]:
    """The packages that ship in the image.

    ``uv export --no-dev`` resolves what a production install pulls in. If uv is
    unavailable — someone running this from a plain venv — fall back to
    everything, and say so rather than silently reporting a wrong split.
    """
    uv = shutil.which("uv")
    if uv is None:
        return set()
    try:
        result = subprocess.run(  # noqa: S603 — resolved absolute path, fixed arguments
            [uv, "export", "--no-dev", "--no-emit-project", "--no-hashes"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=120,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return set()

    names: set[str] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        names.add(_canonical(line.split("==", 1)[0]))
    return names


def _table(rows: list[dict[str, str]]) -> str:
    lines = ["| Package | Version | Licence |", "|---|---|---|"]
    for row in rows:
        lines.append(f"| `{row['name']}` | {row['version']} | {row['licence']} |")
    return "\n".join(lines)


def _summary(rows: list[dict[str, str]]) -> str:
    counts = Counter(r["licence"] for r in rows)
    lines = ["| Licence | Packages |", "|---|---|"]
    for licence, count in counts.most_common():
        lines.append(f"| {licence} | {count} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Open-source licence inventory.")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    packages = installed()
    packages.pop("mawjood", None)  # Mawjood is not its own dependency.
    closure = runtime_closure()

    def bucket(canonical: str) -> str:
        if not closure:
            return "unknown"
        return "runtime" if canonical in closure else "development"

    rows = [{**info, "scope": bucket(canonical)} for canonical, info in sorted(packages.items())]
    # Packages resolved for another platform (colorama is Windows-only) appear in
    # the closure without being installed here. Named so the count is honest.
    missing = sorted(c for c in closure if c not in packages)

    if args.json:
        print(
            json.dumps(
                {
                    "packages": rows,
                    "resolved_but_not_installed_on_this_platform": missing,
                    "totals": {
                        "runtime": sum(1 for r in rows if r["scope"] == "runtime"),
                        "development": sum(1 for r in rows if r["scope"] == "development"),
                    },
                },
                indent=2,
            )
        )
        return 0

    runtime = [r for r in rows if r["scope"] == "runtime"]
    development = [r for r in rows if r["scope"] == "development"]

    print(f"## Runtime ({len(runtime)})\n")
    print(_summary(runtime))
    print()
    print(_table(runtime))
    print(f"\n## Development ({len(development)})\n")
    print(_summary(development))
    print()
    print(_table(development))

    if missing:
        print(f"\nResolved but not installed on this platform: {', '.join(missing)}")

    needs_review = sorted(
        {r["licence"] for r in rows if any(flag in r["licence"] for flag in REVIEW)}
    )
    if needs_review:
        print(f"\nNeeds a look: {', '.join(needs_review)}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
