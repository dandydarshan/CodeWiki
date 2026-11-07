"""Prompts never silently drop a module's code, and budgets follow the model's window."""

from types import SimpleNamespace

import pytest

import codewiki.src.be.prompt_template as pt
from codewiki.src.be import overview_support as support
from codewiki.src.be.context_budget import prompt_budget
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.module_scope import EXTRA_DEPTH_TO_FIT, can_split_module
from codewiki.src.be.utils import count_tokens


def _node(tmp_path, rel, source, depends_on=()):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    name = rel.rsplit("/", 1)[-1].split(".")[0].title()
    return Node(
        id=f"{rel}::{name}",
        name=name,
        component_type="class",
        file_path=str(path),
        relative_path=rel,
        depends_on=set(depends_on),
        source_code=source,
    )


@pytest.fixture
def repo(tmp_path):
    """billing uses payments; reports is unrelated; billing has a child, invoices."""
    nodes = [
        _node(tmp_path, "billing/core.py", "class Core: pass\n", ["payments/pay.py::Pay"]),
        _node(tmp_path, "billing/inv.py", "class Inv: pass\n"),
        _node(tmp_path, "payments/pay.py", "class Pay: pass\n"),
        _node(tmp_path, "reports/rep.py", "class Rep: pass\n"),
        _node(tmp_path, "admin/adm.py", "class Adm: pass\n", ["billing/core.py::Core"]),
    ]
    components = {n.id: n for n in nodes}
    tree = {
        "billing": {
            "components": ["billing/core.py::Core"],
            "children": {"invoices": {"components": ["billing/inv.py::Inv"], "children": {}}},
        },
        "payments": {"components": ["payments/pay.py::Pay"], "children": {}},
        "reports": {"components": ["reports/rep.py::Rep"], "children": {}},
        "admin": {"components": ["admin/adm.py::Adm"], "children": {}},
    }
    return components, tree


def test_tree_details_neighbours_and_dependencies_only(repo):
    components, tree = repo
    prompt = pt.format_user_prompt(
        "invoices", ["billing/inv.py::Inv"], components, tree, module_path=["billing", "invoices"]
    )
    tree_text = prompt.split("<MODULE_TREE>")[1].split("</MODULE_TREE>")[0]
    assert (
        "billing/inv.py: Inv" in tree_text and "billing/core.py: Core" in tree_text
    )  # self, parent
    assert "payments/pay.py" not in tree_text and "reports/rep.py" not in tree_text
    assert "payments" in tree_text and "reports" in tree_text  # still listed by name

    related = pt.related_module_paths(tree, ["billing"], ["billing/core.py::Core"], components)
    # Uses payments, is used by admin, and has a child; reports stays name-only.
    assert {("billing",), ("billing", "invoices"), ("payments",), ("admin",)} <= related
    assert ("reports",) in related  # a sibling at the top level
    deep = pt.related_module_paths(
        tree, ["billing", "invoices"], ["billing/inv.py::Inv"], components
    )
    assert ("reports",) not in deep and ("payments",) not in deep


def test_files_that_do_not_fit_are_listed_whole_never_cut(tmp_path, monkeypatch):
    big = "x = 1\n" * 4000
    nodes = [_node(tmp_path, f"m/f{i}.py", f"# file {i}\n" + big) for i in range(3)]
    nodes.append(_node(tmp_path, "m/small.py", "# small file\nclass Small: pass\n"))
    components = {n.id: n for n in nodes}
    tree = {"m": {"components": list(components), "children": {}}}
    monkeypatch.setattr(pt, "MAX_USER_PROMPT_CHARS", 60_000)
    prompt = pt.format_user_prompt("m", list(components), components, tree, module_path=["m"])

    assert len(prompt) <= 60_000
    assert pt.FILES_NOT_INLINED_NOTE in prompt
    for node in nodes:
        rel = node.relative_path
        inlined = f"# File: {rel}\n" in prompt
        listed = f"- {rel}: {node.id}" in prompt
        assert inlined != listed, rel  # every file exactly one way
        if inlined:
            assert node.source_code in prompt  # whole, never cut
    assert "# small file" in prompt  # the small file is kept, the big ones go first


def test_token_budget_applies_when_window_is_known(tmp_path):
    nodes = [_node(tmp_path, f"m/f{i}.py", "value = 1\n" * 3000) for i in range(3)]
    components = {n.id: n for n in nodes}
    tree = {"m": {"components": list(components), "children": {}}}
    prompt = pt.format_user_prompt(
        "m", list(components), components, tree, module_path=["m"], max_tokens=15_000
    )
    assert count_tokens(prompt) <= 15_000
    assert pt.FILES_NOT_INLINED_NOTE in prompt


def _config(**overrides):
    values = dict(
        max_depth=1,
        min_token_per_module=1,
        max_token_per_leaf_module=1,
        max_context_tokens=0,
        max_tokens=0,
        main_model="m",
        llm_base_url="",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_prompt_budget_from_window():
    assert prompt_budget(_config()) == 0  # unknown window
    budget = prompt_budget(_config(max_context_tokens=10_000, max_tokens=1_000))
    assert budget == int((10_000 - 1_000) * 0.9 * 0.6)


def test_module_too_big_for_one_prompt_splits_past_max_depth(tmp_path):
    source = "value = 1\n" * 2000
    nodes = [_node(tmp_path, f"m/f{i}.py", source) for i in range(2)]
    components = {n.id: n for n in nodes}
    ids = list(components)
    small_window = _config(max_context_tokens=4_000)
    assert can_split_module(components, ids, small_window, depth=1)
    assert not can_split_module(components, ids, small_window, depth=1 + EXTRA_DEPTH_TO_FIT)
    assert not can_split_module(components, ids, _config(max_context_tokens=1_000_000), depth=1)


def test_overview_condenses_large_children_before_clipping(monkeypatch):
    section = "Details of this part. " * 400
    children = {
        f"child{i}": (
            f"# Child {i}\n\nWhat child {i} does.\n\n{section}\n\n## Part A\n\nPart A intro.\n\n"
            f"{section}\n\n<!-- codewiki:summary\nChild {i} summary.\n-->\n"
        )
        for i in range(4)
    }
    prompt = support.bounded_overview_prompt("Structure", children, "System", budget_tokens=3_000)
    assert count_tokens("System" + prompt) <= 3_000
    for i in range(4):
        assert f"Child {i} summary." in prompt and f"What child {i} does." in prompt
    assert "Part A intro." in prompt and "Excerpt truncated" not in prompt

    small = {"child": "# Child\n\nShort page."}
    full = support.bounded_overview_prompt("Structure", small, "System", budget_tokens=3_000)
    assert "Short page." in full and "Summary:" not in full  # fits: sent whole


def test_page_writing_prompts_ask_for_a_summary():
    for template in (pt.SYSTEM_PROMPT, pt.LEAF_SYSTEM_PROMPT, pt.MODULE_OVERVIEW_PROMPT):
        assert "codewiki:summary" in template
    assert "codewiki:summary" not in pt.REPO_OVERVIEW_PROMPT  # the root has no parent
