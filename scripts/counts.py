"""Counts quoted in the prose (tools, tables) are rewritten by the generators, so they
cannot go stale. A page marks each one as

    <!-- count:tools -->160<!-- /count -->

and gen_tools.py / gen_schema.py replace the value (or, with --check, verify it).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Every page that may quote a count. A page with no marker is left alone.
PAGES = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]

MARKER = re.compile(r"<!-- count:([a-z-]+) -->(.*?)<!-- /count -->")


def apply(counts: dict[str, int], check: bool) -> bool:
    """Rewrite the marked spans for the names in `counts`. Returns True when every page is
    current (check mode reports stale ones to stderr without writing)."""
    current = True
    for page in PAGES:
        text = page.read_text()

        def sub(m: re.Match[str]) -> str:
            name = m.group(1)
            return f"<!-- count:{name} -->{counts[name]}<!-- /count -->" if name in counts else m.group(0)

        new = MARKER.sub(sub, text)
        if new == text:
            continue
        current = False
        if check:
            print(f"{page.relative_to(ROOT)}: a quoted count is stale (run the generator)", file=sys.stderr)
        else:
            page.write_text(new)
            print(f"updated counts in {page.relative_to(ROOT)}")
    return current

