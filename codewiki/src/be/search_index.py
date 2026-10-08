"""Build a per-heading section index for client-side search in the GitHub Pages viewer.

Splits every generated ``.md`` file into one record per heading (any level,
H1-H6). A section's ``content`` is only the text strictly between it and the
next heading of *any* level — sections do not nest, so a viewer-side BM25
scorer never scores a parent section's terms twice.

Anchors are GitHub slugs of the heading's *rendered* text (links, code
spans and emphasis stripped), which is what ``marked-gfm-heading-id`` slugs
in the browser, so a result always scrolls to an element that exists.

``file`` is the page's docs-relative path (``auth/login.md`` in the
hierarchical layout), the same path the viewer's router and ``DOC_PATHS``
use.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from codewiki.src.be.crosslinker import _HEADING_RE, _iter_lines, _plain, github_slug
from codewiki.src.be.doc_layout import list_doc_files

_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\[\]]*)\]\([^)]*\)")
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_|~~)")
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>\s?")
_TABLE_PIPE_RE = re.compile(r"\|")
_WHITESPACE_RE = re.compile(r"\s+")
# Table delimiter rows (| --- | :--: |) and thematic breaks carry no text
_RULE_LINE_RE = re.compile(r"^[\s|:\-*_=]*$")


@dataclass
class SectionRecord:
    file: str
    anchor: str
    level: int
    title: str
    content: str

    @property
    def id(self) -> str:
        return f"{self.file}#{self.anchor}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "file": self.file,
            "anchor": self.anchor,
            "level": self.level,
            "title": self.title,
            "content": self.content,
        }


def _strip_markdown_line(line: str) -> str:
    """Reduce one markdown body line to plain, search-friendly text."""
    line = _BLOCKQUOTE_RE.sub("", line)
    line = _LIST_MARKER_RE.sub("", line)
    line = _IMAGE_RE.sub(r"\1", line)
    line = _LINK_RE.sub(r"\1", line)
    line = _INLINE_CODE_RE.sub(r"\1", line)
    line = _HTML_TAG_RE.sub(" ", line)
    line = _EMPHASIS_RE.sub("", line)
    line = _TABLE_PIPE_RE.sub(" ", line)
    return line.strip()


def parse_sections(text: str) -> list[SectionRecord]:
    """Split ``text`` into one record per heading (any level, H1-H6).

    ``file`` is left blank on the returned records; callers (``build_search_index``)
    fill it in per source file.
    """
    lines = text.split("\n")

    headings: list[tuple[int, int, str, str]] = []  # (line_index, level, title, slug)
    in_code_by_line: dict[int, bool] = {}
    seen: Counter[str] = Counter()
    for idx, line, in_code in _iter_lines(text):
        in_code_by_line[idx] = in_code
        if in_code:
            continue
        m = _HEADING_RE.match(line)
        if not m:
            continue
        level = len(m.group(1))
        raw = m.group(2)
        title = _plain(raw).strip()
        base = github_slug(title)
        slug = base if not seen[base] else f"{base}-{seen[base]}"
        seen[base] += 1
        headings.append((idx, level, title, slug))

    if not headings:
        return []

    records: list[SectionRecord] = []
    for i, (start_idx, level, title, slug) in enumerate(headings):
        end_idx = headings[i + 1][0] if i + 1 < len(headings) else len(lines)
        body_lines: list[str] = []
        for line_idx in range(start_idx + 1, end_idx):
            if in_code_by_line.get(line_idx):
                continue  # drop fenced code from the indexed text
            if _RULE_LINE_RE.match(lines[line_idx]):
                continue
            stripped = _strip_markdown_line(lines[line_idx])
            if stripped:
                body_lines.append(stripped)
        content = _WHITESPACE_RE.sub(" ", " ".join(body_lines)).strip()
        records.append(
            SectionRecord(file="", anchor=slug, level=level, title=title, content=content)
        )

    return records


def build_search_index(working_dir: str) -> list[dict[str, Any]]:
    """Parse every page in ``working_dir`` (either layout) into a flat list of section records.

    Best-effort per file: a single unreadable/malformed file is skipped
    rather than failing the whole index (this pass never blocks index.html
    itself — see ``HTMLGenerator._write_search_index``).
    """
    index: list[dict[str, Any]] = []
    for rel in sorted(set(list_doc_files(working_dir).values())):
        try:
            with open(os.path.join(working_dir, rel), encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        for record in parse_sections(text):
            record.file = rel
            index.append(record.to_dict())
    return index


def write_search_index(working_dir: str, output_path: str) -> list[dict[str, Any]]:
    """Build the index and write it as JSON to ``output_path``. Returns the index."""
    index = build_search_index(working_dir)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)
    return index
