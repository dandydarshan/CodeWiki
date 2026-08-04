"""Incremental documentation updates — what changed, and what has to be redone.

``codewiki generate --update`` should re-document only the parts of a repository
that actually changed.  That needs three answers, and this module owns all three
so the CLI and the MCP server cannot drift apart:

1. *What changed on disk* — :func:`detect_changed_files` compares the commit
   recorded in ``metadata.json`` against the current working tree: the committed
   diff plus staged, unstaged and untracked files.  Uncommitted work counts,
   otherwise editing a file and re-running ``--update`` reports "up to date".
   When git cannot answer (no baseline commit, unreachable commit after a
   rebase or shallow clone) it falls back to comparing file mtimes with the
   recorded generation timestamp.

2. *Whether the module tree still describes the repo* —
   :func:`module_tree_rebuild_reason`.  Clustering costs an LLM call, so the
   cached tree is reused while it still covers every source file; adding or
   removing files is what changes the grouping, editing inside a file is not.

3. *Which module docs are now wrong* — :func:`plan_incremental_update` maps
   changed files onto module component IDs, and cascades to the ancestors whose
   overviews are synthesised from those docs.

Component IDs are ``relative/path.py::Symbol``; module names are also the
``.md`` filename stems, which is what makes doc-level reuse possible.
"""

from __future__ import annotations

import json
import logging
import os
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from codewiki.src.be.module_naming import resolve_module_doc_path

logger = logging.getLogger(__name__)

# Extensions CodeWiki's analyzers understand, used by the mtime fallback.
SOURCE_EXTENSIONS = {
    ".py", ".java", ".js", ".jsx", ".ts", ".tsx",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".hh",
    ".cs", ".kt", ".kts", ".php", ".go", ".rb", ".rs",
}

_SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "dist", "build"}


# ---------------------------------------------------------------------------
#  Module tree helpers
# ---------------------------------------------------------------------------

def component_file(component_id: str) -> str:
    """Repo-relative file path a component ID points at."""
    return str(component_id).split("::", 1)[0]


def iter_modules(
    tree: Optional[Dict[str, Any]],
    _parents: Tuple[str, ...] = (),
) -> Iterator[Tuple[Tuple[str, ...], str, Dict[str, Any]]]:
    """Yield ``(path, name, info)`` for every module at every depth of *tree*."""
    if not isinstance(tree, dict):
        return
    for name, info in tree.items():
        if not isinstance(info, dict):
            continue
        path = _parents + (name,)
        yield path, name, info
        children = info.get("children")
        if isinstance(children, dict) and children:
            yield from iter_modules(children, path)


def module_files(module_info: Dict[str, Any]) -> Set[str]:
    """Files owned by a single module (its own components, not its children's)."""
    return {component_file(c) for c in module_info.get("components", []) or []}


def tree_component_files(tree: Optional[Dict[str, Any]]) -> Set[str]:
    """Every file referenced by any module in *tree*."""
    files: Set[str] = set()
    for _, _, info in iter_modules(tree):
        files |= module_files(info)
    return files


def files_overlap(a: str, b: str) -> bool:
    """True when two repo-relative paths refer to the same file or nest."""
    if a == b:
        return True
    # One side may be an absolute-ish or subdirectory-prefixed variant of the
    # other; and a changed *directory* covers the files beneath it.
    return (
        a.endswith("/" + b)
        or b.endswith("/" + a)
        or a.startswith(b + "/")
        or b.startswith(a + "/")
    )


def module_is_touched(module_info: Dict[str, Any], changed_files: Iterable[str]) -> bool:
    """True when any of the module's component files is in *changed_files*."""
    changed = list(changed_files)
    return any(files_overlap(f, cf) for f in module_files(module_info) for cf in changed)


def find_affected_modules(
    module_tree: Dict[str, Any],
    changed_files: List[str],
) -> Tuple[Set[str], Set[str]]:
    """Map changed files onto module names.

    Returns ``(affected, cascade)`` where *affected* own a changed file and
    *cascade* are the ancestors (plus ``overview``) whose summaries are derived
    from them.
    """
    affected: Set[str] = set()
    cascade: Set[str] = set()

    for path, name, info in iter_modules(module_tree):
        if module_is_touched(info, changed_files):
            affected.add(name)
            cascade.update(path[:-1])

    if affected:
        cascade.add("overview")
    return affected, cascade


# ---------------------------------------------------------------------------
#  Change detection
# ---------------------------------------------------------------------------

@dataclass
class ChangeSet:
    """Files that changed since the last generation."""

    files: List[str]
    method: str  # "git" or "mtime"
    baseline: Optional[str] = None


def load_metadata(output_dir: Path) -> Optional[Dict[str, Any]]:
    """Read ``metadata.json`` from a docs directory, or None if unusable."""
    metadata_path = Path(output_dir) / "metadata.json"
    if not metadata_path.exists():
        return None
    try:
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def detect_changed_files(
    repo_path: Path,
    output_dir: Path,
    compare_to: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[ChangeSet]:
    """Detect source files changed since the last documentation generation.

    Returns None when there is no usable baseline (first run, no metadata),
    which callers should treat as "generate everything".
    """
    repo_path = Path(repo_path)
    output_dir = Path(output_dir)
    if metadata is None:
        metadata = load_metadata(output_dir)
    if metadata is None and not compare_to:
        return None

    changes = _detect_via_git(repo_path, metadata or {}, output_dir, compare_to)
    if changes is None:
        changes = _detect_via_mtime(repo_path, metadata or {}, output_dir)
    return changes


def _relativizer(repo_path: Path, git_root: Path, output_dir: Path):
    """Build a path normaliser: git-root-relative → repo-relative, or None to drop."""
    repo_root = repo_path.resolve()
    try:
        subpath = repo_root.relative_to(git_root).as_posix()
    except ValueError:
        subpath = ""
    if subpath == ".":
        subpath = ""

    # Generated docs must never count as source changes, or every run would
    # report the previous run's output as a reason to regenerate.
    output_rel = ""
    try:
        output_rel = Path(output_dir).resolve().relative_to(repo_root).as_posix()
        if output_rel == ".":
            output_rel = ""
    except (ValueError, TypeError):
        pass

    def normalize(path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        if subpath:
            if not path.startswith(subpath + "/"):
                return None  # outside the documented subdirectory
            path = path[len(subpath) + 1:]
        if path.startswith(".codewiki/"):
            return None
        if output_rel and (path == output_rel or path.startswith(output_rel + "/")):
            return None
        return path

    return normalize


def _detect_via_git(
    repo_path: Path,
    metadata: Dict[str, Any],
    output_dir: Path,
    compare_to: Optional[str] = None,
) -> Optional[ChangeSet]:
    """Committed diff since the baseline plus staged/unstaged/untracked work.

    Returns None when git cannot answer, so the caller can fall back to mtimes.
    """
    try:
        import git
        repo = git.Repo(repo_path, search_parent_directories=True)
    except Exception:
        return None

    prev_commit = compare_to or metadata.get("generation_info", {}).get("commit_id")
    if not prev_commit:
        return None  # no baseline; let the mtime fallback decide

    try:
        current_commit = repo.head.commit.hexsha
    except Exception:
        return None

    if repo.working_tree_dir is None:
        return None
    normalize = _relativizer(repo_path, Path(repo.working_tree_dir).resolve(), output_dir)

    changed: List[str] = []
    seen: Set[str] = set()

    def add(raw: Optional[str]) -> None:
        path = normalize(raw)
        if path and path not in seen:
            seen.add(path)
            changed.append(path)

    if prev_commit != current_commit:
        try:
            diff_index = repo.commit(prev_commit).diff(current_commit)
        except Exception:
            # Baseline unreachable (shallow clone, rebase, gc). Reporting an
            # empty diff here would falsely claim "up to date".
            logger.warning(
                "Baseline commit %s is unreachable in %s; falling back to mtime detection.",
                prev_commit, repo_path,
            )
            return None
        for diff in diff_index:
            add(diff.a_path)
            add(diff.b_path)

    # Uncommitted work counts too: staged, unstaged and untracked.
    try:
        for diff in list(repo.index.diff("HEAD")) + list(repo.index.diff(None)):
            add(diff.a_path)
            add(diff.b_path)
        for path in repo.untracked_files:
            add(path)
    except Exception:
        pass

    return ChangeSet(files=changed, method="git", baseline=prev_commit)


def _detect_via_mtime(
    repo_path: Path,
    metadata: Dict[str, Any],
    output_dir: Path,
) -> Optional[ChangeSet]:
    """Fallback: source files modified after the recorded generation timestamp."""
    timestamp_str = metadata.get("generation_info", {}).get("timestamp")
    if not timestamp_str:
        return None

    try:
        from datetime import datetime
        prev_time = datetime.fromisoformat(timestamp_str).timestamp()
    except (ValueError, TypeError):
        return None

    try:
        output_resolved = Path(output_dir).resolve()
    except OSError:
        output_resolved = None

    changed: List[str] = []
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d not in _SKIP_DIRS
        ]
        if output_resolved is not None and Path(dirpath).resolve() == output_resolved:
            dirnames[:] = []
            continue
        for filename in filenames:
            filepath = Path(dirpath) / filename
            if filepath.suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            try:
                if filepath.stat().st_mtime > prev_time:
                    changed.append(filepath.relative_to(repo_path).as_posix())
            except (OSError, ValueError):
                continue

    return ChangeSet(files=changed, method="mtime", baseline=timestamp_str)


# ---------------------------------------------------------------------------
#  Deciding whether the module tree is still valid
# ---------------------------------------------------------------------------

def module_tree_rebuild_reason(
    cached_tree: Optional[Dict[str, Any]],
    leaf_nodes: List[str],
    components: Dict[str, Any],
) -> Optional[str]:
    """Explain why the cached module tree no longer describes the repository.

    Returns None when the cached tree can be reused as-is.  Clustering is an
    LLM call, so it is only redone when the *set of files* changed: edits
    inside a file leave the grouping intact, but a file that no module knows
    about would never be documented at all.
    """
    if not cached_tree:
        # An empty tree means whole-repository mode was chosen because the repo
        # fit in one context window.  Re-running the (LLM-free when it still
        # fits) clustering check is how we notice the repo outgrew that.
        return "no cached module tree"

    def file_of(node_id: str) -> str:
        node = components.get(node_id)
        return getattr(node, "relative_path", None) or component_file(node_id)

    tree_files = tree_component_files(cached_tree)
    leaf_files = {file_of(node_id) for node_id in leaf_nodes}
    current_files = {
        getattr(node, "relative_path", None) or ""
        for node in components.values()
    }

    added = leaf_files - tree_files
    removed = {f for f in tree_files if f not in current_files}

    if added and removed:
        return f"{len(added)} new and {len(removed)} deleted source file(s)"
    if added:
        return f"{len(added)} new source file(s) not covered by the cached tree"
    if removed:
        return f"{len(removed)} source file(s) in the cached tree no longer exist"
    return None


# ---------------------------------------------------------------------------
#  Keeping module names (and therefore docs) stable across re-clustering
# ---------------------------------------------------------------------------

def align_module_names(
    previous_tree: Optional[Dict[str, Any]],
    new_tree: Dict[str, Any],
    min_overlap: float = 0.5,
) -> Dict[str, Any]:
    """Rename freshly clustered modules back to their previous names.

    Module names are LLM-chosen and also the doc filenames, so a re-clustering
    that calls the same group ``core_services`` instead of ``cli_core`` would
    orphan every existing doc and force a full regeneration.  Each new module is
    matched to the previous module it shares the most component files with; a
    match above *min_overlap* (Jaccard) inherits that name.
    """
    if not previous_tree or not new_tree:
        return new_tree

    # Only top-level modules are candidates: a clustered tree is flat, and its
    # sub-modules are re-derived by the sub-agents during generation.
    previous_files = {
        name: module_files(info)
        for name, info in previous_tree.items()
        if isinstance(info, dict)
    }

    scores: List[Tuple[float, str, str]] = []
    for new_name, new_info in new_tree.items():
        new_files = module_files(new_info)
        if not new_files:
            continue
        for old_name, old_files in previous_files.items():
            if not old_files:
                continue
            overlap = len(new_files & old_files) / len(new_files | old_files)
            if overlap >= min_overlap:
                scores.append((overlap, new_name, old_name))

    # Greedy best-first matching: each old name is claimed at most once.
    scores.sort(key=lambda item: (-item[0], item[1], item[2]))
    renames: Dict[str, str] = {}
    claimed_old: Set[str] = set()
    for overlap, new_name, old_name in scores:
        if new_name in renames or old_name in claimed_old:
            continue
        if new_name == old_name:
            claimed_old.add(old_name)
            continue
        # Don't steal a name another new module already keeps unchanged.
        if old_name in new_tree and old_name not in renames:
            continue
        renames[new_name] = old_name
        claimed_old.add(old_name)
        logger.info(
            "Re-clustered module '%s' matches previous module '%s' (%.0f%% of files); "
            "keeping the existing name so its doc can be reused.",
            new_name, old_name, overlap * 100,
        )

    if not renames:
        return new_tree
    return {renames.get(name, name): info for name, info in new_tree.items()}


# ---------------------------------------------------------------------------
#  The update plan
# ---------------------------------------------------------------------------

@dataclass
class UpdatePlan:
    """Which docs survive an incremental update and which get rebuilt."""

    regenerate: Set[str] = field(default_factory=set)
    reuse: Set[str] = field(default_factory=set)
    removed: Set[str] = field(default_factory=set)
    reasons: Dict[str, str] = field(default_factory=dict)

    @property
    def is_noop(self) -> bool:
        return not self.regenerate and not self.removed


def plan_incremental_update(
    previous_tree: Optional[Dict[str, Any]],
    new_tree: Dict[str, Any],
    changed_files: Iterable[str],
    working_dir: str,
) -> UpdatePlan:
    """Decide which modules must be re-documented.

    A module is rebuilt when it is new, when its doc is missing, when its
    component set moved, or when one of its files changed.  Everything else
    keeps the doc it already has.  Ancestors of a rebuilt module are rebuilt
    too, because a parent overview is synthesised from its children's docs.
    """
    changed = list(changed_files)
    plan = UpdatePlan()

    previous_modules = {name: info for _, name, info in iter_modules(previous_tree)}
    new_modules = {name: info for _, name, info in iter_modules(new_tree)}

    for path, name, info in iter_modules(new_tree):
        previous = previous_modules.get(name)
        reason: Optional[str] = None
        if previous is None:
            reason = "new module"
        elif resolve_module_doc_path(working_dir, name) is None:
            reason = "documentation missing"
        elif set(previous.get("components", []) or []) != set(info.get("components", []) or []):
            reason = "components changed"
        elif module_is_touched(info, changed):
            reason = "source changed"

        if reason:
            plan.regenerate.add(name)
            plan.reasons[name] = reason
            for ancestor in path[:-1]:
                plan.regenerate.add(ancestor)
                plan.reasons.setdefault(ancestor, f"child module '{name}' changed")
        else:
            plan.reuse.add(name)

    # Docs with nothing left to describe. A freshly clustered tree is flat —
    # sub-modules only appear once sub-agents split a module during generation —
    # so a nested module is judged by its top-level ancestor: while that survives
    # its whole branch is carried over, docs included.
    for path, name, _info in iter_modules(previous_tree):
        if name in new_modules or path[0] in new_modules:
            continue
        plan.removed.add(name)
        plan.reasons[name] = "module no longer exists"

    # A rebuilt module's sub-agents recreate its children from scratch; leaving
    # the old child docs on disk would make their names look taken and produce
    # duplicate "<name>_2.md" pages instead.
    for _, name, info in iter_modules(previous_tree):
        if name not in plan.regenerate:
            continue
        for _, descendant, _info in iter_modules(info.get("children") or {}):
            if descendant not in plan.regenerate:
                plan.removed.add(descendant)
                plan.reasons.setdefault(descendant, f"parent module '{name}' is being rebuilt")

    plan.reuse -= plan.regenerate
    plan.reuse -= plan.removed
    return plan


def apply_update_plan(plan: UpdatePlan, working_dir: str) -> List[str]:
    """Delete the docs the plan invalidates so generation rewrites them.

    Returns the filenames that were removed.  ``overview.md`` goes whenever
    anything else does — it summarises the whole tree.
    """
    removed_files: List[str] = []
    targets = set(plan.regenerate) | set(plan.removed)
    if targets:
        targets.add("overview")

    for name in sorted(targets):
        doc_path = resolve_module_doc_path(working_dir, name)
        if doc_path is None:
            continue
        try:
            os.remove(doc_path)
        except OSError as exc:
            logger.warning("Could not remove stale doc %s: %s", doc_path, exc)
            continue
        removed_files.append(os.path.basename(doc_path))
    return removed_files


def carry_over_children(
    new_tree: Dict[str, Any],
    previous_tree: Optional[Dict[str, Any]],
    preserved: Set[str],
) -> Dict[str, Any]:
    """Keep the sub-modules a previous run discovered under reused modules.

    ``module_tree.json`` grows branches as sub-agents split large modules, while
    ``first_module_tree.json`` stays flat.  Rewriting the former from the latter
    would drop those branches — and the docs they point at — from the published
    wiki even though the files are still on disk.
    """
    if not previous_tree:
        return new_tree

    previous_children = {
        name: info.get("children")
        for _, name, info in iter_modules(previous_tree)
    }

    merged = deepcopy(new_tree)
    existing_names = {name for _, name, _ in iter_modules(merged)}
    # Materialised up front: grafting children in mid-walk would make the
    # generator descend into the branches just added.
    for _, name, info in list(iter_modules(merged)):
        if name not in preserved:
            continue
        children = previous_children.get(name)
        if not isinstance(children, dict) or not children:
            continue
        # A name is a doc filename, so the same one must not appear twice in the
        # tree: skip a branch the new tree already places somewhere else.
        info["children"] = deepcopy({
            child: subtree
            for child, subtree in children.items()
            if child not in existing_names
        })
    return merged
