"""Report inline Markdown links whose relative target does not exist."""

from __future__ import annotations

import re
import sys
from pathlib import Path

_LINK = re.compile(r"\[[^\]]*\]\(([^)]*)\)")
_SKIPPED = ("http://", "https://", "mailto:", "#")


def broken_links(path: Path) -> list[tuple[int, str]]:
    """Return (line number, target) for each broken link in one file, in line order."""
    found: list[tuple[int, str]] = []
    text = path.read_text(encoding="utf-8")
    for number, line in enumerate(text.splitlines(), start=1):
        for match in _LINK.finditer(line):
            target = match.group(1)
            if target.startswith(_SKIPPED):
                continue
            file_part = target.split("#", 1)[0]
            if not (path.parent / file_part).exists():
                found.append((number, target))
    return found


def _files_for(arg: str) -> list[tuple[str, Path]]:
    """Return (display name, path) pairs for one PATH argument."""
    path = Path(arg)
    if path.is_file():
        return [(arg, path)]
    base = arg.rstrip("/") or "/"
    prefix = "" if base == "/" else base
    return [
        (f"{prefix}/{p.relative_to(path).as_posix()}", p)
        for p in sorted(path.rglob("*.md"))
        if p.is_file()
    ]


def main(argv: list[str]) -> int:
    """Check the files and folders in argv; return 0 clean, 1 broken links, 2 usage error."""
    if not argv:
        print("usage: linkcheck.py PATH...", file=sys.stderr)
        return 2
    for arg in argv:
        if not Path(arg).exists():
            print(f"linkcheck: path does not exist: {arg}", file=sys.stderr)
            return 2
    status = 0
    for arg in argv:
        for name, path in _files_for(arg):
            for number, target in broken_links(path):
                print(f"{name}:{number}: {target}")
                status = 1
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
