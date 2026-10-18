"""Validate Mermaid through PythonMonkey, then exit; the runner checks the exit code.

codewiki/src/be/utils.py skips PythonMonkey on Python 3.12+ because it was seen
to segfault at interpreter shutdown on macOS. This runs CodeWiki's own
validation path with that guard off, so a crash at exit shows up as a
non-zero exit code.

Usage: check.py main|caw|caw-preimport
  main: validate on the main thread (pydantic-ai backend).
  caw:  validate from a daemon thread inside asyncio.to_thread, marshalled back
        to the main loop (caw backend, where FastMCP runs tools on a thread).
        PythonMonkey is first imported on that daemon thread, as in CodeWiki.
  caw-preimport: as caw, but PythonMonkey is imported on the main thread first.
"""

import asyncio
import os
import sys
import tempfile
import threading

# No mermaid.ink fallback: only a working PythonMonkey can flag the bad diagram.
os.environ["MERMAID_VALIDATE"] = "0"

import codewiki.src.be.utils as u  # noqa: E402

u._PYTHONMONKEY_BROKEN = False

if sys.argv[1] == "caw-preimport":
    import mermaid_parser.parser  # noqa: E402, F401

GOOD = "```mermaid\ngraph TD\n  A[Greeter] --> B[shout]\n```\n"
BAD = "```mermaid\ngraph TD\n  A -->\n```\n"


def write(text):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(text)
    return f.name


async def check():
    good = await u.validate_mermaid_diagrams(write(GOOD), "good.md")
    bad = await u.validate_mermaid_diagrams(write(BAD), "bad.md")
    return good, bad


async def caw_style():
    u.set_main_loop(asyncio.get_running_loop())

    def tool_thread():
        out = {}
        t = threading.Thread(target=lambda: out.update(r=asyncio.run(check())), daemon=True)
        t.start()
        t.join()
        return out["r"]

    return await asyncio.to_thread(tool_thread)


good, bad = asyncio.run(check() if sys.argv[1] == "main" else caw_style())
print(good)
print(bad)
assert not u._PYTHONMONKEY_BROKEN, "PythonMonkey path was not used"
assert "syntax correct" in good, good
assert "errors found" in bad, bad
print("validation OK; exiting the interpreter")
