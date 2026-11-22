"""Parse unified diffs and apply them to source text."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    old_start: int
    old_len: int
    lines: list[str] = field(default_factory=list)  # each starts with ' ', '-' or '+'

    def removed_lines(self) -> list[int]:
        """Old line numbers of '-' lines."""
        out, cursor = [], self.old_start
        for line in self.lines:
            if line.startswith("-"):
                out.append(cursor)
                cursor += 1
            elif line.startswith(" "):
                cursor += 1
        return out

    def insertion_points(self) -> list[int]:
        """Old line numbers before which '+' lines are inserted (len(old)+1 means at the end)."""
        out, cursor = [], self.old_start if self.old_len else self.old_start + 1
        for line in self.lines:
            if line.startswith("+"):
                out.append(cursor)
            else:
                cursor += 1
        return sorted(set(out))

    def new_text(self) -> list[str]:
        return [line[1:] for line in self.lines if line[:1] in (" ", "+")]

    def changed_text(self) -> list[str]:
        return [line[1:] for line in self.lines if line[:1] in ("-", "+")]


@dataclass
class FileDiff:
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False

    @property
    def path(self) -> str:
        return self.old_path or self.new_path or ""


def _strip_prefix(raw: str) -> str | None:
    raw = raw.split("\t", 1)[0].strip()
    if raw == "/dev/null":
        return None
    if raw.startswith(("a/", "b/")):
        return raw[2:]
    return raw


def parse_patch(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: Hunk | None = None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            parts = line[len("diff --git "):].split(" b/", 1)
            old = parts[0][2:] if parts[0].startswith("a/") else parts[0]
            new = parts[1] if len(parts) > 1 else old
            current = FileDiff(old_path=old, new_path=new)
            files.append(current)
            hunk = None
        elif current is None:
            continue
        elif line.startswith("new file mode"):
            current.old_path = None
        elif line.startswith("deleted file mode"):
            current.new_path = None
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            current.binary = True
        elif hunk is None and line.startswith("--- "):
            current.old_path = _strip_prefix(line[4:])
        elif hunk is None and line.startswith("+++ "):
            current.new_path = _strip_prefix(line[4:])
        elif line.startswith("@@"):
            match = HUNK_RE.match(line)
            if match:
                old_len = int(match.group(2)) if match.group(2) is not None else 1
                hunk = Hunk(old_start=int(match.group(1)), old_len=old_len)
                current.hunks.append(hunk)
        elif hunk is not None and line[:1] in (" ", "-", "+"):
            hunk.lines.append(line)
        elif hunk is not None and line == "":
            hunk.lines.append(" ")  # a context line whose single space was stripped
    return files


def apply_hunks(old_lines: list[str], hunks: list[Hunk]) -> list[str]:
    """Apply hunks (in old coordinates) to old_lines (no line endings). Returns new lines."""
    new = list(old_lines)
    for hunk in sorted(hunks, key=lambda h: h.old_start, reverse=True):
        start = hunk.old_start - 1 if hunk.old_len else hunk.old_start
        new[start:start + hunk.old_len] = hunk.new_text()
    return new
