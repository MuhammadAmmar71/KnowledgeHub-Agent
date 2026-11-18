"""
Project numbers: any standalone run of exactly 8 digits.

Used on both sides of search so they always agree on what a project number
is: chunks are tagged with the numbers found in their text and in their
document's name/path at chunking time, and a question mentioning a number is
restricted to chunks tagged with it.

"Standalone" means not part of a longer digit run -- letters and
punctuation around it are fine, which is the point: Postgres full-text
search can't see 10000001 inside "10000001pulsedashboardacr" (one token) or
"pulsedashboard-10000001-contapp" (a negative integer), but this does.

[0-9] rather than \\d: \\d also matches non-ASCII digits (e.g. Arabic-Indic),
which aren't project numbers. Keep PROJECT_NUMBER_SQL_PATTERN in sync -- the
migration that backfilled existing chunks uses it.
"""
from __future__ import annotations

import re

PROJECT_NUMBER_LENGTH = 8
_PROJECT_NUMBER_RE = re.compile(r"(?<![0-9])[0-9]{8}(?![0-9])")
PROJECT_NUMBER_SQL_PATTERN = r"(?<![0-9])[0-9]{8}(?![0-9])"


def extract_project_numbers(*texts: str | None) -> list[str]:
    """Distinct project numbers across all given texts, sorted."""
    found: set[str] = set()
    for text in texts:
        if text:
            found.update(_PROJECT_NUMBER_RE.findall(text))
    return sorted(found)


def is_project_number(value: str) -> bool:
    return len(value) == PROJECT_NUMBER_LENGTH and value.isascii() and value.isdigit()
