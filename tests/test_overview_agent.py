"""Parent / repo overviews on API backends read child docs through a real tool.

Regression coverage: the overview prompt references child docs by path, but API
backends used to answer it with a tool-less completion, so models such as Kimi
wrote their tool calls as text into the page or invented the children.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

import codewiki.src.be.pydantic_ai_backend as backend_module
from codewiki.src.be.agent_tools.read_module_documentation import (
    OverviewDeps,
    read_module_documentation,
)
from codewiki.src.be.documentation_generator import DocumentationGenerator
from codewiki.src.config import Config


def _read(docs: Path, file_path: str) -> str:
    ctx = SimpleNamespace(deps=OverviewDeps(absolute_docs_path=str(docs)))
    return asyncio.run(read_module_documentation(ctx, file_path))


@pytest.fixture
def docs(tmp_path):
    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    (docs / "child.md").write_text("# Child\nREAL-CHILD-CONTENT\n", encoding="utf-8")
    (docs / "sub" / "nested.md").write_text("# Nested\n", encoding="utf-8")
    (docs / "module_tree.json").write_text("{}", encoding="utf-8")
    (tmp_path / "secret.md").write_text("secret", encoding="utf-8")
    return docs


@pytest.mark.parametrize("path", ["child.md", "docs/child.md", "sub/nested.md"])
def test_tool_reads_docs_by_relative_path(docs, path):
    assert _read(docs, path).startswith("# ")


def test_tool_reads_docs_by_absolute_path(docs):
    assert "REAL-CHILD-CONTENT" in _read(docs, str(docs / "child.md"))


@pytest.mark.parametrize("path", ["../secret.md", "sub/../../secret.md"])
def test_tool_refuses_paths_outside_docs(docs, path):
    assert "outside the documentation directory" in _read(docs, path)


def test_tool_refuses_absolute_path_outside_docs(docs):
    assert "outside the documentation directory" in _read(docs, str(docs.parent / "secret.md"))


def test_tool_reports_missing_and_non_markdown_files(docs):
    assert "not found" in _read(docs, "missing.md")
    assert "only .md" in _read(docs, "module_tree.json")


def test_pydantic_overview_agent_reads_child_docs(docs, monkeypatch):
    def scripted(messages, info):
        returns = [
            str(p.content) for m in messages for p in m.parts if isinstance(p, ToolReturnPart)
        ]
        if not returns:
            return ModelResponse(
                parts=[
                    ToolCallPart("read_module_documentation", {"file_path": str(docs / "child.md")})
                ]
            )
        return ModelResponse(parts=[TextPart(f"<OVERVIEW>{returns[0]}</OVERVIEW>")])

    monkeypatch.setattr(backend_module, "create_fallback_models", lambda c: FunctionModel(scripted))
    config = Config.from_cli(
        repo_path=str(docs.parent),
        output_dir=str(docs),
        llm_base_url="http://127.0.0.1:9",
        llm_api_key="unused",
        main_model="scripted",
        cluster_model="scripted",
    )
    backend = backend_module.PydanticAIBackend(config)
    out = asyncio.run(backend.run_overview_agent("prompt", working_dir=str(docs), target="parent"))
    assert "REAL-CHILD-CONTENT" in out


# ------------------------------------------------- leaked tool-call guard

LEAKED = (
    "<|tool_calls_section_begin|><|tool_call_begin|>functions.read_module_documentation:0"
    '<|tool_call_argument_begin|>{"file_path": "child.md"}<|tool_call_end|>'
)


class _ScriptedBackend:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def run_overview_agent(self, prompt, *, system_prompt=None, **_):
        self.calls += 1
        return self.replies.pop(0)


def _generator(docs: Path, replies) -> DocumentationGenerator:
    gen = object.__new__(DocumentationGenerator)
    gen.config = SimpleNamespace(
        docs_dir=str(docs), repo_path=str(docs), get_prompt_addition=lambda: "", layout="flat"
    )
    gen.backend = _ScriptedBackend(replies)
    (docs / "module_tree.json").write_text(
        json.dumps({"parent": {"components": [], "children": {}}}), encoding="utf-8"
    )
    return gen


@pytest.mark.parametrize(
    "leaked", [LEAKED, "functions.read_module_documentation:0 {}", "<tool_call>{}</tool_call>"]
)
def test_leaked_tool_call_is_retried_not_published(docs, leaked):
    gen = _generator(docs, [leaked, "<OVERVIEW># Parent\nGood.</OVERVIEW>"])
    asyncio.run(gen.generate_parent_module_docs(["parent"], str(docs)))
    assert gen.backend.calls == 2
    assert (docs / "parent.md").read_text(encoding="utf-8") == "# Parent\nGood."


def test_leaked_tool_call_twice_fails_without_writing(docs):
    gen = _generator(docs, [LEAKED, f"<OVERVIEW>{LEAKED}</OVERVIEW>"])
    with pytest.raises(RuntimeError, match="raw tool-call text"):
        asyncio.run(gen.generate_parent_module_docs(["parent"], str(docs)))
    assert not (docs / "parent.md").exists()


def test_prose_mentioning_functions_is_not_flagged(docs):
    page = "<OVERVIEW># Parent\nSee the functions.md page and `utils.functions:1`.</OVERVIEW>"
    gen = _generator(docs, [page])
    asyncio.run(gen.generate_parent_module_docs(["parent"], str(docs)))
    assert gen.backend.calls == 1
