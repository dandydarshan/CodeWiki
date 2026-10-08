"""Deterministic post-generation cross-linking for the generated wiki.

The LLM decides how many links a page gets, and weaker instruction followers
(or the tool-less overview path) leave module and component names as plain
text. This pass runs after all pages exist and, without any LLM call:

1. repairs existing ``.md`` links (absolute/nested paths, name variants,
   stale ``#anchors``) and unwraps links to pages that do not exist;
2. links mentions of module names, page filenames and component names to the
   page (and, for components, the section) that documents them — first
   mention per ``##`` section, wiki style, never inside code blocks,
   headings or existing links;
3. appends a managed "Related pages" block listing structural relations
   (parent, sub-modules, dependency neighbours, or every top-level module
   for the overview) that the page still does not link.

Every step is idempotent: re-running on its own output changes nothing.
Anchors use GitHub heading slugs, which the bundled viewer
(``marked-gfm-heading-id``) and GitHub both generate.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

from codewiki.src.be.doc_layout import list_doc_files, relative_link
from codewiki.src.be.module_naming import resolve_module_doc_path
from codewiki.src.config import OVERVIEW_FILENAME

logger = logging.getLogger(__name__)

OVERVIEW_STEM = os.path.splitext(OVERVIEW_FILENAME)[0]
RELATED_START = "<!-- codewiki:related:start -->"
RELATED_END = "<!-- codewiki:related:end -->"
MAX_DEPENDENCY_LINKS = 5

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
_REF_DEF_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s")
_RELATED_BLOCK_RE = re.compile(
    r"\n*" + re.escape(RELATED_START) + r".*?" + re.escape(RELATED_END) + r"\n?", re.DOTALL
)
_LINK_RE = re.compile(
    r"(?P<img>!?)\[(?P<text>(?:[^\[\]]|\[[^\[\]]*\])*)\]"
    r"\((?P<href><[^>\n]*>|[^)\s]*)(?P<title>\s+\"[^\"]*\")?\)"
)
# Spans the inline linker must not touch. Order matters only for spans that
# start at the same offset.
_PROTECTED_RE = re.compile(
    r"(?P<code>(?P<ticks>`+)(?P<body>.+?)(?P=ticks))"
    r"|(?P<link>!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]\((?:<[^>\n]*>|[^)\s]*)(?:\s+\"[^\"]*\")?\))"
    r"|(?P<ref>\[[^\[\]]*\](?:\[[^\]]*\])?)"
    r"|(?P<comment><!--.*?-->)"
    r"|(?P<auto><(?:https?:|mailto:)[^>]+>)"
    r"|(?P<html></?[A-Za-z][^>]*>)"
    r"|(?P<url>https?://[^\s)>\]]+)"
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_][\w\-]*(?:\.[\w\-]+)*")
# Component names that read as identifiers rather than English words:
# camelCase / PascalCase humps, snake_case, or dotted names.
_DISTINCTIVE_RE = re.compile(r"[a-z0-9][A-Z]|[A-Za-z0-9]_[A-Za-z0-9]|\w\.\w")


@dataclass
class CrossLinkReport:
    pages_changed: list[str] = field(default_factory=list)
    links_added: int = 0
    links_repaired: int = 0
    links_removed: int = 0
    related_added: int = 0

    def summary(self) -> str:
        return (
            f"{len(self.pages_changed)} page(s) changed: +{self.links_added} inline links, "
            f"{self.links_repaired} repaired, {self.links_removed} dangling removed, "
            f"+{self.related_added} related-page links"
        )


@dataclass(frozen=True)
class _Target:
    page: str
    anchor: str | None = None
    kind: str = "module"  # "module" | "component"


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------


def github_slug(text: str) -> str:
    """GitHub / github-slugger heading slug (without duplicate suffixing)."""
    text = re.sub(r"<[!/a-zA-Z][^>]*>", "", text).strip().lower()
    out = []
    for ch in text:
        if ch == " ":
            out.append("-")
        elif ch in "_-" or ch.isalnum():
            out.append(ch)
        elif ord(ch) > 127 and unicodedata.category(ch)[0] in "LMN":
            out.append(ch)
    return "".join(out)


def _iter_lines(text: str):
    """Yield ``(index, line, in_code)`` with fenced code blocks flagged."""
    fence: str | None = None
    for i, line in enumerate(text.split("\n")):
        m = _FENCE_RE.match(line)
        if fence is None:
            if m:
                fence = m.group(1)
                yield i, line, True
                continue
            yield i, line, False
        else:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                if not line.strip()[len(m.group(1)) :].strip():
                    fence = None
            yield i, line, True


def parse_headings(text: str) -> list[tuple[int, str, str]]:
    """Return ``(level, raw_text, slug)`` for each ATX heading outside code."""
    headings = []
    seen: Counter[str] = Counter()
    for _, line, in_code in _iter_lines(text):
        if in_code:
            continue
        m = _HEADING_RE.match(line)
        if not m:
            continue
        raw = m.group(2)
        base = github_slug(raw)
        slug = base if not seen[base] else f"{base}-{seen[base]}"
        seen[base] += 1
        headings.append((len(m.group(1)), raw, slug))
    return headings


def _plain(heading_text: str) -> str:
    return _LINK_RE.sub(lambda m: m.group("text"), heading_text).replace("`", "").replace("*", "")


def _display_name(module_name: str) -> str:
    return module_name.replace("_", " ")


# ---------------------------------------------------------------------------
# Tree helpers
# ---------------------------------------------------------------------------


def _walk(tree: dict[str, Any], parent: tuple[str, ...] = ()):
    for name, info in tree.items():
        if not isinstance(info, dict):
            continue
        path = parent + (name,)
        yield path, info
        children = info.get("children")
        if isinstance(children, dict) and children:
            yield from _walk(children, path)


# ---------------------------------------------------------------------------
# Cross-linker
# ---------------------------------------------------------------------------


class CrossLinker:
    def __init__(
        self,
        working_dir: str,
        module_tree: dict[str, Any],
        components: dict[str, Any] | None = None,
    ):
        self.working_dir = working_dir
        self.module_tree = module_tree or {}
        self.components = components or {}
        # page stem -> docs-relative path (nested in the hierarchical layout)
        self.page_rel = list_doc_files(working_dir, self.module_tree)
        self.docs: dict[str, str] = {}
        for stem, rel in sorted(self.page_rel.items()):
            with open(os.path.join(working_dir, rel), encoding="utf-8") as f:
                self.docs[stem] = f.read()
        self._lower_stems = {s.lower(): s for s in self.docs}

        # module name -> page stem, plus tree relations
        self.module_page: dict[str, str] = {}
        self.module_path: dict[str, tuple[str, ...]] = {}
        for path, _ in _walk(self.module_tree):
            name = path[-1]
            self.module_path[name] = path
            if name in self.docs:
                self.module_page[name] = name
                continue
            resolved = resolve_module_doc_path(working_dir, name, self.module_tree)
            if resolved is not None:
                stem = os.path.splitext(os.path.basename(resolved))[0]
                if stem in self.docs:
                    self.module_page[name] = stem

        self.component_owner = self._component_owners()
        self.slugs: dict[str, list[tuple[int, str, str]]] = {}
        self._refresh_slugs()

    # -- setup ------------------------------------------------------------

    def _component_owners(self) -> dict[str, str]:
        """Component id -> deepest module that lists it (parents repeat children's ids)."""
        owner: dict[str, tuple[int, str]] = {}
        for path, info in _walk(self.module_tree):
            for cid in info.get("components") or []:
                if cid not in owner or len(path) > owner[cid][0]:
                    owner[cid] = (len(path), path[-1])
        return {cid: name for cid, (_, name) in owner.items()}

    def _refresh_slugs(self) -> None:
        self.slugs = {stem: parse_headings(text) for stem, text in self.docs.items()}

    def _href(self, from_stem: str, to_stem: str, anchor: str | None = None) -> str:
        """Link from page ``from_stem`` to ``to_stem`` (relative path, optional anchor)."""
        base = (
            ""
            if to_stem == from_stem
            else relative_link(self.page_rel[from_stem], self.page_rel[to_stem])
        )
        return base + (f"#{anchor}" if anchor else "")

    def _slug_set(self, stem: str) -> set[str]:
        return {slug for _, _, slug in self.slugs.get(stem, [])}

    def _resolve_stem(self, name: str) -> str | None:
        if name in self.docs:
            return name
        if name in self.module_page:
            return self.module_page[name]
        for variant in (
            name,
            name.replace(" ", "_"),
            name.replace("-", "_"),
            name.replace(" ", "-"),
        ):
            if variant.lower() in self._lower_stems:
                return self._lower_stems[variant.lower()]
        return None

    def _component_anchor(self, stem: str, name: str) -> str | None:
        """Slug of the tightest non-title heading in ``stem`` naming ``name``."""
        pattern = re.compile(r"(?<![\w.])" + re.escape(name) + r"(?![\w])")
        best: tuple[int, str] | None = None
        for level, raw, slug in self.slugs.get(stem, []):
            if level == 1:
                continue
            plain = _plain(raw)
            if pattern.search(plain):
                score = len(plain)
                if best is None or score < best[0]:
                    best = (score, slug)
        return best[1] if best else None

    def _build_terms(self) -> tuple[dict[str, _Target], dict[str, _Target]]:
        """Return ``(prose_terms, code_terms)``: term -> target.

        Prose terms are linked anywhere in running text; code terms only when
        an inline code span consists of exactly that term.
        """
        prose: dict[str, _Target] = {}
        code: dict[str, _Target] = {}

        # Components (lowest priority, so module names override them).
        by_name: dict[str, set[str]] = defaultdict(set)
        for cid, module in self.component_owner.items():
            node = self.components.get(cid)
            if node is not None and getattr(node, "component_type", None) == "artifact":
                if getattr(node, "node_type", None) != "artifact_file":
                    continue  # build targets / config keys are too generic to link
            page = self.module_page.get(module)
            if page is None:
                continue
            name = cid.split("::", 1)[1] if "::" in cid else cid
            code[cid] = _Target(page, self._component_anchor(page, name), "component")
            if len(name) >= 3 and not name.startswith("__"):
                by_name[name].add(page)
        for name, pages in by_name.items():
            if len(pages) != 1:
                continue  # same name documented on several pages: ambiguous
            page = next(iter(pages))
            target = _Target(page, self._component_anchor(page, name), "component")
            code[name] = target
            if len(name) >= 4 and _DISTINCTIVE_RE.search(name):
                prose[name] = target

        # Module names and page filenames.
        for module, page in self.module_page.items():
            for term in {module, _display_name(module)}:
                if len(term) >= 3:
                    prose[term] = code[term] = _Target(page)
        for stem in self.docs:
            prose[f"{stem}.md"] = code[f"{stem}.md"] = _Target(stem)
        return prose, code

    # -- pass 1: repair existing links -------------------------------------

    def _repair_links(self, stem: str, text: str, report: CrossLinkReport) -> str:
        own_slugs = self._slug_set(stem)

        def fix(m: re.Match) -> str:
            href = m.group("href")
            if m.group("img") or "://" in href or href.startswith(("mailto:", "<http")):
                return m.group(0)
            href = href[1:-1] if href.startswith("<") else href
            path, _, frag = href.partition("#")
            frag = unquote(frag)
            if not path:
                if not frag or frag in own_slugs:
                    return m.group(0)
                if github_slug(frag) in own_slugs:
                    report.links_repaired += 1
                    return f"[{m.group('text')}](#{github_slug(frag)})"
                report.links_removed += 1
                return m.group("text")
            decoded = unquote(path).replace("\\", "/")
            if not decoded.lower().endswith(".md"):
                return m.group(0)  # source files, images, external docs
            target = self._resolve_stem(os.path.basename(decoded)[:-3])
            if target is None:
                report.links_removed += 1
                return m.group("text")
            slugs = self._slug_set(target)
            new_frag = frag
            if frag and frag not in slugs:
                new_frag = github_slug(frag) if github_slug(frag) in slugs else ""
            target_href = relative_link(self.page_rel[stem], self.page_rel[target])
            if decoded == target_href and new_frag == frag:
                return m.group(0)
            report.links_repaired += 1
            new_href = target_href + (f"#{new_frag}" if new_frag else "")
            return f"[{m.group('text')}]({new_href}{m.group('title') or ''})"

        out = []
        for _, line, in_code in _iter_lines(text):
            out.append(line if in_code else _LINK_RE.sub(fix, line))
        return "\n".join(out)

    # -- pass 2: inline wiki links -----------------------------------------

    def _link_inline(
        self,
        stem: str,
        text: str,
        prose: dict[str, _Target],
        code: dict[str, _Target],
        report: CrossLinkReport,
    ) -> str:
        first_token: dict[str, list[str]] = defaultdict(list)
        for term in prose:
            m = _TOKEN_RE.match(term)
            if m:
                first_token[m.group(0)].append(term)
        for terms in first_token.values():
            terms.sort(key=len, reverse=True)

        lines = text.split("\n")
        flags = [in_code for _, _, in_code in _iter_lines(text)]

        # Split into ``#``/``##`` sections and note which section each heading
        # (by slug) falls in, so a component is not linked from its own section.
        sections: list[tuple[int, int]] = []
        section_of_heading: dict[str, int] = {}
        slug_iter = iter(slug for _, _, slug in parse_headings(text))
        start = 0
        for i, line in enumerate(lines):
            hm = None if flags[i] else _HEADING_RE.match(line)
            if not hm:
                continue
            if len(hm.group(1)) <= 2 and i > start:
                sections.append((start, i))
                start = i
            slug = next(slug_iter, None)
            if slug is not None:
                section_of_heading[slug] = len(sections)
        sections.append((start, len(lines)))

        def href_for(target: _Target) -> str:
            return self._href(stem, target.page, target.anchor)

        for idx, (s, e) in enumerate(sections):
            linked: set[str] = set()
            for i in range(s, e):
                if flags[i]:
                    continue
                for m in _LINK_RE.finditer(lines[i]):
                    href = m.group("href").strip("<>")
                    path, _, _ = href.partition("#")
                    linked.add(href)
                    if path:
                        linked.add(os.path.basename(unquote(path)))

            def allowed(target: _Target) -> str | None:
                if target.page == stem:
                    if target.kind == "module" or not target.anchor:
                        return None  # never link a page to itself
                    if section_of_heading.get(target.anchor) == idx:
                        return None  # already in the section that documents it
                href = href_for(target)
                page_file = f"{target.page}.md"
                if href in linked or (target.kind == "module" and page_file in linked):
                    return None
                linked.add(href)
                if target.kind == "module":
                    linked.add(page_file)
                return href

            def link_text(segment: str) -> str:
                out, pos = [], 0
                for tm in _TOKEN_RE.finditer(segment):
                    if tm.start() < pos:
                        continue
                    before = segment[tm.start() - 1] if tm.start() else ""
                    if before and (before.isalnum() or before in "_/.-#"):
                        continue
                    for term in first_token.get(tm.group(0), ()):
                        if not segment.startswith(term, tm.start()):
                            continue
                        end = tm.start() + len(term)
                        after = segment[end : end + 2]
                        if after[:1] and (after[0].isalnum() or after[0] in "_-"):
                            continue
                        if after[:1] == "." and len(after) > 1 and after[1].isalnum():
                            continue
                        href = allowed(prose[term])
                        if href is None:
                            break
                        out.append(segment[pos : tm.start()])
                        out.append(f"[{term}]({href})")
                        report.links_added += 1
                        pos = end
                        break
                out.append(segment[pos:])
                return "".join(out)

            for i in range(s, e):
                line = lines[i]
                if flags[i] or _HEADING_RE.match(line) or _REF_DEF_RE.match(line):
                    continue
                if line.lstrip().startswith("<"):
                    continue  # raw HTML block
                out, pos = [], 0
                for pm in _PROTECTED_RE.finditer(line):
                    out.append(link_text(line[pos : pm.start()]))
                    piece = pm.group(0)
                    if pm.group("code") is not None:
                        body = pm.group("body").strip()
                        key = body[:-2] if body.endswith("()") else body
                        target = code.get(key)
                        href = allowed(target) if target else None
                        if href is not None:
                            piece = f"[{piece}]({href})"
                            report.links_added += 1
                    out.append(piece)
                    pos = pm.end()
                out.append(link_text(line[pos:]))
                lines[i] = "".join(out)
        return "\n".join(lines)

    # -- pass 3: related pages ---------------------------------------------

    def _dependency_neighbours(self) -> tuple[dict[str, Counter], dict[str, Counter]]:
        uses: dict[str, Counter] = defaultdict(Counter)
        used_by: dict[str, Counter] = defaultdict(Counter)
        for cid, module in self.component_owner.items():
            node = self.components.get(cid)
            for dep in getattr(node, "depends_on", None) or ():
                other = self.component_owner.get(dep)
                if other and other != module:
                    uses[module][other] += 1
                    used_by[other][module] += 1
        return uses, used_by

    def _related(self, module: str | None, uses, used_by) -> list[tuple[str, list[str]]]:
        if module is None:
            return [("Modules", [name for name in self.module_tree if name in self.module_page])]
        path = self.module_path[module]
        info = self.module_tree
        for part in path[:-1]:
            info = info[part].get("children", {})
        children = list((info[module].get("children") or {}).keys())

        def unrelated_to_tree(other: str) -> bool:
            other_path = self.module_path.get(other, ())
            return other_path[: len(path)] != path and path[: len(other_path)] != other_path

        def top(counter: Counter) -> list[str]:
            ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
            return [m for m, _ in ranked if unrelated_to_tree(m)][:MAX_DEPENDENCY_LINKS]

        groups = [
            ("Parent", [path[-2]] if len(path) > 1 else []),
            ("Sub-modules", children),
            ("Depends on", top(uses.get(module, Counter()))),
            ("Used by", top(used_by.get(module, Counter()))),
        ]
        return [(label, [m for m in mods if m in self.module_page]) for label, mods in groups]

    def _append_related(self, stem, body, module, uses, used_by) -> tuple[str, int]:
        """Append the managed block for relations ``body`` does not link yet."""
        if module is None and stem != OVERVIEW_STEM:
            return body, 0
        present = set()
        for m in _LINK_RE.finditer(body):
            path = unquote(m.group("href").strip("<>").partition("#")[0])
            if path:
                present.add(os.path.basename(path)[:-3] if path.endswith(".md") else path)
        lines_out, added = [], 0
        for label, modules in self._related(module, uses, used_by):
            missing = [
                m
                for m in modules
                if self.module_page[m] not in present and self.module_page[m] != stem
            ]
            if missing:
                links = ", ".join(
                    f"[{_display_name(m)}]({self._href(stem, self.module_page[m])})"
                    for m in missing
                )
                lines_out.append(f"- **{label}:** {links}")
                added += len(missing)
        if not lines_out:
            return body, 0
        block = "\n".join([RELATED_START, "## Related pages", "", *lines_out, RELATED_END])
        return body.rstrip("\n") + "\n\n" + block + "\n", added

    # -- driver ------------------------------------------------------------

    def run(self) -> CrossLinkReport:
        report = CrossLinkReport()
        original = dict(self.docs)

        for stem in self.docs:
            self.docs[stem] = self._repair_links(stem, self.docs[stem], report)
        self._refresh_slugs()

        prose, code = self._build_terms()
        uses, used_by = self._dependency_neighbours()
        page_module = {page: module for module, page in self.module_page.items()}
        for stem, text in self.docs.items():
            body = _RELATED_BLOCK_RE.sub("", text)
            had_block = body != text
            body = self._link_inline(stem, body, prose, code, report)
            if had_block and not body.endswith("\n"):
                body += "\n"
            self.docs[stem], added = self._append_related(
                stem, body, page_module.get(stem), uses, used_by
            )
            old_block = _RELATED_BLOCK_RE.search(original[stem])
            new_block = _RELATED_BLOCK_RE.search(self.docs[stem])
            if (old_block and old_block.group(0).strip()) != (
                new_block and new_block.group(0).strip()
            ):
                report.related_added += added

        for stem, text in self.docs.items():
            if text != original[stem]:
                path = os.path.join(self.working_dir, self.page_rel[stem])
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                report.pages_changed.append(stem)
        return report


def crosslink_docs(
    working_dir: str,
    module_tree: dict[str, Any],
    components: dict[str, Any] | None = None,
) -> CrossLinkReport:
    """Repair, add and complete cross-links across every page in ``working_dir``."""
    return CrossLinker(working_dir, module_tree, components).run()
