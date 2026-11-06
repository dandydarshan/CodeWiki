"""CLI startup stays light: only `generate` loads the LLM stack, and only when run."""

import subprocess
import sys

HEAVY = ("pydantic_ai", "openai", "litellm", "fastmcp", "mcp")


def test_cli_import_does_not_load_llm_stack():
    # A fresh interpreter, so modules other tests imported don't count.
    code = (
        f"import sys, codewiki.cli.main\nprint(','.join(m for m in {HEAVY!r} if m in sys.modules))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert loaded == "", f"importing the CLI loaded {loaded}; import them inside the command"
