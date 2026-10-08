"""OS-independent component IDs.

Component IDs look like ``<repo-relative path>::<name>``. The path part always
uses forward slashes, so the same repository yields the same IDs on every OS,
IDs match what LLMs echo back (they write ``/`` regardless of platform), and
code IDs line up with artifact IDs (which were already POSIX).
"""

import os
from typing import Any


def repo_relpath(file_path, repo_path=None) -> str:
    """Return *file_path* relative to *repo_path*, always with ``/`` separators."""
    path = str(file_path)
    if repo_path:
        try:
            path = os.path.relpath(path, repo_path)
        except ValueError:  # Windows: file on a different drive than the repo
            pass
    return path.replace(os.sep, "/")


def normalize_component_id(component_id: str) -> str:
    """Rewrite ``\\`` to ``/`` in the path part of a component ID.

    IDs supplied by an LLM, or loaded from a module tree saved by an older
    Windows run, may use backslashes; the name part after ``::`` is left alone.
    """
    if not isinstance(component_id, str):
        return component_id
    path, sep, name = component_id.partition("::")
    if not sep:
        return component_id
    return f"{path.replace(chr(92), '/')}{sep}{name}"


def normalize_component_ids(component_ids) -> list:
    """Normalize a list of component IDs, dropping duplicates it creates."""
    if not isinstance(component_ids, list):
        return component_ids
    return list(dict.fromkeys(normalize_component_id(cid) for cid in component_ids))


def normalize_module_tree_ids(module_tree: Any) -> bool:
    """Normalize every ``components`` list in a module tree in place.

    Returns True when anything changed.
    """
    changed = False
    if not isinstance(module_tree, dict):
        return changed
    for module_info in module_tree.values():
        if not isinstance(module_info, dict):
            continue
        components = module_info.get("components")
        if isinstance(components, list):
            normalized = normalize_component_ids(components)
            if normalized != components:
                module_info["components"] = normalized
                changed = True
        if normalize_module_tree_ids(module_info.get("children")):
            changed = True
    return changed
