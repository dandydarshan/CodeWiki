"""Previous-graph lookup when the repo was analysed from differently named checkouts."""

from __future__ import annotations

import json
import os
import time

from codewiki.src.be.updater.graph_store import (
    find_any_graph_file,
    graph_file_path,
    prune_superseded_graphs,
    snapshot_old_graph,
)


def _write_graph(d, name, payload):
    path = os.path.join(d, f"{name}_dependency_graph.json")
    with open(path, "w") as f:
        json.dump(payload, f)
    return path


def test_single_candidate_is_used_whatever_its_name(tmp_path):
    d = str(tmp_path)
    old = _write_graph(d, "rq3_svelte_anchor", {"a": 1})
    assert find_any_graph_file(d) == old
    prev = snapshot_old_graph(d, "/repos/rq3-svelte-w1")
    assert prev is not None and prev.endswith("rq3_svelte_w1_dependency_graph.prev.json")
    assert json.load(open(prev)) == {"a": 1}


def test_several_candidates_pick_newest_not_none(tmp_path):
    d = str(tmp_path)
    older = _write_graph(d, "rq3_svelte_anchor", {"rev": "anchor"})
    time.sleep(0.01)
    newer = _write_graph(d, "rq3_svelte_w1", {"rev": "w1"})
    os.utime(older, (1, 1))  # make the order unambiguous
    assert find_any_graph_file(d) == newer
    prev = snapshot_old_graph(d, "/repos/rq3-svelte-w2")
    assert json.load(open(prev)) == {"rev": "w1"}


def test_prev_snapshots_are_not_candidates(tmp_path):
    d = str(tmp_path)
    _write_graph(d, "rq3_svelte_w1", {"rev": "w1"})
    with open(os.path.join(d, "rq3_svelte_w1_dependency_graph.prev.json"), "w") as f:
        json.dump({"rev": "anchor"}, f)
    assert find_any_graph_file(d).endswith("rq3_svelte_w1_dependency_graph.json")


def test_prune_keeps_current_and_prev_only(tmp_path):
    d = str(tmp_path)
    _write_graph(d, "rq3_svelte_anchor", {})
    _write_graph(d, "rq3_svelte_w1", {})
    current = _write_graph(d, "rq3_svelte_w2", {})
    with open(os.path.join(d, "rq3_svelte_w2_dependency_graph.prev.json"), "w") as f:
        json.dump({}, f)
    removed = prune_superseded_graphs(d, keep=graph_file_path(d, "/repos/rq3-svelte-w2"))
    assert sorted(os.path.basename(r) for r in removed) == [
        "rq3_svelte_anchor_dependency_graph.json",
        "rq3_svelte_w1_dependency_graph.json",
    ]
    assert sorted(os.listdir(d)) == [
        "rq3_svelte_w2_dependency_graph.json",
        "rq3_svelte_w2_dependency_graph.prev.json",
    ]
    assert os.path.exists(current)


def _git(repo, *args):
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _config(repo):
    from codewiki.src.config import Config

    return Config(
        repo_path=str(repo),
        output_dir=str(repo / "docs" / "temp"),
        dependency_graph_dir=str(repo / "docs" / "temp" / "dependency_graphs"),
        docs_dir=str(repo / "docs"),
        max_depth=2,
        llm_base_url="x",
        llm_api_key="x",
        main_model="x",
        cluster_model="x",
    )


def test_previous_graph_is_rebuilt_from_the_docs_commit(tmp_path):
    """docs/temp/ is git-ignored, so a fresh clone must rebuild the graph `--update` diffs against."""
    from codewiki.src.be.updater.graph_store import load_graph, rebuild_graph_at_commit

    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("def old_func():\n    return 1\n")
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "docs commit")
    docs_commit = _git(repo, "rev-parse", "HEAD")
    (repo / "pkg" / "a.py").write_text("def new_func():\n    return 2\n")
    _git(repo, "commit", "-qam", "later")

    path = rebuild_graph_at_commit(_config(repo), docs_commit)

    names = {cid.rsplit("::", 1)[-1] for cid in load_graph(path)}
    assert "old_func" in names and "new_func" not in names  # the docs commit, not HEAD
    assert path.endswith(".prev.json")
    assert _git(repo, "worktree", "list").count("\n") == 0  # temporary worktree removed
    assert sorted(os.listdir(repo / "docs" / "temp")) == [
        "dependency_graphs"
    ]  # nothing else written
    assert (repo / "pkg" / "a.py").read_text().startswith("def new_func")  # checkout untouched


def test_unknown_docs_commit_falls_back_quietly(tmp_path):
    from codewiki.src.be.updater.graph_store import rebuild_graph_at_commit

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c")
    assert rebuild_graph_at_commit(_config(repo), "0" * 40) is None  # e.g. a shallow CI clone


def test_graph_writes_are_atomic(tmp_path):
    """Two IDEs' MCP servers (or serve and generate) may write the same graph at once."""
    import pytest

    from codewiki.src.utils import file_manager

    path = tmp_path / "repo_dependency_graph.json"
    file_manager.save_json({"ok": 1}, str(path), indent=2)
    with pytest.raises(TypeError):
        file_manager.save_json({"a": "x" * 100_000, "b": object()}, str(path), indent=2)
    assert json.loads(path.read_text()) == {"ok": 1}  # a failed write never leaves half a file
    assert os.listdir(tmp_path) == [path.name]  # nor its temp file


def test_graph_is_committed_with_the_docs_but_scratch_is_not(tmp_path):
    """The graph travels with the docs (so `--update` on any branch reuses them); scratch does not."""
    from pathlib import Path

    from codewiki.src.be.dependency_analyzer import DependencyGraphBuilder

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def f():\n    return 1\n")
    _git(tmp_path, "init", "-q", str(repo))
    config = _config(repo)
    DependencyGraphBuilder(config).build_dependency_graph()
    # git prints / even on Windows
    graph = Path(os.path.relpath(find_any_graph_file(config.dependency_graph_dir), repo)).as_posix()
    with open(
        os.path.join(config.dependency_graph_dir, "repo_dependency_graph.prev.json"), "w"
    ) as f:
        f.write("{}")
    _git(repo, "add", "-A")

    staged = _git(repo, "diff", "--cached", "--name-only").splitlines()
    assert graph in staged
    assert not any(p.endswith(".prev.json") for p in staged)
    attrs = _git(repo, "check-attr", "diff", "merge", "linguist-generated", "--", graph)
    # No 35 MB diffs or text merges (conflict markers inside JSON); collapsed in GitHub PRs.
    assert (
        "diff: unset" in attrs and "merge: unset" in attrs and "linguist-generated: true" in attrs
    )


def test_update_removes_its_scratch_copy_of_the_previous_graph(tmp_path, monkeypatch):
    import asyncio
    from pathlib import Path
    from types import SimpleNamespace

    import codewiki.src.be.updater.orchestrator as orchestrator
    from codewiki.cli.adapters.doc_generator import CLIDocumentationGenerator

    prev = tmp_path / "temp" / "dependency_graphs" / "repo_dependency_graph.prev.json"
    prev.parent.mkdir(parents=True)
    prev.write_text("{}")

    class Updater:
        def __init__(self, *a):
            pass

        async def run(self, prev_path, *a):
            assert os.path.exists(prev_path)  # still there while the diff runs
            return SimpleNamespace(outcome="full_fallback", summary=lambda: {})

    monkeypatch.setattr(orchestrator, "IncrementalUpdater", Updater)
    gen = object.__new__(CLIDocumentationGenerator)
    gen.output_dir, gen.repo_path, gen.commit_id, gen.verbose = (
        Path(tmp_path),
        tmp_path,
        "c2",
        False,
    )
    gen.progress_tracker = SimpleNamespace(start_stage=lambda *a: None, complete_stage=lambda: None)
    doc_generator = SimpleNamespace(
        backend=None, usage_ledger=SimpleNamespace(in_phase=lambda phase: _null_context())
    )
    asyncio.run(gen._run_incremental_update(None, doc_generator, None, str(prev), {}, []))
    assert not prev.exists()  # was half of a user's 85 MB docs folder


def _null_context():
    import contextlib

    return contextlib.nullcontext()


def _gen_for(docs):
    from types import SimpleNamespace

    from codewiki.cli.adapters.doc_generator import CLIDocumentationGenerator

    gen = object.__new__(CLIDocumentationGenerator)
    gen.output_dir, gen.commit_id, gen.notes = docs, "abcdef1234", []
    gen.progress_tracker = SimpleNamespace(note=gen.notes.append)
    return gen


def _repo_with_docs(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "overview.md").write_text("# Old\n")
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "docs")
    return repo


def test_full_rebuild_relies_on_git_for_committed_docs(tmp_path):
    from codewiki.cli.adapters.doc_generator import _committed_and_clean

    repo = _repo_with_docs(tmp_path)
    gen = _gen_for(repo / "docs")
    gen._docs_clean_at_start = _committed_and_clean(repo / "docs")  # as --update records it
    # The update itself writes into docs/ (graph, record, deleted pages) before falling back.
    (repo / "docs" / "update_record.json").write_text("{}")
    (repo / "docs" / "overview.md").unlink()
    gen._move_docs_aside()
    assert os.listdir(repo / "docs") == []  # cleared for the rebuild
    assert not list(repo.glob("docs.prev-*"))  # no 85 MB copy next to docs/
    assert "git restore --source=HEAD docs/" in gen.notes[0]
    _git(repo, "restore", "--source=HEAD", "docs/")
    assert (repo / "docs" / "overview.md").read_text() == "# Old\n"  # the advice works


def test_full_rebuild_backs_up_uncommitted_docs_out_of_git(tmp_path):
    repo = _repo_with_docs(tmp_path)
    (repo / "docs" / "overview.md").write_text("# Edited, not committed\n")
    (repo / "docs.prev-older").mkdir()  # a backup from an earlier full rebuild
    gen = _gen_for(repo / "docs")
    gen._move_docs_aside()
    backups = sorted(p.name for p in repo.glob("docs.prev-*"))
    assert backups == ["docs.prev-abcdef12"]  # only the latest copy is kept
    assert (repo / "docs.prev-abcdef12" / "overview.md").read_text() == "# Edited, not committed\n"
    status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    assert "docs.prev" not in status  # `git add .` will not commit it


def test_docs_in_the_repo_root_or_outside_git_are_never_deleted(tmp_path):
    from pathlib import Path

    from codewiki.cli.adapters.doc_generator import _committed_and_clean

    repo = _repo_with_docs(tmp_path)
    assert _committed_and_clean(repo / "docs")
    assert not _committed_and_clean(Path(repo))  # --output . must never wipe the checkout
    plain = tmp_path / "plain" / "docs"
    plain.mkdir(parents=True)
    assert not _committed_and_clean(plain)


def test_saved_graph_holds_no_absolute_paths(tmp_path):
    """The graph is committed with the docs; an absolute path would publish the checkout location."""
    from codewiki.src.be.dependency_analyzer import DependencyGraphBuilder
    from codewiki.src.be.updater.graph_store import load_graph, save_graph

    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("def f():\n    return 1\n")
    config = _config(repo)
    components, _ = DependencyGraphBuilder(config).build_dependency_graph()
    path = find_any_graph_file(config.dependency_graph_dir)

    assert str(tmp_path) not in open(path, encoding="utf-8").read()
    assert {n.file_path for n in load_graph(path).values()} == {"pkg/a.py"}
    # Generation keeps reading source through the in-memory absolute paths.
    assert all(os.path.isabs(c.file_path) for c in components.values())

    save_graph(components, str(tmp_path / "copy.json"))  # the updater's writer too
    assert str(tmp_path) not in (tmp_path / "copy.json").read_text(encoding="utf-8")


def test_codewikis_own_output_is_not_analysed_as_repo_code(tmp_path):
    """A second run used to add the copied helper script (~17 functions) to the user's code."""

    from codewiki.src.be.dependency_analyzer import DependencyGraphBuilder
    from codewiki.src.config import MODULE_TREE_OWNERSHIP_VALIDATOR, Config

    def analysed_files(repo, docs_dir):
        config = Config(
            repo_path=str(repo),
            output_dir=str(tmp_path / "out"),
            dependency_graph_dir=str(tmp_path / "out" / "graphs"),
            docs_dir=str(docs_dir),
            max_depth=2,
            llm_base_url="x",
            llm_api_key="x",
            main_model="x",
            cluster_model="x",
        )
        components, _ = DependencyGraphBuilder(config).build_dependency_graph()
        return {n.relative_path for n in components.values()}

    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("def f():\n    return 1\n")
    (repo / "docs" / "temp").mkdir(parents=True)
    # A stand-in with the helper's name: the tests may run from a copy outside the repo.
    script = "def check_tree(tree):\n    return tree\n\n\ndef main():\n    return check_tree({})\n"
    (repo / "docs" / MODULE_TREE_OWNERSHIP_VALIDATOR).write_text(script)  # as `generate` copies it
    (repo / "pkg" / MODULE_TREE_OWNERSHIP_VALIDATOR).write_text(script)  # a real source copy
    (repo / "docs" / "temp" / "cache.py").write_text("def cached():\n    return 1\n")
    (repo / "docs" / "conf.py").write_text("def setup(app):\n    return app\n")  # the user's own

    files = analysed_files(repo, repo / "docs")
    assert "pkg/a.py" in files and "docs/conf.py" in files
    assert f"pkg/{MODULE_TREE_OWNERSHIP_VALIDATOR}" in files
    assert f"docs/{MODULE_TREE_OWNERSHIP_VALIDATOR}" not in files
    assert not any(f.startswith("docs/temp/") for f in files)

    files = analysed_files(repo, repo)  # docs written to the repo root: exclude nothing
    assert "pkg/a.py" in files and "docs/conf.py" in files


def test_uncommitted_docs_are_backed_up_before_the_update_edits_them(tmp_path):
    """The backup used to be taken at fallback time, after the update had deleted pages."""
    repo = _repo_with_docs(tmp_path)
    docs = repo / "docs"
    (docs / "overview.md").write_text("# Edited, not committed\n")
    (docs / "page.md").write_text("# A page the update deletes\n")
    (docs / "temp" / "dependency_graphs").mkdir(parents=True)
    (docs / "temp" / "dependency_graphs" / "g.json").write_text("{}")
    gen = _gen_for(docs)
    gen._docs_clean_at_start = False
    gen._backup_docs()  # what --update does first when docs are not committed

    (docs / "page.md").unlink()  # the update invalidates pages...
    (docs / "overview.md").write_text("# Half-rewritten\n")  # ...and rewrites others
    gen._move_docs_aside()  # then falls back to a full build

    backup = repo / "docs.prev-abcdef12"
    assert (backup / "page.md").read_text() == "# A page the update deletes\n"
    assert (backup / "overview.md").read_text() == "# Edited, not committed\n"
    assert not (backup / "temp").exists()  # the graph is rebuilt from git when needed
    assert os.listdir(docs) == []


def test_successful_update_drops_the_backup(tmp_path):
    repo = _repo_with_docs(tmp_path)
    (repo / "docs" / "overview.md").write_text("# Edited\n")
    gen = _gen_for(repo / "docs")
    gen._backup_docs()
    assert (repo / "docs.prev-abcdef12").is_dir()
    gen._drop_docs_backup()
    assert not list(repo.glob("docs.prev-*"))


def test_a_repo_root_is_never_cleared(tmp_path):
    repo = _repo_with_docs(tmp_path)
    (repo / "overview.md").write_text("# docs written to the repo root\n")
    gen = _gen_for(repo)
    gen._docs_clean_at_start = False
    gen._backup_docs()
    gen._move_docs_aside()
    assert (repo / "overview.md").exists() and (repo / ".git").exists()
    assert not list(tmp_path.glob("repo.prev-*"))
    assert "never cleared" in gen.notes[-1]
