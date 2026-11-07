"""Bounded context, output checks and conservative recovery for overview pages."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
import re
import tempfile

from codewiki.src.be.prompt_template import MAX_USER_PROMPT_CHARS
from codewiki.src.be.utils import count_tokens, enc

logger = logging.getLogger(__name__)

OVERVIEW_INPUT_TOKENS = 32_000
PREVIOUS_DRAFT_TOKENS = 8_000
_TRUNCATED = "\n[Excerpt truncated; base the overview on the supplied content.]"
_PATH = re.compile(r"(?<!\S)(?:/|\./|\.\./|[A-Za-z]:[\\/])\S+\.md", re.IGNORECASE)
_RELATIVE_PATH = re.compile(r"[\w.\\/-]+\.md\b", re.IGNORECASE)
_PREAMBLE = re.compile(
    r"\AI['’]ll start by reading\b[^\n]*?(?:documentation files|files)\.[ \t]*(?:\n|$)",
    re.IGNORECASE,
)


def incomplete_overview_reason(content: str) -> str | None:
    """Recognize only empty pages and the specific preamble/path-only failure.

    Existing user prose is deliberately not subjected to new-output validation.
    """
    if not content.strip():
        return "empty page"
    match = _PREAMBLE.match(content.strip())
    if match:
        rest = content.strip()[match.end() :]
        if not _PATH.sub("", rest).strip():
            return "unfinished reading preamble with no documentation"
    return None


def reusable_overview(path: str) -> bool:
    if not os.path.isfile(path):
        return False
    content = Path(path).read_text(encoding="utf-8")
    reason = incomplete_overview_reason(content)
    if reason:
        logger.warning("Regenerating incomplete documentation at %s: %s", path, reason)
    return reason is None


def root_recovery_prompt(prompt: str, docs_dir: str, module_name: str) -> str:
    paths = [Path(docs_dir, name) for name in (f"{module_name}.md", "overview.md")]
    if any(
        path.is_file() and incomplete_overview_reason(path.read_text(encoding="utf-8"))
        for path in paths
    ):
        return prompt + (
            f"\n\nAn existing root page is incomplete. Write the finished page to {module_name}.md. "
            "If that file already exists, replace its incomplete contents with str_replace_editor "
            "instead of using create. Do not treat the incomplete overview.md as completed work."
        )
    return prompt


@contextmanager
def preserve_incomplete_overviews(paths: list[str]):
    """Keep old root pages available if an editing agent fails during recovery."""
    originals = {}
    for path in paths:
        if os.path.isfile(path):
            content = Path(path).read_text(encoding="utf-8")
            if incomplete_overview_reason(content):
                originals[path] = content
    try:
        yield
    except BaseException:
        for path, content in originals.items():
            save_overview(content, path)
        raise


def overview_content(response: str | None) -> str:
    text = (response or "").strip()
    if "<OVERVIEW>" in text and "</OVERVIEW>" in text:
        text = text.split("<OVERVIEW>", 1)[1].split("</OVERVIEW>", 1)[0].strip()
    return text


def overview_error(content: str) -> str | None:
    reason = incomplete_overview_reason(content)
    if reason:
        return reason
    heading = False
    body_lines = []
    fence = None
    for line in content.splitlines():
        stripped = line.strip()
        marker = re.match(r"^(`{3,}|~{3,})", stripped)
        if marker:
            run = marker.group(1)
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence):
                fence = None
            continue
        if fence:
            continue
        if re.match(r"^#{1,6}\s+\S", stripped):
            heading = True
        else:
            body_lines.append(stripped)
    body = "\n".join(body_lines).strip()
    if incomplete_overview_reason(body):
        return "unfinished or empty explanatory body"
    prose = _RELATIVE_PATH.sub("", _PATH.sub("", body))
    if not heading or not any(c.isalpha() for c in prose):
        return "expected a Markdown heading and explanatory body outside code fences and file paths"
    return None


def _clip(text: str, tokens: int) -> str:
    encoded = enc.encode(text, disallowed_special=())
    if len(encoded) <= tokens:
        return text
    # Avoid decoding a partial UTF-8 character at the excerpt boundary.
    prefix = enc.decode_bytes(encoded[: max(0, tokens)]).decode("utf-8", errors="ignore")
    return prefix + _TRUNCATED


_SUMMARY = re.compile(r"<!--\s*codewiki:summary\s*(.*?)-->", re.DOTALL)
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING = re.compile(r"^#{1,6}\s+\S")


def page_summary(text: str) -> str | None:
    """The summary a page wrote for its parent (PAGE_SUMMARY_GUIDANCE), if any."""
    match = _SUMMARY.search(text)
    return match.group(1).strip() or None if match else None


def condensed_child(text: str) -> str:
    """A child page cut down to what an overview needs, without cutting it off:
    its own summary when it has one, then every heading with its first paragraph."""
    lines: list[str] = []
    fence = None
    take = got = False  # in the paragraph after a heading / took a line of it
    for line in _SUMMARY.sub("", text).splitlines():
        marker = _FENCE.match(line)
        if fence is not None or marker:
            # Diagrams and code are left to the child page.
            if fence is None:
                fence = marker.group(1)
            elif marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = None
            continue
        if _HEADING.match(line.strip()):
            lines += ["", line.strip()]
            take, got = True, False
        elif not line.strip():
            take = take and not got
        elif take:
            lines.append(line.rstrip())
            got = True
    outline = "\n".join(lines).strip()
    summary = page_summary(text)
    return f"Summary: {summary}\n\n{outline}" if summary else outline


def bounded_overview_prompt(
    base: str,
    children: dict[str, str],
    system: str | None,
    *,
    correction: str = "",
    previous: str = "",
    budget_tokens: int = 0,
) -> str:
    """Fairly distribute the remaining input budget over immediate child docs.

    ``budget_tokens`` comes from the model's context window (0 = unknown, use
    OVERVIEW_INPUT_TOKENS). Children that do not fit are condensed to their
    summary and outline, largest first, before any is clipped.
    """
    limit = budget_tokens or OVERVIEW_INPUT_TOKENS
    suffix = ""
    if correction:
        suffix = (
            "\n\nCorrect this previous draft and return the complete Markdown overview. "
            "Preserve useful content. Validation failure: "
            + correction
            + "\n<PREVIOUS_DRAFT>\n"
            + _clip(previous, PREVIOUS_DRAFT_TOKENS)
            + "\n</PREVIOUS_DRAFT>"
        )
    labels = {name: f"\n<CHILD_DOCUMENTATION name={name!r}>\n" for name in children}

    def render(allocations: dict[str, int]) -> str:
        return (
            base
            + "".join(
                labels[name] + _clip(text, allocations[name]) + "\n</CHILD_DOCUMENTATION>"
                for name, text in children.items()
            )
            + suffix
        )

    sizes = {name: count_tokens(text) for name, text in children.items()}
    # Empty excerpts still reserve labels and omission markers.
    allocations = dict.fromkeys(children, 0)
    mandatory = render(allocations)
    remaining = limit - count_tokens((system or "") + mandatory) - 64
    if remaining < 0 or len((system or "") + mandatory) > MAX_USER_PROMPT_CHARS:
        raise RuntimeError("Mandatory overview context exceeds the input budget")
    for name in sorted(children, key=sizes.get, reverse=True):
        if sum(sizes.values()) <= remaining:
            break
        condensed = condensed_child(children[name])
        # A page without headings or summary has no outline; it is clipped instead.
        if condensed.strip() and len(condensed) < len(children[name]):
            children = {**children, name: condensed}  # same order
            sizes[name] = count_tokens(condensed)
    pending = set(children)
    while pending and remaining:
        share = max(1, remaining // len(pending))
        for name in children:  # stable ordering, including when the budget is very small
            if name not in pending:
                continue
            take = min(share, sizes[name] - allocations[name], remaining)
            allocations[name] += take
            remaining -= take
            if allocations[name] == sizes[name]:
                pending.remove(name)
    prompt = render(allocations)
    # Token concatenation and long whitespace tokens can affect either cap.
    while (
        count_tokens((system or "") + prompt) > limit
        or len((system or "") + prompt) > MAX_USER_PROMPT_CHARS
    ):
        if not any(allocations.values()):
            raise RuntimeError("Mandatory overview context exceeds the input budget")
        allocations = {name: value // 2 for name, value in allocations.items()}
        prompt = render(allocations)
    return prompt


def save_overview(content: str, path: str) -> None:
    """Replace a page only after validation, leaving the old file on failure."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent, delete=False
        ) as stream:
            temporary = stream.name
            stream.write(content)
        mode = destination.stat().st_mode & 0o777 if destination.exists() else 0o644
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
