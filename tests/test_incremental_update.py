"""Tests for `codewiki generate --update` incremental regeneration."""

import json
import subprocess
from pathlib import Path

import pytest

from codewiki.src.be import incremental


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


class _Node:
    """Stand-in for a dependency-graph node."""

    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path


@pytest.fixture
def documented_repo(tmp_path: Path):
    """A git repo with one previous generation already recorded in ./docs."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/a.py").write_text("class A: pass\n", encoding="utf-8")
    (repo / "src/b.py").write_text("class B: pass\n", encoding="utf-8")

    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")

    docs = repo / "docs"
    docs.mkdir()
    module_tree = {
        "mod_a": {"components": ["src/a.py::A"], "children": {}},
        "mod_b": {
            "components": ["src/b.py::B"],
            "children": {"mod_b_inner": {"components": ["src/b.py::B"], "children": {}}},
        },
    }
    first_tree = {
        name: {"components": info["components"], "children": {}}
        for name, info in module_tree.items()
    }
    (docs / "module_tree.json").write_text(json.dumps(module_tree), encoding="utf-8")
    (docs / "first_module_tree.json").write_text(json.dumps(first_tree), encoding="utf-8")
    for name in ("mod_a", "mod_b", "mod_b_inner", "overview"):
        (docs / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")
    (docs / "metadata.json").write_text(
        json.dumps(
            {
                "generation_info": {
                    "commit_id": _git(repo, "rev-parse", "HEAD"),
                    "timestamp": "2020-01-01T00:00:00",
                }
            }
        ),
        encoding="utf-8",
    )
    return repo, docs, module_tree, first_tree


def test_no_changes_reports_nothing(documented_repo):
    repo, docs, _, _ = documented_repo
    changes = incremental.detect_changed_files(repo, docs)
    assert changes is not None
    assert changes.files == []


def test_uncommitted_edits_are_detected(documented_repo):
    """The baseline is HEAD, so a plain edit used to look like "up to date"."""
    repo, docs, _, _ = documented_repo
    (repo / "src/a.py").write_text("class A:\n    x = 1\n", encoding="utf-8")

    changes = incremental.detect_changed_files(repo, docs)
    assert changes.method == "git"
    assert changes.files == ["src/a.py"]


def test_untracked_files_are_detected(documented_repo):
    repo, docs, _, _ = documented_repo
    (repo / "src/c.py").write_text("class C: pass\n", encoding="utf-8")

    changes = incremental.detect_changed_files(repo, docs)
    assert "src/c.py" in changes.files


def test_generated_docs_do_not_count_as_source_changes(documented_repo):
    repo, docs, _, _ = documented_repo
    (docs / "mod_a.md").write_text("# regenerated\n", encoding="utf-8")

    changes = incremental.detect_changed_files(repo, docs)
    assert changes.files == []


def test_missing_baseline_falls_back_to_mtime(documented_repo):
    repo, docs, _, _ = documented_repo
    (docs / "metadata.json").write_text(
        json.dumps({"generation_info": {"timestamp": "2020-01-01T00:00:00"}}),
        encoding="utf-8",
    )

    changes = incremental.detect_changed_files(repo, docs)
    assert changes.method == "mtime"
    assert set(changes.files) == {"src/a.py", "src/b.py"}


def test_no_previous_generation_returns_none(tmp_path, documented_repo):
    repo, _, _, _ = documented_repo
    assert incremental.detect_changed_files(repo, tmp_path / "empty-docs") is None


def test_cached_tree_is_reused_while_the_file_set_holds(documented_repo):
    """Editing a file must not trigger a (paid) re-clustering."""
    _, _, _, first_tree = documented_repo
    components = {"src/a.py::A": _Node("src/a.py"), "src/b.py::B": _Node("src/b.py")}

    assert incremental.module_tree_rebuild_reason(
        first_tree, list(components), components
    ) is None


def test_new_file_forces_a_tree_rebuild(documented_repo):
    """A file no module knows about would otherwise never be documented."""
    _, _, _, first_tree = documented_repo
    components = {
        "src/a.py::A": _Node("src/a.py"),
        "src/b.py::B": _Node("src/b.py"),
        "src/c.py::C": _Node("src/c.py"),
    }

    reason = incremental.module_tree_rebuild_reason(first_tree, list(components), components)
    assert reason and "new source file" in reason


def test_deleted_file_forces_a_tree_rebuild(documented_repo):
    _, _, _, first_tree = documented_repo
    components = {"src/a.py::A": _Node("src/a.py")}

    reason = incremental.module_tree_rebuild_reason(first_tree, list(components), components)
    assert reason and "no longer exist" in reason


def test_plan_rebuilds_only_the_changed_module(documented_repo):
    _, docs, module_tree, first_tree = documented_repo

    plan = incremental.plan_incremental_update(
        module_tree, first_tree, ["src/a.py"], str(docs)
    )

    assert plan.regenerate == {"mod_a"}
    assert plan.reuse == {"mod_b"}
    assert plan.removed == set()


def test_plan_is_a_noop_when_nothing_changed(documented_repo):
    _, docs, module_tree, first_tree = documented_repo

    plan = incremental.plan_incremental_update(module_tree, first_tree, [], str(docs))

    assert plan.is_noop
    assert plan.reuse == {"mod_a", "mod_b"}


def test_plan_rebuilds_a_module_whose_doc_is_missing(documented_repo):
    _, docs, module_tree, first_tree = documented_repo
    (docs / "mod_b.md").unlink()

    plan = incremental.plan_incremental_update(module_tree, first_tree, [], str(docs))

    assert plan.regenerate == {"mod_b"}
    assert plan.reasons["mod_b"] == "documentation missing"


def test_rebuilding_a_module_drops_its_sub_module_docs(documented_repo):
    """Leaving them behind makes their names look taken, yielding "<name>_2.md"."""
    _, docs, module_tree, first_tree = documented_repo

    plan = incremental.plan_incremental_update(
        module_tree, first_tree, ["src/b.py"], str(docs)
    )
    assert plan.regenerate == {"mod_b"}
    assert plan.removed == {"mod_b_inner"}

    removed = incremental.apply_update_plan(plan, str(docs))

    assert set(removed) == {"mod_b.md", "mod_b_inner.md", "overview.md"}
    assert sorted(p.name for p in docs.glob("*.md")) == ["mod_a.md"]


def test_reused_modules_keep_their_docs_and_branches(documented_repo):
    _, docs, module_tree, first_tree = documented_repo

    plan = incremental.plan_incremental_update(
        module_tree, first_tree, ["src/a.py"], str(docs)
    )
    incremental.apply_update_plan(plan, str(docs))
    merged = incremental.carry_over_children(first_tree, module_tree, plan.reuse)

    assert (docs / "mod_b.md").exists()
    assert (docs / "mod_b_inner.md").exists()
    # The flat clustered tree would otherwise drop the sub-module the previous
    # run discovered, orphaning mod_b_inner.md.
    assert set(merged["mod_b"]["children"]) == {"mod_b_inner"}


def test_modules_that_disappeared_are_cleaned_up(documented_repo):
    _, docs, module_tree, _ = documented_repo
    new_tree = {"mod_a": {"components": ["src/a.py::A"], "children": {}}}

    plan = incremental.plan_incremental_update(module_tree, new_tree, [], str(docs))

    assert plan.removed == {"mod_b", "mod_b_inner"}
    incremental.apply_update_plan(plan, str(docs))
    assert sorted(p.name for p in docs.glob("*.md")) == ["mod_a.md"]


def test_reclustering_keeps_previous_module_names(documented_repo):
    """Names are doc filenames: renaming them all would force a full rebuild."""
    _, _, module_tree, _ = documented_repo
    reclustered = {
        "core_a": {"components": ["src/a.py::A"], "children": {}},
        "core_b": {"components": ["src/b.py::B"], "children": {}},
        "brand_new": {"components": ["src/c.py::C"], "children": {}},
    }

    aligned = incremental.align_module_names(module_tree, reclustered)

    assert set(aligned) == {"mod_a", "mod_b", "brand_new"}


def test_affected_modules_match_on_file_paths(documented_repo):
    """Component IDs are "path::Symbol"; naive substring matching missed them."""
    _, _, module_tree, _ = documented_repo

    affected, cascade = incremental.find_affected_modules(module_tree, ["src/b.py"])

    assert affected == {"mod_b", "mod_b_inner"}
    assert cascade == {"mod_b", "overview"}
