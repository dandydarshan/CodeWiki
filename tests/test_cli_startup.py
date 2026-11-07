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


# Runs a command and reports, at its first line of output, which slow modules were loaded.
FIRST_LINE = """
import os, sys, click
SLOW = ("fastapi", "codewiki.src.be.qa") + {heavy!r}
def first(message=None, *a, **k):
    print(ascii(str(message)), [m for m in SLOW if m in sys.modules], flush=True)
    os._exit(0)
click.echo = click.secho = first
from codewiki.cli.main import cli
cli({args!r})
"""


def _first_line(*args):
    code = FIRST_LINE.format(heavy=HEAVY, args=list(args))
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_serve_and_ask_say_something_before_their_slow_imports():
    # Users on the compiled Windows wheel saw a blank terminal for seconds.
    # ascii(): the pipe's encoding differs on Windows
    assert _first_line("serve", "--docs", "nowhere") == f"{ascii('Starting CodeWiki serve…')} []"
    assert (
        _first_line("ask", "--docs", "x", "why?") == f"{ascii('… Loading the docs and code…')} []"
    )
