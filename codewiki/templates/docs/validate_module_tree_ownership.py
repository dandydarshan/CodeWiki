#!/usr/bin/env python3
"""
Validate component ownership in CodeWiki module_tree.json.

Placed next to module_tree.json when the GitHub Pages viewer is generated
(codewiki CLI html step). You can also copy this file from
codewiki/templates/docs/ into your docs output folder, then run:

  python validate_module_tree_ownership.py

Writes: validation_results/module_tree_ownership.md

Exit code 0 = no duplicate ownership; 1 = issues found; 2 = missing/invalid input.

Rule: each component ID may appear under exactly one module node. The first
top-level module in JSON key order establishes canonical ownership for its whole
subtree (e.g. module_a -> sub_module_a1 owns node_1, node_2). A later top-level
module (module_b) must not assign those IDs again — even mixed with new IDs in
one sub-module (e.g. node_1, node_2, node_7, node_8). Link to the existing .md
instead of re-documenting.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MODULE_TREE_FILENAME = "module_tree.json"
REPORT_DIRNAME = "validation_results"
REPORT_FILENAME = "module_tree_ownership.md"

DISPLAY_SEGMENT_SEP = "\\\\"


def script_docs_dir() -> Path:
    """Directory containing this script (= CodeWiki docs output dir)."""
    return Path(__file__).resolve().parent


def walk_tree(
    tree: dict[str, Any],
    path_prefix: list[str] | None = None,
) -> list[tuple[list[str], str, list[str]]]:
    """Yield (path, module_name, component_ids) for every module node."""
    path_prefix = path_prefix or []
    out: list[tuple[list[str], str, list[str]]] = []
    for name, info in tree.items():
        if not isinstance(info, dict):
            continue
        current = path_prefix + [name]
        comps = info.get("components") or []
        if not isinstance(comps, list):
            comps = []
        out.append((current, name, [c for c in comps if isinstance(c, str)]))
        children = info.get("children") or {}
        if isinstance(children, dict) and children:
            out.extend(walk_tree(children, current))
    return out


def path_str(path: list[str]) -> str:
    """Format module paths for markdown output."""
    return DISPLAY_SEGMENT_SEP.join(path)


def _path_segments(path: str) -> list[str]:
    normalized = path.replace("\\", "/")
    return [part for part in normalized.split("/") if part]


def _file_path_for_display(file_part: str) -> str:
    return DISPLAY_SEGMENT_SEP.join(_path_segments(file_part))


def component_id_str(component_id: str) -> str:
    if "::" in component_id:
        file_part, _, symbol_part = component_id.partition("::")
        return f"{_file_path_for_display(file_part)}::{symbol_part}"
    return _file_path_for_display(component_id)


def doc_stem_from_path(path: list[str]) -> str:
    return path[-1] if path else "unknown"


def top_level_module(path: list[str]) -> str:
    return path[0] if path else ""


def find_global_duplicates(
    nodes: list[tuple[list[str], str, list[str]]],
) -> dict[str, list[list[str]]]:
    claims: dict[str, list[list[str]]] = defaultdict(list)
    for path, _name, comps in nodes:
        for cid in comps:
            claims[cid].append(path)
    return {cid: paths for cid, paths in claims.items() if len(paths) > 1}


def build_first_owner_registry(
    nodes: list[tuple[list[str], str, list[str]]],
) -> dict[str, list[str]]:
    owner: dict[str, list[str]] = {}
    for path, _name, comps in nodes:
        for cid in comps:
            if cid not in owner:
                owner[cid] = path
    return owner


def analyze_node_against_registry(
    path: list[str],
    comps: list[str],
    owner: dict[str, list[str]],
) -> dict[str, Any]:
    dupes: list[dict[str, str]] = []
    novel: list[str] = []
    for cid in comps:
        prev = owner.get(cid)
        if prev is not None and prev != path:
            dupes.append(
                {
                    "component_id": cid,
                    "owner_path": path_str(prev),
                    "owner_path_list": prev,
                    "suggested_link": doc_stem_from_path(prev) + ".md",
                }
            )
        elif prev is None:
            novel.append(cid)

    overlap_by_owner: dict[str, list[str]] = defaultdict(list)
    suggested_links: set[str] = set()
    cross_top_level: list[dict[str, Any]] = []
    current_top = top_level_module(path)

    for d in dupes:
        owner_path_list: list[str] = d["owner_path_list"]
        overlap_by_owner[d["owner_path"]].append(d["component_id"])
        suggested_links.add(d["suggested_link"])
        if current_top and top_level_module(owner_path_list) != current_top:
            cross_top_level.append(
                {
                    "component_id": d["component_id"],
                    "canonical_owner": d["owner_path"],
                    "link": d["suggested_link"],
                }
            )

    mixed_redocumentation = len(dupes) > 0 and len(novel) > 0
    valid = len(comps) == 0 or len(dupes) == 0

    fix_lines: list[str] = []
    if not valid:
        fix_lines.append(
            f"Remove {len(dupes)} component ID(s) already owned by an earlier module subtree."
        )
        if novel:
            fix_lines.append(
                f"Keep only the {len(novel)} novel ID(s) in this node, or move them to a "
                f"separate sub-module that does not repeat earlier symbols."
            )
        if suggested_links:
            links = ", ".join(f"`{link}`" for link in sorted(suggested_links))
            fix_lines.append(f"Link to existing docs instead of re-generating: {links}.")

    return {
        "path": path_str(path),
        "path_list": path,
        "top_level": current_top,
        "component_count": len(comps),
        "valid_as_owner": valid,
        "duplicate_count": len(dupes),
        "novel_component_ids": novel,
        "mixed_redocumentation": mixed_redocumentation,
        "cross_top_level_redocumentation": len(cross_top_level) > 0,
        "cross_top_level_duplicates": cross_top_level,
        "overlap_by_owner_module": dict(
            sorted((k, sorted(v)) for k, v in overlap_by_owner.items())
        ),
        "suggested_doc_links": sorted(suggested_links),
        "recommended_fix": fix_lines,
    }


def processing_order_key(path: list[str]) -> tuple[int, list[str]]:
    """Leaf-first: deeper paths before ancestors (matches CodeWiki doc order)."""
    return (-len(path), path)


def incremental_by_top_level(tree: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Walk top-level modules in JSON key order.

    The first top-level module is the baseline (defines subtree ownership, e.g.
    module_a -> sub_module_a1). Each later module is checked against all
    component IDs already registered under earlier top-level modules.
    """
    top_names = list(tree.keys())
    all_nodes = walk_tree(tree)
    reports: list[dict[str, Any]] = []

    for i, top in enumerate(top_names):
        earlier_tops = set(top_names[:i])
        frozen_nodes = [n for n in all_nodes if n[0] and n[0][0] in earlier_tops]
        upcoming_nodes = [n for n in all_nodes if n[0] and n[0][0] == top]

        owner = build_first_owner_registry(frozen_nodes)
        node_results: list[dict[str, Any]] = []
        upcoming_sorted = sorted(upcoming_nodes, key=lambda n: processing_order_key(n[0]))
        for path, _name, comps in upcoming_sorted:
            result = analyze_node_against_registry(path, comps, owner)
            node_results.append(result)
            for cid in comps:
                if cid not in owner:
                    owner[cid] = path
        node_results.sort(key=lambda r: processing_order_key(r["path_list"]))

        reports.append(
            {
                "top_level_module": top,
                "order_index": i,
                "is_baseline_module": i == 0,
                "policy": (
                    "baseline — establishes canonical ownership for this subtree "
                    "(sub-modules such as sub_module_a1 / sub_module_a2)"
                    if i == 0
                    else "must not re-assign component IDs owned under earlier top-level modules"
                ),
                "frozen_modules": sorted(earlier_tops),
                "nodes": node_results,
                "any_invalid": any(not r["valid_as_owner"] for r in node_results),
            }
        )
    return reports


def collect_cross_module_violations(
    incremental: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Later modules reclaiming IDs from an earlier module's subtree (any top-level)."""
    violations: list[dict[str, Any]] = []
    for block in incremental:
        for node in block["nodes"]:
            if node["valid_as_owner"]:
                continue
            violations.append(
                {
                    "violating_module": node["path"],
                    "top_level_module": block["top_level_module"],
                    "baseline_module": block["is_baseline_module"],
                    "mixed_with_novel_ids": node["mixed_redocumentation"],
                    "duplicate_count": node["duplicate_count"],
                    "novel_component_ids": node["novel_component_ids"],
                    "overlap_by_owner_module": node["overlap_by_owner_module"],
                    "suggested_doc_links": node["suggested_doc_links"],
                    "recommended_fix": node["recommended_fix"],
                    "cross_top_level": node["cross_top_level_redocumentation"],
                }
            )
    return violations


def _format_id_list(ids: list[str], limit: int = 20) -> str:
    if not ids:
        return "_(none)_"
    shown = ", ".join(f"`{component_id_str(x)}`" for x in ids[:limit])
    if len(ids) > limit:
        shown += f" … (+{len(ids) - limit} more)"
    return shown


def render_markdown(
    tree_path: Path,
    global_dupes: dict[str, list[list[str]]],
    incremental: list[dict[str, Any]],
    cross_violations: list[dict[str, Any]],
) -> str:
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines: list[str] = [
        "# Module tree ownership validation",
        "",
        f"- **Generated:** {ts}",
        f"- **Source:** `{tree_path.name}`",
        "",
        "## Summary",
        "",
    ]

    global_invalid = len(global_dupes) > 0
    incr_invalid = any(r["any_invalid"] for r in incremental)
    if not global_invalid and not incr_invalid:
        lines.append("**Status: OK** — Each component ID is assigned to at most one module node.")
    else:
        lines.append("**Status: ISSUES FOUND** — See sections below.")
    lines.extend(["", "---", ""])

    lines.extend(
        [
            "## Rule (generation order)",
            "",
            "1. The **first** top-level module in `module_tree.json` defines canonical ownership "
            "for its full subtree (parent + delegated sub-modules).",
            "2. Example: `module_a` -> `sub_module_a1` owns `node_1`, `node_2`; "
            "`sub_module_a2` owns `node_3`, `node_4`.",
            "3. When **`module_b`** is processed later, it **must not** list those IDs again under "
            "its own sub-modules — even combined with new IDs "
            "(e.g. `[node_1, node_2, node_7, node_8]` in `sub_module_b2` is **wrong**).",
            "4. **Correct behavior:** document only novel IDs in `module_b`; "
            "**link** to `sub_module_a1.md` / `sub_module_a2.md` for shared symbols.",
            "",
            "---",
            "",
        ]
    )

    lines.extend(
        [
            "## Cross-module re-documentation",
            "",
            "Invalid assignments: a module node lists component IDs already owned by another "
            "node that should have been documented first (sub-module before parent; earlier "
            "top-level module before later). Includes **mixed bundles** "
            "(e.g. `node_1`, `node_2` from `sub_module_a1` plus novel `node_7`, `node_8`).",
            "",
        ]
    )
    if not cross_violations:
        lines.append("_No re-documentation violations detected._")
    else:
        lines.append(f"**{len(cross_violations)} module node(s) invalid:**")
        lines.append("")
        for v in cross_violations:
            mixed = (
                "yes — remove duplicates; keep or relocate novel IDs only"
                if v["mixed_with_novel_ids"]
                else "no — entire component list duplicates an earlier subtree"
            )
            scope = (
                "cross-top-level (e.g. module_b vs module_a subtree)"
                if v["cross_top_level"]
                else "within same top-level (e.g. parent repeats sub-module IDs)"
            )
            lines.append(f"### `{v['violating_module']}`")
            lines.append("")
            lines.append(f"- **Top-level module:** `{v['top_level_module']}`")
            lines.append(f"- **Scope:** {scope}")
            lines.append(f"- **Mixed bundle:** {mixed}")
            lines.append(
                f"- **Duplicate IDs (already documented elsewhere):** {v['duplicate_count']}"
            )
            for owner_path, ids in v["overlap_by_owner_module"].items():
                lines.append(
                    f"  - Canonical subtree `{owner_path}` ({len(ids)} ID(s)): "
                    + _format_id_list(ids, limit=8)
                )
            lines.append(
                "- **Novel IDs (may stay if moved to a clean sub-module):** "
                + _format_id_list(v["novel_component_ids"], limit=8)
            )
            links = ", ".join(f"`{link}`" for link in v["suggested_doc_links"])
            lines.append(f"- **Link instead of re-generating:** {links}")
            for fix in v["recommended_fix"]:
                lines.append(f"- **Fix:** {fix}")
            lines.append("")

    lines.extend(["---", "", "## Global duplicate ownership", "", ""])
    if not global_dupes:
        lines.append("_No global duplicates._")
    else:
        lines.append(f"**{len(global_dupes)} component ID(s) with multiple owners:**")
        lines.append("")
        for cid in sorted(global_dupes.keys()):
            lines.append(f"### `{component_id_str(cid)}`")
            lines.append("")
            for p in global_dupes[cid]:
                lines.append(f"- `{path_str(p)}` → link target: `{doc_stem_from_path(p)}.md`")
            lines.append("")

    lines.extend(["---", "", "## Incremental view (by top-level module order)", ""])
    lines.append(
        "Matches doc generation order: baseline module first, then each later top-level module "
        "checked against all IDs registered under earlier modules."
    )
    lines.append("")

    for block in incremental:
        top = block["top_level_module"]
        status = "INVALID" if block["any_invalid"] else "OK"
        lines.append(f"### `{top}` — {status}")
        lines.append("")
        lines.append(f"- **Policy:** {block['policy']}")
        if block["frozen_modules"]:
            frozen = ", ".join(f"`{m}`" for m in block["frozen_modules"])
            lines.append(f"- **Compared against:** {frozen}")
        lines.append("")

        for node in block["nodes"]:
            if node["component_count"] == 0:
                lines.append(f"- **`{node['path']}`** — empty `components` (overview-only node)")
                continue
            if node["valid_as_owner"]:
                lines.append(
                    f"- **`{node['path']}`** — OK "
                    f"({node['component_count']} component(s), all novel or first owner)"
                )
                continue

            mixed_note = (
                " **Mixed bundle invalid** (earlier subtree + novel IDs)."
                if node["mixed_redocumentation"]
                else ""
            )
            lines.append(
                f"- **`{node['path']}`** — **INVALID** "
                f"({node['duplicate_count']} duplicate(s)).{mixed_note}"
            )
            lines.append(
                "  - Duplicate IDs re-document an earlier subtree; remove them from this node."
            )
            lines.append(
                f"  - Novel IDs ({len(node['novel_component_ids'])}): "
                + _format_id_list(node["novel_component_ids"])
            )
            link_parts = ", ".join(f"[…]({link})" for link in node["suggested_doc_links"])
            lines.append(f"  - Suggested markdown links: {link_parts}")
            for owner_path, ids in node["overlap_by_owner_module"].items():
                lines.append(
                    f"  - Already owned under `{owner_path}` ({len(ids)} ID(s)): "
                    + _format_id_list(ids, limit=5)
                )
            lines.append("")

        lines.append("")

    lines.extend(
        [
            "---",
            "",
            "## Recommended actions",
            "",
            "1. Edit `module_tree.json`: remove duplicate component IDs from later modules.",
            "2. Keep one canonical module node per component (usually under the first module's "
            "subtree).",
            "3. In markdown, link to existing sub-module docs instead of re-running sub-agents.",
            "4. Re-run this script until **Status: OK**.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    docs_dir = script_docs_dir()
    tree_path = docs_dir / MODULE_TREE_FILENAME
    if not tree_path.is_file():
        print(f"Missing {tree_path}", file=sys.stderr)
        print(
            "Copy validate_module_tree_ownership.py into your docs folder "
            "(next to module_tree.json) and run again.",
            file=sys.stderr,
        )
        return 2

    try:
        tree = json.loads(tree_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Invalid JSON in {tree_path}: {e}", file=sys.stderr)
        return 2

    if not isinstance(tree, dict):
        print("module_tree.json root must be a JSON object", file=sys.stderr)
        return 2

    all_nodes = walk_tree(tree)
    global_dupes = find_global_duplicates(all_nodes)
    incremental = incremental_by_top_level(tree)
    cross_violations = collect_cross_module_violations(incremental)
    md = render_markdown(tree_path, global_dupes, incremental, cross_violations)

    report_dir = docs_dir / REPORT_DIRNAME
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / REPORT_FILENAME
    report_path.write_text(md, encoding="utf-8")

    print(f"Wrote {report_path}")
    has_issues = bool(global_dupes) or any(r["any_invalid"] for r in incremental)
    return 1 if has_issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
