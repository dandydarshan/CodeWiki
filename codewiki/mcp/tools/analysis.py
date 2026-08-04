"""MCP tool: analyze_repo — parse a repository and build the dependency graph.

This is the entry-point tool for the IDE-driven wiki generation pipeline.
It runs CodeWiki's Tree-sitter-based dependency analyzer (no LLM needed),
caches the results in a new session, and writes the full component index,
leaf nodes, and other analysis data to files on disk.  The IDE agent reads
those files directly instead of receiving large payloads over stdio.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from codewiki.mcp.session import SessionState, SessionStore
from codewiki.mcp.workspace import SessionWorkspace
# Change detection is shared with `codewiki generate --update`; keeping one
# implementation is what stops the CLI and the MCP server from disagreeing
# about what changed.
from codewiki.src.be import incremental

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Incremental update: detect changes since last generation
# ---------------------------------------------------------------------------

def _detect_changes(
    repo_path: Path,
    output_dir: Path,
) -> Optional[Dict[str, Any]]:
    """Detect changes since last documentation generation.

    Returns a changes dict with affected modules, or None if no previous
    generation exists (first run).

    Detection strategy:
      1. Git-based: compare stored commit_id with current HEAD, plus check
         uncommitted changes via ``git status``.
      2. Fallback: compare file mtime with stored ``timestamp`` in metadata.
    """
    metadata_path = output_dir / "metadata.json"
    module_tree_path = output_dir / "module_tree.json"

    if not metadata_path.exists() or not module_tree_path.exists():
        return None

    try:
        module_tree = json.loads(module_tree_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None

    changes = incremental.detect_changed_files(repo_path, output_dir)
    if changes is None:
        return None

    if not changes.files:
        return {
            "has_previous": True,
            "no_changes": True,
            "method": changes.method,
            "message": "No changes detected since last generation. Documentation is up to date.",
        }

    affected, cascade = incremental.find_affected_modules(module_tree, changes.files)

    return {
        "has_previous": True,
        "no_changes": False,
        "method": changes.method,
        "changed_files": changes.files,
        "affected_modules": sorted(affected),
        "cascade_modules": sorted(cascade),
        "hint": (
            f"Only {len(affected)} module(s) need updating: {sorted(affected)}. "
            f"Parent modules to refresh: {sorted(cascade)}. "
            "Use edit_doc_file for targeted updates, write_doc_file for new modules."
        ),
    }


def handle_analyze_repo(
    arguments: Dict[str, Any],
    store: SessionStore,
) -> str:
    """Run the dependency analysis, write results to workspace files,
    and return a compact summary with file paths."""
    repo_path = Path(arguments["repo_path"]).expanduser().resolve()
    if not repo_path.exists():
        return json.dumps({"error": f"Repository not found: {repo_path}"})

    output_dir = Path(arguments.get("output_dir", str(repo_path / "docs"))).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Build a minimal Config for the dependency analyzer (no LLM fields used)
    from codewiki.src.config import Config
    config = Config(
        repo_path=str(repo_path),
        output_dir=str(output_dir / "temp"),
        dependency_graph_dir=str(output_dir / "temp" / "dependency_graphs"),
        docs_dir=str(output_dir),
        max_depth=2,
        llm_base_url="not-needed",
        llm_api_key="not-needed",
        main_model="unused",
        cluster_model="unused",
        use_gitignore=arguments.get("use_gitignore", True),
    )

    # Apply optional include/exclude patterns
    include = arguments.get("include_patterns")
    exclude = arguments.get("exclude_patterns")
    if include or exclude:
        agent_instructions: Dict[str, Any] = {}
        if include:
            agent_instructions["include_patterns"] = [p.strip() for p in include.split(",")]
        if exclude:
            agent_instructions["exclude_patterns"] = [p.strip() for p in exclude.split(",")]
        config.agent_instructions = agent_instructions

    from codewiki.src.be.dependency_analyzer import DependencyGraphBuilder
    builder = DependencyGraphBuilder(config)
    components, leaf_nodes = builder.build_dependency_graph()

    # Create the session (generates session_id)
    session = store.create(
        repo_path=str(repo_path),
        output_dir=str(output_dir),
        components=components,
        leaf_nodes=leaf_nodes,
    )

    # Record the analyzed commit now — close_session uses it as the
    # incremental-update baseline in metadata.json.
    from codewiki.cli.utils.repo_validator import get_git_commit_hash
    session.analyzed_commit = get_git_commit_hash(repo_path) or None

    # Create the workspace with the real session_id
    workspace = SessionWorkspace(repo_path, session.session_id)
    session.workspace = workspace

    # -- Write full data to workspace files --

    # 1. Full component index (no pagination)
    component_index: list[dict] = []
    for comp_id, node in components.items():
        component_index.append({
            "id": comp_id,
            "type": getattr(node, "component_type", "unknown"),
            "file": getattr(node, "relative_path", ""),
        })
    workspace.write_json("component_index.json", component_index)

    # 2. Full leaf nodes list
    workspace.write_json("leaf_nodes.json", leaf_nodes)

    # 3. Language stats
    languages: Dict[str, int] = {}
    for node in components.values():
        lang = getattr(node, "language", "unknown")
        languages[lang] = languages.get(lang, 0) + 1
    workspace.write_json("languages.json", languages)

    # 4. Incremental update: detect changes since last generation
    changes = _detect_changes(repo_path, output_dir)
    if changes is not None:
        workspace.write_json("changes.json", changes)

    # 5. Summary with preview for quick reference
    summary = {
        "session_id": session.session_id,
        "repo_name": repo_path.name,
        "repo_path": str(repo_path),
        "output_dir": str(output_dir),
        "total_components": len(components),
        "total_leaf_nodes": len(leaf_nodes),
        "languages": languages,
        "leaf_nodes_preview": leaf_nodes[:20],
    }
    workspace.write_json("summary.json", summary)

    # -- Return compact MCP response --
    result = {
        "session_id": session.session_id,
        "workspace_dir": str(workspace.root),
        "repo_name": repo_path.name,
        "output_dir": str(output_dir),
        "stats": {
            "total_components": len(components),
            "total_leaf_nodes": len(leaf_nodes),
            "languages": languages,
        },
        "files": {
            "component_index": str(workspace.root / "component_index.json"),
            "leaf_nodes": str(workspace.root / "leaf_nodes.json"),
            "languages": str(workspace.root / "languages.json"),
            "summary": str(workspace.root / "summary.json"),
        },
        "changes": changes,
        "hint": (
            "Read the files above for full data. "
            "Use read_code_components(session_id, component_ids) to read source code. "
            "Use save_module_tree(session_id, module_tree) after clustering. "
            "Call get_prompt('cluster') for clustering rules."
        ),
    }
    if changes and not changes.get("no_changes"):
        result["hint"] = (
            "Incremental update detected. Only update affected modules listed in "
            "'changes.affected_modules'. Use edit_doc_file for targeted updates. "
            "Refresh cascade parent modules in 'changes.cascade_modules'."
        )
    return json.dumps(result, indent=2, ensure_ascii=False)
