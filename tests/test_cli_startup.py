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


VALIDATE = """
import openai
seen = {}
class Unreachable:
    def __init__(self, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("boom: host unreachable")
openai.OpenAI = Unreachable
from codewiki.cli.main import cli
try:
    cli(["config", "validate"])
except SystemExit:
    pass
print("PROBE", seen.get("max_retries"), seen.get("timeout"))
"""


def test_config_validate_says_it_is_testing_and_why_it_failed(tmp_path):
    """The connectivity test ran in silence (the SDK retried for seconds), then failed without
    a reason unless --verbose."""
    import os

    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    env.update(CODEWIKI_NO_KEYRING="1", PYTHONIOENCODING="utf-8")
    set_args = ["--base-url", "http://127.0.0.1:9/v1", "--api-key", "dummy-key-0123456789"]
    set_args += ["--main-model", "m", "--cluster-model", "m", "--fallback-model", "m"]
    setup = f"from codewiki.cli.main import cli\ncli(['config', 'set', *{set_args!r}])"
    subprocess.run([sys.executable, "-c", setup], env=env, capture_output=True, check=False)

    out = subprocess.run(
        [sys.executable, "-c", VALIDATE], env=env, capture_output=True, text=True, encoding="utf-8"
    )
    text = out.stdout + out.stderr
    assert "Testing API connectivity..." in text, text
    assert "Error: boom: host unreachable" in text, text  # the reason, without --verbose
    assert "PROBE 0 15" in text, text  # one attempt, bounded wait
