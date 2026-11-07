"""`generate` says which branch and which uncommitted files it documents, and asks when that
looks unintended. A user generated docs on a bugfix branch without noticing."""

import subprocess
import sys
from pathlib import Path

import pytest

import codewiki.cli.commands.generate as gen
from codewiki.cli.git_manager import GitManager


def git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("x = 1\n")
    (repo / "src" / "b.py").write_text("y = 1\n")
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo


def test_context_names_branch_and_the_uncommitted_files_that_go_in(repo):
    git(repo, "switch", "-q", "-c", "bugfix/login")
    (repo / "src" / "a.py").write_text("x = 2\n")  # unstaged
    (repo / "src" / "b.py").write_text("y = 2\n")
    git(repo, "add", "src/b.py")  # staged: on disk all the same
    (repo / "src" / "scratch.py").write_text("SECRET = 1\n")  # untracked
    (repo / "docs").mkdir()
    (repo / "docs" / "overview.md").write_text("# generated\n")  # the output: not code

    ctx = GitManager(repo).generation_context(repo / "src", repo / "docs")
    assert ctx["branch"] == "bugfix/login" and ctx["default_branch"] == "main"
    assert ctx["modified"] == ["src/a.py", "src/b.py"]
    assert ctx["untracked"] == ["src/scratch.py"]

    elsewhere = GitManager(repo).generation_context(repo / "docs", repo / "out")
    assert elsewhere["modified"] == [] and elsewhere["untracked"] == ["docs/overview.md"]


class Log:
    def __init__(self):
        self.lines = []

    def info(self, m):
        self.lines.append(m)

    warning = debug = info


def run(repo, monkeypatch, *, tty=True, answer=True, assume_yes=False):
    asked = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty)
    monkeypatch.setattr(gen.click, "confirm", lambda q, default: asked.append(q) or answer)
    log = Log()
    gen._confirm_what_is_documented(log, repo, repo / "docs", assume_yes=assume_yes)
    return log.lines, asked


def test_default_branch_with_a_clean_tree_just_says_so(repo, monkeypatch):
    lines, asked = run(repo, monkeypatch)
    assert lines[0].startswith("Documenting branch main, commit ") and not asked


def test_another_branch_asks_and_cancelling_says_how_to_switch(repo, monkeypatch):
    git(repo, "switch", "-q", "-c", "bugfix/login")
    lines, asked = run(repo, monkeypatch)
    assert "branch bugfix/login (not main)" in lines[0]
    assert asked == [
        "You're on 'bugfix/login', not 'main'. Generate documentation from this working tree?"
    ]
    with pytest.raises(SystemExit):
        run(repo, monkeypatch, answer=False)


def test_untracked_files_are_named_and_asked_about(repo, monkeypatch):
    (repo / "src" / "scratch.py").write_text("x = 3\n")
    lines, asked = run(repo, monkeypatch)
    assert "sent to the LLM: src/scratch.py" in lines[1] and asked


def test_never_blocks_without_a_terminal_or_with_yes(repo, monkeypatch):
    git(repo, "switch", "-q", "-c", "feature/x")
    (repo / "src" / "scratch.py").write_text("x = 3\n")
    for kwargs in ({"tty": False}, {"assume_yes": True}):
        lines, asked = run(repo, monkeypatch, **kwargs)
        assert asked == [] and "branch feature/x (not main)" in lines[0], kwargs


def test_git_trouble_never_stops_generation(tmp_path, monkeypatch):
    lines, asked = run(Path(tmp_path), monkeypatch)  # not a repo at all
    assert asked == []
