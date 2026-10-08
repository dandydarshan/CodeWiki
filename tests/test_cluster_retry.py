"""Clustering survives an endpoint that drops requests or omits the answer tags."""

from __future__ import annotations

import re

import httpx

from codewiki.src.be.cluster_modules import (
    MAX_TRANSPORT_FAILURES,
    cluster_modules,
    get_clustering_input_token_count,
)
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.config import Config

IDS = [f"d{d}/f{f}.py::C{d}_{f}" for d in range(8) for f in range(25)]
DIRS = {f"d{d}" for d in range(8)}


def _components(ids: list[str]) -> dict[str, Node]:
    nodes = {}
    for cid in ids:
        rel_path, _, name = cid.partition("::")
        nodes[cid] = Node(
            id=cid, name=name, component_type="class", file_path=rel_path,
            relative_path=rel_path, source_code="code",
        )
    return nodes


COMPONENTS = _components(IDS)
# One directory's code fits the whole-module threshold; two do not.
THRESHOLD = get_clustering_input_token_count([i for i in IDS if i.startswith("d0/")], COMPONENTS)


def _config() -> Config:
    return Config.from_cli(
        repo_path="/tmp/repo", output_dir="/tmp/out", llm_base_url="http://localhost",
        llm_api_key="key", main_model="m", cluster_model="m", max_token_per_module=THRESHOLD,
    )


def _gateway(calls: list[int], drop_above: int, tagged: bool = True):
    """Completer that, like a flaky gateway, drops requests over a size."""

    def complete(prompt: str):
        ids = re.findall(r"^\t(\S+::\S+)$", prompt, flags=re.M)
        calls.append(len(ids))
        if len(ids) > drop_above:
            raise httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body "
                "(incomplete chunked read)"
            )
        by_dir: dict[str, list[str]] = {}
        for cid in ids:
            by_dir.setdefault(cid.split("/")[0], []).append(cid)
        tree = {f"llm_{d}": {"path": d, "components": c} for d, c in by_dir.items()}
        if tagged:
            return f"<GROUPED_COMPONENTS>{tree!r}</GROUPED_COMPONENTS>"
        return f"I grouped the components by feature.\n\n```python\n{tree!r}\n```\n"

    return complete


def _cluster(completer):
    return cluster_modules(IDS, COMPONENTS, _config(), {}, None, [], completer=completer)


def _covered(tree):
    return sorted(cid for module in tree.values() for cid in module["components"])


def test_dropped_batch_is_split_once_and_retried():
    calls: list[int] = []
    tree = _cluster(_gateway(calls, drop_above=150))
    assert calls == [200, 100, 100]
    assert set(tree) == {f"llm_{d}" for d in DIRS}
    assert _covered(tree) == sorted(IDS)


def test_unreliable_endpoint_falls_back_to_directory_modules_after_bounded_failures():
    calls: list[int] = []
    tree = _cluster(_gateway(calls, drop_above=0))
    # the whole batch, then the first half: no more requests after the limit
    assert calls == [200, 100] and len(calls) == MAX_TRANSPORT_FAILURES
    assert set(tree) == DIRS
    assert _covered(tree) == sorted(IDS)
    for name, module in tree.items():
        assert all(cid.startswith(name + "/") for cid in module["components"])


def test_failure_count_resets_for_each_run():
    _cluster(_gateway([], drop_above=0))
    calls: list[int] = []
    _cluster(_gateway(calls, drop_above=1000))
    assert calls == [200]


def test_response_without_tags_uses_the_module_dict_in_it():
    tree = _cluster(_gateway([], drop_above=1000, tagged=False))
    assert set(tree) == {f"llm_{d}" for d in DIRS}
    assert _covered(tree) == sorted(IDS)


def test_batch_ids_leave_room_for_other_tokenizers():
    """IDs may use a quarter of max_tokens: a Claude answer took ~2.6x their count."""
    from codewiki.src.be.cluster_modules import _cluster_batch_fits
    from codewiki.src.be.utils import count_tokens

    config = Config.from_cli(
        repo_path="/tmp/repo", output_dir="/tmp/out", llm_base_url="http://localhost",
        llm_api_key="key", main_model="m", cluster_model="m", max_tokens=32768,
        max_leaf_nodes_per_cluster=10_000,
    )
    ids = [f"src/app/widgets/w{i}/w{i}.component.ts::Widget{i}Component" for i in range(2000)]

    def largest_fitting() -> list[str]:
        lo, hi = 1, len(ids)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            lo, hi = (mid, hi) if _cluster_batch_fits(ids[:mid], config) else (lo, mid - 1)
        return ids[:lo]

    batch = largest_fitting()
    tokens = count_tokens("\n".join(batch))
    assert tokens <= 32768 // 4
    # at the ~2.6x a Claude answer took, it stays well under max_tokens
    assert tokens * 2.6 < 32768 * 0.7
