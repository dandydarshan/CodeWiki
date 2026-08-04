from typing import List, Dict, Any, Callable, Optional
from collections import defaultdict
import ast
import json
import logging
import re
import traceback
logger = logging.getLogger(__name__)

from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.llm_services import call_llm
from codewiki.src.be.utils import count_tokens
from codewiki.src.config import Config
from codewiki.src.be.prompt_template import format_cluster_prompt

Completer = Callable[[str], str]

# When whole-repo mode is chosen but leaf entry points touch fewer than this
# fraction of parsed files, warn that coverage depends on agent exploration.
LOW_COVERAGE_RATIO = 0.5

# The clustering reply has to echo back every component ID it was handed, so the
# response is roughly as large as the component listing we send.  That response
# is capped by ``config.max_tokens``, and reasoning models (kimi, deepseek-r1,
# o-series) spend part of the same budget thinking before they emit a single
# group.  Keep one request's listing to this fraction of the output budget so the
# reply can't be cut off mid-dict; anything larger is split across requests.
CLUSTER_INPUT_TOKEN_BUDGET_RATIO = 0.35
MIN_CLUSTER_INPUT_TOKEN_BUDGET = 4_000

# Reasoning traces some providers inline in the message content.
_REASONING_BLOCK_RE = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
_OPEN_REASONING_RE = re.compile(r"<(think|thinking|reasoning)>", re.IGNORECASE)


def _group_leaf_nodes_by_file(
    leaf_nodes: List[str], components: Dict[str, Node]
) -> Dict[str, List[str]]:
    """Group leaf nodes by their file, dropping IDs that aren't in *components*."""
    leaf_nodes_by_file = defaultdict(list)
    for leaf_node in leaf_nodes:
        if leaf_node in components:
            leaf_nodes_by_file[components[leaf_node].relative_path].append(leaf_node)
        else:
            logger.warning(f"Skipping invalid leaf node '{leaf_node}' - not found in components")
    return dict(sorted(leaf_nodes_by_file.items()))


def format_potential_core_components(leaf_nodes: List[str], components: Dict[str, Node]) -> tuple[str, str]:
    """
    Format the potential core components into a string that can be used in the prompt.
    """
    potential_core_components = ""
    potential_core_components_with_code = ""
    for file, file_leaf_nodes in _group_leaf_nodes_by_file(leaf_nodes, components).items():
        potential_core_components += f"# {file}\n"
        potential_core_components_with_code += f"# {file}\n"
        for leaf_node in file_leaf_nodes:
            potential_core_components += f"\t{leaf_node}\n"
            potential_core_components_with_code += f"\t{leaf_node}\n"
            potential_core_components_with_code += f"{components[leaf_node].source_code}\n"

    return potential_core_components, potential_core_components_with_code


def _cluster_input_token_budget(config: Config) -> int:
    """Token budget for the component listing in a single clustering request."""
    return max(
        int(config.max_tokens * CLUSTER_INPUT_TOKEN_BUDGET_RATIO),
        MIN_CLUSTER_INPUT_TOKEN_BUDGET,
    )


def _chunk_leaf_nodes(
    leaf_nodes: List[str], components: Dict[str, Node], budget: int
) -> List[List[str]]:
    """
    Split leaf nodes into batches whose formatted listing fits within *budget*.

    Batches follow file boundaries where they can, so components that live
    together stay together and the model sees coherent groups.  A single file
    larger than the budget is split rather than blowing past it.
    """
    chunks: List[List[str]] = []
    current: List[str] = []
    current_tokens = 0

    for file, file_leaf_nodes in _group_leaf_nodes_by_file(leaf_nodes, components).items():
        header_tokens = count_tokens(f"# {file}\n")
        needs_header = True
        for leaf_node in file_leaf_nodes:
            cost = count_tokens(f"\t{leaf_node}\n") + (header_tokens if needs_header else 0)
            if current and current_tokens + cost > budget:
                chunks.append(current)
                current = []
                current_tokens = 0
                # A new batch re-emits the file header for the remaining nodes.
                cost += 0 if needs_header else header_tokens
                needs_header = True
            current.append(leaf_node)
            current_tokens += cost
            needs_header = False

    if current:
        chunks.append(current)
    return chunks


def _strip_reasoning(response: str) -> str:
    """Drop inline reasoning blocks so tag detection sees the real answer."""
    body = _REASONING_BLOCK_RE.sub("", response)
    # An unclosed opening tag means the model was cut off mid-thought; nothing
    # after it is answer text.
    open_tag = _OPEN_REASONING_RE.search(body)
    if open_tag:
        body = body[: open_tag.start()]
    return body


def _salvage_truncated_dict(text: str) -> Optional[dict]:
    """
    Rebuild a dict from output that was cut off part-way through.

    Walks the top-level entries and keeps the longest prefix that closes
    cleanly, so a reply truncated inside module five still yields modules
    one through four.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    quote = ""
    escaped = False
    last_complete_entry = -1

    for i in range(start, len(text)):
        char = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue
        if char in "\"'":
            in_string = True
            quote = char
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 1:
                # A top-level value just closed; everything up to here is usable.
                last_complete_entry = i
            elif depth == 0:
                return _literal_eval(text[start : i + 1])

    if last_complete_entry == -1:
        return None
    return _literal_eval(text[start : last_complete_entry + 1] + "}")


def _literal_eval(text: str) -> Optional[dict]:
    """Parse a Python/JSON dict literal without executing arbitrary code."""
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            return None
    return value if isinstance(value, dict) else None


def _parse_cluster_response(response: str, label: str) -> Dict[str, Any]:
    """
    Extract the module tree from a clustering reply, tolerating truncation.

    Returns ``{}`` when nothing usable can be recovered; the caller falls back
    to whole-module documentation in that case.
    """
    if not response or not response.strip():
        logger.warning("Empty LLM clustering response for %s.", label)
        return {}

    body = _strip_reasoning(response)
    if "<GROUPED_COMPONENTS>" not in body:
        logger.warning(
            "Invalid LLM clustering response for %s: missing <GROUPED_COMPONENTS> "
            "tags. This usually means the reply hit the output token limit before "
            "the answer began — raise --max-tokens or lower --max-token-per-module. "
            "Response preview: %s...",
            label,
            response[:200],
        )
        return {}

    content = body.split("<GROUPED_COMPONENTS>", 1)[1]
    if "</GROUPED_COMPONENTS>" in content:
        content = content.split("</GROUPED_COMPONENTS>", 1)[0]
        module_tree = _literal_eval(content.strip()) or _salvage_truncated_dict(content)
    else:
        logger.warning(
            "Clustering response for %s is missing the closing </GROUPED_COMPONENTS> "
            "tag — the model ran out of output tokens. Recovering the module groups "
            "that completed.",
            label,
        )
        module_tree = _salvage_truncated_dict(content)

    if module_tree is None:
        logger.warning(
            "Could not parse the clustering response for %s. Response preview: %s...",
            label,
            response[:200],
        )
        return {}

    # Keep only entries that actually name components; a truncated or sloppy
    # reply can leave behind placeholders that would break downstream recursion.
    valid: Dict[str, Any] = {}
    for module_name, module_info in module_tree.items():
        if not isinstance(module_name, str) or not isinstance(module_info, dict):
            logger.warning("Skipping malformed module entry %r for %s", module_name, label)
            continue
        component_ids = module_info.get("components")
        if not isinstance(component_ids, list) or not component_ids:
            logger.warning("Skipping module '%s' for %s - no components listed", module_name, label)
            continue
        module_info["components"] = [c for c in component_ids if isinstance(c, str)]
        module_info.setdefault("path", "")
        valid[module_name] = module_info

    return valid


def _merge_module_trees(trees: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Union module trees from separate batches, joining modules by name."""
    merged: Dict[str, Any] = {}
    for tree in trees:
        for module_name, module_info in tree.items():
            existing = merged.get(module_name)
            if existing is None:
                merged[module_name] = dict(module_info)
                merged[module_name]["components"] = list(module_info["components"])
                continue
            seen = set(existing["components"])
            for component_id in module_info["components"]:
                if component_id not in seen:
                    existing["components"].append(component_id)
                    seen.add(component_id)
    return merged


def get_clustering_input_token_count(
    leaf_nodes: List[str], components: Dict[str, Node]
) -> int:
    """Count the tokens used to decide whether a module needs clustering."""
    _, potential_core_components_with_code = format_potential_core_components(
        leaf_nodes, components
    )
    return count_tokens(potential_core_components_with_code)


def cluster_modules(
    leaf_nodes: List[str],
    components: Dict[str, Node],
    config: Config,
    current_module_tree: dict[str, Any] = {},
    current_module_name: str = None,
    current_module_path: List[str] = [],
    completer: Optional[Completer] = None,
) -> Dict[str, Any]:
    """
    Cluster the potential core components into modules.

    Args:
        completer: optional ``(prompt: str) -> str`` callable.  When provided,
            clustering calls go through this completer instead of the legacy
            ``call_llm``.  This is how the LLMBackend abstraction injects
            subscription-mode (caw) routing.  If ``None``, falls back to
            ``call_llm`` for backward compatibility with direct callers.
    """
    potential_core_components, potential_core_components_with_code = (
        format_potential_core_components(leaf_nodes, components)
    )
    input_tokens = count_tokens(potential_core_components_with_code)
    threshold = config.max_token_per_module
    module_label = current_module_name or "repository"

    logger.info(
        "Module clustering input for %s: %d leaf nodes, %d tokens, threshold %d",
        module_label,
        len(leaf_nodes),
        input_tokens,
        threshold,
    )

    if input_tokens <= threshold:
        logger.info(
            "Skipping LLM module clustering for %s because %d tokens fit within the "
            "%d-token threshold; using whole-module documentation mode.",
            module_label,
            input_tokens,
            threshold,
        )
        if current_module_name is None:
            leaf_files = {
                components[leaf_node].relative_path
                for leaf_node in leaf_nodes
                if leaf_node in components
            }
            all_files = {c.relative_path for c in components.values()}
            if all_files and len(leaf_files) / len(all_files) < LOW_COVERAGE_RATIO:
                logger.warning(
                    "Leaf-node entry points cover only %d of %d parsed files (%.0f%%). "
                    "Whole-repository documentation will start from these entry points and "
                    "rely on agent exploration to reach the rest of the codebase.",
                    len(leaf_files),
                    len(all_files),
                    100 * len(leaf_files) / len(all_files),
                )
        return {}

    logger.info(
        "Requesting LLM module clustering for %s because %d tokens exceed the %d-token threshold.",
        module_label,
        input_tokens,
        threshold,
    )

    # The reply must repeat every component ID we send, so a listing that is
    # large relative to the output budget gets truncated mid-answer.  Split it
    # into batches that comfortably fit and merge the resulting modules.
    budget = _cluster_input_token_budget(config)
    listing_tokens = count_tokens(potential_core_components)
    if listing_tokens <= budget:
        batches = [leaf_nodes]
    else:
        batches = _chunk_leaf_nodes(leaf_nodes, components, budget)
        logger.info(
            "Component listing for %s is %d tokens, above the %d-token per-request "
            "budget (max_tokens=%d); splitting clustering into %d batches so the "
            "reply cannot be truncated.",
            module_label,
            listing_tokens,
            budget,
            config.max_tokens,
            len(batches),
        )

    batch_trees: List[Dict[str, Any]] = []
    for batch_index, batch in enumerate(batches):
        label = (
            module_label
            if len(batches) == 1
            else f"{module_label} (batch {batch_index + 1}/{len(batches)})"
        )
        batch_components, _ = format_potential_core_components(batch, components)
        prompt = format_cluster_prompt(batch_components, current_module_tree, current_module_name)
        try:
            if completer is not None:
                response = completer(prompt)
            else:
                response = call_llm(prompt, config, model=config.cluster_model)
            batch_tree = _parse_cluster_response(response, label)
        except Exception as e:
            # One failed batch shouldn't cost us the groups the others found.
            logger.warning("LLM module clustering failed for %s: %s", label, e)
            logger.debug("Traceback: %s", traceback.format_exc())
            if len(batches) == 1:
                raise
            continue
        if batch_tree:
            batch_trees.append(batch_tree)

    module_tree = _merge_module_trees(batch_trees)
    if not module_tree:
        logger.warning(
            "LLM module clustering produced no usable modules for %s; falling back "
            "to whole-module documentation.",
            module_label,
        )
        return {}

    # check if the module tree is valid
    if len(module_tree) <= 1:
        logger.info(
            "Skipping LLM clustering result for %s because it produced only "
            "%d module(s); using whole-module documentation mode.",
            module_label,
            len(module_tree),
        )
        return {}

    logger.info(
        "LLM module clustering for %s produced %d top-level modules.",
        module_label,
        len(module_tree),
    )

    if current_module_tree == {}:
        current_module_tree = module_tree
    else:
        value = current_module_tree
        for key in current_module_path:
            value = value[key]["children"]
        for module_name, module_info in module_tree.items():
            module_info.pop("path", None)
            value[module_name] = module_info

    for module_name, module_info in module_tree.items():
        sub_leaf_nodes = module_info.get("components", [])
        
        # Filter sub_leaf_nodes to ensure they exist in components
        valid_sub_leaf_nodes = []
        for node in sub_leaf_nodes:
            if node in components:
                valid_sub_leaf_nodes.append(node)
            else:
                logger.warning(f"Skipping invalid sub leaf node '{node}' in module '{module_name}' - not found in components")
        
        current_module_path.append(module_name)
        module_info["children"] = {}
        module_info["children"] = cluster_modules(
            valid_sub_leaf_nodes,
            components,
            config,
            current_module_tree,
            module_name,
            current_module_path,
            completer=completer,
        )
        current_module_path.pop()

    return module_tree
