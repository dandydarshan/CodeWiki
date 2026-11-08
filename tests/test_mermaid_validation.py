"""Mermaid syntax, repair persistence and inconclusive validation (issue #141)."""

import asyncio
import json
from pathlib import Path

import pytest

from codewiki.src.be import utils
from codewiki.src.be.mermaid_support import (
    DiagramResult,
    MERMAID_GUIDELINES,
    mermaid_blocks,
    quote_flowchart_labels,
)

FIXTURES = json.loads((Path(__file__).parent / "mermaid/fixtures.json").read_text())
INVALID = "flowchart TD\nC{msg.error()?}"
FIXED = 'flowchart TD\nC{"msg.error()?"}'
PARSE_ERROR = "Error: Parse error on line 2: expected DIAMOND_STOP, got 'PS'"


def markdown(diagram=INVALID):
    return f"```mermaid\n{diagram}\n```\n"


@pytest.fixture
def parser(monkeypatch):
    calls = []

    async def parse(source):
        calls.append(source)
        if "C{msg.error()?}" in source:
            return DiagramResult("invalid", PARSE_ERROR)
        if "broken" in source:
            return DiagramResult("invalid", "Parse error on line 2: got 'TEXT'")
        return DiagramResult("valid")

    monkeypatch.setattr(utils, "_validate_diagram", parse)
    return calls


@pytest.mark.parametrize("case", FIXTURES, ids=lambda case: case["name"])
def test_repair_fixtures_match_real_parser_expectations(case):
    repaired, changes = quote_flowchart_labels(case["source"])
    assert repaired == case.get("repaired", case["source"])
    assert bool(changes) == ("repaired" in case)
    assert quote_flowchart_labels(repaired) == (repaired, [])


def test_read_only_validation_reports_absolute_lines(parser):
    async def check():
        original = markdown()
        report = await utils.validate_mermaid_content(original, repair=False)
        assert report.content == original
        assert report.errors and not report.repairs
        assert "line 3" in report.errors[0]
        assert "got" in report.errors[0]
        assert MERMAID_GUIDELINES in report.format("x.md")

    asyncio.run(check())
    assert len(parser) == 1


def test_repairs_only_after_failure_and_persists_exact_bytes(tmp_path, parser):
    path = tmp_path / "x.md"
    before = (
        ("# Intro\r\n\r\n" + markdown() + "\n```python\nC{msg.error()?}\n```\n")
        .replace("\n", "\r\n")
        .replace("\r\r\n", "\r\n")
        .encode()
    )
    path.write_bytes(before)
    readonly = asyncio.run(utils.validate_mermaid_diagrams(str(path), "x.md"))
    assert "syntax errors" in readonly
    assert path.read_bytes() == before
    result = asyncio.run(utils.validate_and_repair_mermaid_diagrams(str(path), "x.md"))
    assert "repaired and validated" in result
    assert "fence line 3" in result
    assert path.read_bytes() == before.replace(
        INVALID.replace("\n", "\r\n").encode(), FIXED.replace("\n", "\r\n").encode(), 1
    )
    after = path.read_bytes()
    result = asyncio.run(utils.validate_and_repair_mermaid_diagrams(str(path), "x.md"))
    assert "syntax correct" in result and "repaired" not in result
    assert path.read_bytes() == after


@pytest.mark.parametrize("status", ["invalid", "unavailable"])
def test_failed_revalidation_does_not_save_candidate(tmp_path, monkeypatch, status):
    path = tmp_path / "x.md"
    path.write_text(markdown())
    original = path.read_bytes()
    results = iter([DiagramResult("invalid", PARSE_ERROR), DiagramResult(status, "still broken")])

    async def parse(_):
        return next(results)

    monkeypatch.setattr(utils, "_validate_diagram", parse)
    result = asyncio.run(utils.validate_and_repair_mermaid_diagrams(str(path), "x.md"))
    assert "syntax errors" in result and "repaired and validated" not in result
    assert path.read_bytes() == original
    if status == "unavailable":
        assert "revalidation unavailable" in result


def test_concurrent_change_is_not_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "x.md"
    path.write_text(markdown())

    async def parse(source):
        if "C{msg.error()?}" in source:
            return DiagramResult("invalid", PARSE_ERROR)
        path.write_text("concurrent edit")
        return DiagramResult("valid")

    monkeypatch.setattr(utils, "_validate_diagram", parse)
    result = asyncio.run(utils.validate_and_repair_mermaid_diagrams(str(path), "x.md"))
    assert "File changed" in result and "repaired and validated" not in result
    assert path.read_text() == "concurrent edit"


def test_multiple_diagrams_and_non_mermaid_fences(parser):
    original = (
        "# Title\n````markdown\n"
        + markdown()
        + "````\n"
        + markdown()
        + "\n~~~mermaid\n"
        + INVALID
        + "\n~~~\n"
    )
    report = asyncio.run(utils.validate_mermaid_content(original, repair=True))
    assert report.diagrams == 2 and len(report.repairs) == 2
    assert report.content.count(FIXED) == 2
    assert report.content.count(INVALID) == 1  # example inside outer Markdown fence
    assert len(parser) == 4
    assert len(utils.extract_mermaid_blocks(original)) == 2


def test_unclosed_fence_and_other_languages_are_not_repaired(parser):
    original = "```mermaidx\n" + INVALID + "\n```\n```mermaid\n" + INVALID
    report = asyncio.run(utils.validate_mermaid_content(original, repair=True))
    assert report.diagrams == 1 and report.errors
    assert report.content == original and not report.repairs
    assert not mermaid_blocks("```python\n" + INVALID + "\n```")


def test_mixed_results_do_not_claim_all_valid(monkeypatch):
    results = iter(
        [
            DiagramResult("valid"),
            DiagramResult("invalid", PARSE_ERROR.replace("'PS'", "'TEXT'")),
            DiagramResult("unavailable", "offline"),
        ]
    )

    async def parse(_):
        return next(results)

    monkeypatch.setattr(utils, "_validate_diagram", parse)
    report = asyncio.run(utils.validate_mermaid_content(markdown() * 3, repair=True))
    # First diagram stays untouched even though the source looks repairable.
    assert report.content == markdown() * 3
    assert len(report.errors) == 1 and report.unavailable
    message = report.format("mixed.md")
    assert "syntax errors" in message and "unavailable" in message
    assert "are syntax correct" not in message


# The fork validates locally only: PythonMonkey, else system Node.js. Diagrams are never sent
# to a web service (mermaid.ink), so upstream's remote-adapter tests are replaced by these.


def test_timed_out_local_validation_is_unavailable(monkeypatch):
    async def no_local(_):
        return None

    monkeypatch.setattr(utils, "_try_pythonmonkey_parse", no_local)
    calls = []

    async def timeout(*args, **kwargs):
        calls.append(kwargs["timeout"])
        raise asyncio.TimeoutError

    def ready(*args):
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(None)
        return fut

    monkeypatch.setattr(utils, "_run_in_daemon_thread", ready)
    monkeypatch.setattr(utils.asyncio, "wait_for", timeout)
    report = asyncio.run(utils.validate_mermaid_content(markdown() * 2, repair=True))
    assert len(report.unavailable) == 2 and not report.errors and not report.repairs
    assert report.content == markdown() * 2
    assert "syntax correct" not in report.format("x.md")
    assert calls == [utils._NODE_TIMEOUT_SECONDS + 5.0] * 2


@pytest.mark.parametrize("local", ["", PARSE_ERROR, None])
def test_local_parser_success_error_and_node_fallback(monkeypatch, local):
    async def parse(_):
        return local

    monkeypatch.setattr(utils, "_try_pythonmonkey_parse", parse)
    calls = []

    def node(source):
        calls.append(source)
        return ""  # valid

    monkeypatch.setattr(utils, "_parse_via_node", node)
    result = asyncio.run(utils._validate_diagram(INVALID))
    assert result.status == ("invalid" if local else "valid")
    assert len(calls) == (1 if local is None else 0)


def test_no_local_parser_is_an_explicit_warning(monkeypatch):
    async def no_local(_):
        return None

    monkeypatch.setattr(utils, "_try_pythonmonkey_parse", no_local)
    monkeypatch.setattr(utils, "_parse_via_node", lambda source: None)  # Node missing
    result = asyncio.run(utils.validate_single_diagram(FIXED, 1, 1))
    assert "validation unavailable" in result and "no local Mermaid parser" in result


def test_validation_never_contacts_a_web_service(monkeypatch):
    import requests

    def no_network(*args, **kwargs):
        raise AssertionError("Mermaid validation must not send diagrams to a web service")

    async def no_local(_):
        return None

    monkeypatch.setattr(requests, "get", no_network)
    monkeypatch.setattr(requests, "post", no_network)
    monkeypatch.setattr(utils, "_try_pythonmonkey_parse", no_local)
    monkeypatch.setattr(utils, "_parse_via_node", lambda source: None)
    report = asyncio.run(utils.validate_mermaid_content(markdown(), repair=True))
    assert report.unavailable and not report.errors


def test_empty_markdown_does_not_use_parser(parser):
    report = asyncio.run(utils.validate_mermaid_content("# No diagrams"))
    assert report.format("x.md") == "No mermaid diagrams found in the file"
    assert not parser


def test_valid_result_never_triggers_repair(monkeypatch):
    calls = []

    async def valid(source):
        calls.append(source)
        return DiagramResult("valid")

    monkeypatch.setattr(utils, "_validate_diagram", valid)
    report = asyncio.run(utils.validate_mermaid_content(markdown(), repair=True))
    assert not report.errors and not report.repairs
    assert report.content == markdown() and len(calls) == 1


def test_issue_exact_reproduction_repairs_only_diamond(parser):
    case = next(case for case in FIXTURES if case["name"] == "issue 141 exact reproduction")
    original = markdown(case["source"])
    report = asyncio.run(utils.validate_mermaid_content(original, repair=True))
    assert report.content == markdown(case["repaired"])
    assert not report.errors and len(report.repairs) == 1
    assert len(parser) == 2


def _fresh_probe(monkeypatch, returncode):
    """Reset the once-per-process probe and fake its child process exit code."""
    import subprocess
    from types import SimpleNamespace

    monkeypatch.setattr(utils, "_probe_result", None)
    monkeypatch.setattr(utils, "_PYTHONMONKEY_BROKEN", False)
    monkeypatch.setattr(utils, "_pm_unavailable_warned", False)
    runs = []

    def run(*a, **k):
        runs.append(a)
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(subprocess, "run", run)
    return runs


@pytest.mark.parametrize("returncode", [-11, 0xC0000005])  # segfault; Windows access violation
def test_crashing_pythonmonkey_falls_back_without_parsing_in_process(monkeypatch, returncode):
    """Python 3.14.0-3.14.3: PythonMonkey's wheels crash the process on the first parse
    (PythonMonkey issue #515). The probe sees it in a child, so CodeWiki uses Node."""
    runs = _fresh_probe(monkeypatch, returncode)

    async def must_not_run(source):
        pytest.fail("PythonMonkey must not parse in-process after a crashing probe")

    monkeypatch.setattr(utils, "_parse_with_pythonmonkey", must_not_run)
    assert asyncio.run(utils._try_pythonmonkey_parse("flowchart TD\n  A --> B\n")) is None
    assert utils._PYTHONMONKEY_BROKEN
    asyncio.run(utils._try_pythonmonkey_parse("flowchart TD\n  A --> B\n"))
    assert len(runs) == 1  # probed once, not per diagram


def test_python_error_in_probe_is_not_a_crash(monkeypatch):
    """PythonMonkey not installed exits 1 with an ImportError: handled in-process as before."""
    _fresh_probe(monkeypatch, 1)
    assert utils._pythonmonkey_probe_passes()


def test_probe_passes_where_pythonmonkey_works(monkeypatch):
    import importlib.util

    if importlib.util.find_spec("pythonmonkey") is None:
        pytest.skip("PythonMonkey is not installed")
    import sys

    if sys.version_info[:2] == (3, 14) and sys.version_info.micro < 4:
        pytest.skip("PythonMonkey crashes on Python 3.14.0-3.14.3 (issue #515)")
    monkeypatch.setattr(utils, "_probe_result", None)
    assert utils._pythonmonkey_probe_passes()
