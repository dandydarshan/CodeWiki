"""Coordination for concurrently-running per-module documentation agents.

Module agents at the same depth of the module tree are independent of each
other — a parent only ever consumes its own children's docs — so they can run
concurrently.  Two pieces of shared state stop that from being safe on its own,
and this module owns both:

``module_tree.json``
    Each agent loads the whole tree, its sub-agents graft new branches into
    that in-memory copy, and it saves the whole tree back.  Run two of those
    concurrently and the second save silently drops the first agent's branches.
    :meth:`ModuleTreeCoordinator.merge_subtree` re-reads the file under a lock
    and grafts only the calling module's subtree, so concurrent writers
    accumulate instead of clobbering.

Sub-module names
    ``normalize_sub_module_specs`` decides a name is free by looking at the tree
    and the ``.md`` files on disk.  Concurrent sub-agents both look before
    either writes, both see the name free, and one doc overwrites the other
    (the failure issue #76 fixed for the serial case).
    :meth:`ModuleTreeCoordinator.reserve_sub_module_names` keeps an in-process
    reservation set so a name is taken the moment it is handed out.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Set

from codewiki.src.be.module_naming import normalize_sub_module_specs
from codewiki.src.utils import file_manager

logger = logging.getLogger(__name__)


def node_at_path(tree: Dict[str, Any], module_path: List[str]) -> Optional[Dict[str, Any]]:
    """Return the module node at *module_path*, or None if the path is absent."""
    node = tree
    for depth, key in enumerate(module_path):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
        if depth < len(module_path) - 1:
            node = node.get("children", {})
    return node if isinstance(node, dict) else None


class ModuleTreeCoordinator:
    """Serializes module-tree writes and sub-module name allocation."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._reserved_names: Set[str] = set()

    async def merge_subtree(
        self,
        module_tree_path: str,
        module_path: List[str],
        agent_tree: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Persist one module's branches without discarding concurrent writers'.

        *agent_tree* is the agent's private copy of the tree; the only part it
        can have changed is the subtree under *module_path*.  Everything else in
        it is a stale snapshot, so grafting just that subtree onto a freshly
        read tree is what keeps parallel runs consistent.
        """
        async with self._lock:
            if not module_path:
                # Whole-repo mode: there is exactly one agent, nothing to merge.
                file_manager.save_json(agent_tree, module_tree_path)
                return agent_tree

            disk_tree = file_manager.load_json(module_tree_path)
            agent_node = node_at_path(agent_tree, module_path)
            disk_node = node_at_path(disk_tree, module_path)

            if agent_node is None or disk_node is None:
                # Overwriting wholesale here would drop other modules' work, so
                # leave the file alone and say so.
                logger.warning(
                    "Could not merge subtree for %s into %s; the module is missing "
                    "from one of the trees. Leaving the saved tree unchanged.",
                    "/".join(module_path),
                    module_tree_path,
                )
                return disk_tree

            disk_node["children"] = agent_node.get("children", {})
            file_manager.save_json(disk_tree, module_tree_path)
            return disk_tree

    async def reserve_sub_module_names(
        self,
        sub_module_specs: Dict[str, Any],
        parent_name: Optional[str],
        module_tree: Dict[str, Any],
        working_dir: str,
    ) -> Dict[str, str]:
        """Allocate unique sub-module names, atomically against other agents."""
        async with self._lock:
            name_map = normalize_sub_module_specs(
                sub_module_specs,
                parent_name,
                module_tree,
                working_dir,
                reserved=self._reserved_names,
            )
            self._reserved_names.update(name_map.values())
            return name_map
