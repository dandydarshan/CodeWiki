"""Tests for OS-independent component IDs.

On Windows, ``os.path.relpath`` yields ``apps\\billing-ui\\x.ts`` while LLMs echo
IDs back as ``apps/billing-ui/x.ts``; every lookup by an LLM-supplied ID then
missed ("Skipping invalid leaf node ... not found in components").
"""

import asyncio
import ntpath
from types import SimpleNamespace

import codewiki.src.be.dependency_analyzer.utils.paths as paths
from codewiki.src.be.agent_tools.read_code_components import read_code_components
from codewiki.src.be.dependency_analyzer.analyzers.typescript import TreeSitterTSAnalyzer
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.dependency_analyzer.utils.paths import (
    normalize_component_id,
    normalize_component_ids,
    normalize_module_tree_ids,
    repo_relpath,
)


def _as_windows(monkeypatch) -> None:
    monkeypatch.setattr(paths, "os", SimpleNamespace(sep="\\", path=ntpath))


def test_repo_relpath_uses_forward_slashes_on_windows(monkeypatch) -> None:
    _as_windows(monkeypatch)
    assert (
        repo_relpath(r"C:\dev\repo\apps\billing-ui\src\main.ts", r"C:\dev\repo")
        == "apps/billing-ui/src/main.ts"
    )


def test_repo_relpath_on_another_drive_keeps_full_path(monkeypatch) -> None:
    _as_windows(monkeypatch)
    assert repo_relpath(r"D:\other\main.ts", r"C:\dev\repo") == "D:/other/main.ts"


def test_repo_relpath_posix(tmp_path) -> None:
    assert repo_relpath(tmp_path / "src" / "a.py", tmp_path) == "src/a.py"
    assert repo_relpath("src/a.py") == "src/a.py"


def test_analyzer_component_ids_use_forward_slashes(monkeypatch) -> None:
    _as_windows(monkeypatch)
    analyzer = TreeSitterTSAnalyzer.__new__(TreeSitterTSAnalyzer)
    analyzer.file_path = r"C:\dev\repo\apps\billing-ui\otc-list.service.ts"
    analyzer.repo_path = r"C:\dev\repo"
    assert (
        analyzer._get_component_id("BUIOTCListService")
        == "apps/billing-ui/otc-list.service.ts::BUIOTCListService"
    )


def test_normalize_component_id_only_touches_the_path_part() -> None:
    assert normalize_component_id(r"apps\ui\a.ts::A.b") == "apps/ui/a.ts::A.b"
    assert normalize_component_id("src/a.rs::mod::f") == "src/a.rs::mod::f"
    assert normalize_component_id("pkg.module.Class") == "pkg.module.Class"


def test_normalize_component_ids_dedupes_mixed_separators() -> None:
    assert normalize_component_ids([r"a\b.ts::X", "a/b.ts::X", "a/c.ts::Y"]) == [
        "a/b.ts::X",
        "a/c.ts::Y",
    ]


def test_normalize_module_tree_ids_rewrites_nested_trees() -> None:
    tree = {
        "otc": {
            "components": [r"apps\otc\list.ts::List"],
            "children": {"otc_list": {"components": [r"apps\otc\svc.ts::Svc"], "children": {}}},
        }
    }
    assert normalize_module_tree_ids(tree)
    assert tree["otc"]["components"] == ["apps/otc/list.ts::List"]
    assert tree["otc"]["children"]["otc_list"]["components"] == ["apps/otc/svc.ts::Svc"]
    assert not normalize_module_tree_ids(tree)


def test_read_code_components_accepts_backslash_ids() -> None:
    node = Node(
        id="apps/otc/list.ts::List",
        name="List",
        component_type="class",
        file_path="apps/otc/list.ts",
        relative_path="apps/otc/list.ts",
        source_code="class List {}",
    )
    ctx = SimpleNamespace(deps=SimpleNamespace(components={node.id: node}))
    result = asyncio.run(read_code_components(ctx, [r"apps\otc\list.ts::List"]))
    assert "class List {}" in result
    assert "not found" not in result
