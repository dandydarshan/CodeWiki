"""Find which PythonMonkey use crashes this machine (Windows access violation hunt).

Run:  python scripts/pythonmonkey_repro.py
Each step runs in a fresh interpreter, so a native crash names the step and the
rest still run. Send back the whole output, from every machine you try.
"""

import faulthandler
import platform
import subprocess
import sys

VALID = "flowchart TD\n    A[Start] --> B[End]\n"
INVALID = "flowchart TD\n    A[Start] --> --> B(\n"  # agents often write broken diagrams

STEPS = {
    "import_pythonmonkey": "import pythonmonkey",
    "import_mermaid_parser": (
        "import contextlib, io\n"
        "with contextlib.redirect_stdout(io.StringIO()):\n"
        "    from mermaid_parser.parser import parse_mermaid_py"
    ),
    "one_valid_parse": "run([VALID])",
    "one_invalid_parse": "run([INVALID])",
    "two_valid_parses": "run([VALID, VALID])",  # upstream #271: the second call crashes
    "200_mixed_sequential": "run([VALID, INVALID] * 100)",
    "50_concurrent": "run([VALID, INVALID] * 25, concurrent=True)",
    "stderr_to_devnull": (  # codewiki points sys.stderr at devnull around each parse
        "import os, sys\nsys.stderr = open(os.devnull, 'w')\nrun([VALID, INVALID] * 20)"
    ),
    "gc_between_parses": "import gc\nrun([VALID, INVALID] * 20, collect=gc.collect)",
    # Windows' default loop is Proactor; Linux/macOS (where it never crashes) use Selector
    "selector_loop": "run([VALID, INVALID] * 20, loop_factory=asyncio.SelectorEventLoop)",
    "via_codewiki": (  # codewiki's real entry point, wrappers included
        "import asyncio\n"
        "from codewiki.src.be.utils import validate_mermaid_content\n"
        "doc = ''.join(f'```mermaid\\n{d}```\\n' for d in [VALID, INVALID] * 20)\n"
        "print(asyncio.run(validate_mermaid_content(doc)))"
    ),
}

PRELUDE = f"""
import asyncio, contextlib, io
VALID = {VALID!r}
INVALID = {INVALID!r}
def run(diagrams, concurrent=False, collect=None, loop_factory=None):
    with contextlib.redirect_stdout(io.StringIO()):
        from mermaid_parser.parser import parse_mermaid_py
    async def one(d):
        try:
            await parse_mermaid_py(d)
            return True
        except Exception:
            return False
    async def main():
        if concurrent:
            return await asyncio.gather(*(one(d) for d in diagrams))
        out = []
        for d in diagrams:
            out.append(await one(d))
            if collect:
                collect()
        return out
    ok = asyncio.run(main(), loop_factory=loop_factory) if loop_factory else asyncio.run(main())
    print(f"{{sum(ok)}} parsed, {{len(ok) - sum(ok)}} rejected")
"""


def version(name: str) -> str:
    try:
        from importlib.metadata import version as v

        return v(name)
    except Exception:  # noqa: BLE001
        return "not installed"


def crashed(returncode: int) -> bool:
    """A native crash: killed by a signal (POSIX) or an NTSTATUS error code (Windows)."""
    return returncode < 0 or (returncode & 0xFFFFFFFF) >= 0xC0000000


def main() -> None:
    faulthandler.enable()
    crashes = []
    print(f"Python {sys.version.split()[0]} ({platform.architecture()[0]}) on {platform.platform()}")
    print(f"pythonmonkey {version('pythonmonkey')}, mermaid-parser-py {version('mermaid-parser-py')}, "
          f"codewiki {version('codewiki')}")
    for name, body in STEPS.items():
        code = PRELUDE + body
        r = subprocess.run([sys.executable, "-X", "faulthandler", "-c", code],
                           capture_output=True, text=True, timeout=300)
        if r.returncode == 0:
            status = "ok"
        else:
            kind = "CRASH" if crashed(r.returncode) else "error"
            status = f"{kind} {r.returncode} ({r.returncode & 0xFFFFFFFF:#010x})"
            if kind == "CRASH":
                crashes.append(name)
        detail = (r.stdout.strip().splitlines() or [""])[-1][:100]
        print(f"{name:24} {status:28} {detail}")
        if r.returncode != 0:
            print("    " + "\n    ".join(r.stderr.strip().splitlines()[-8:]))
    if crashes:
        print(f"Native crash in: {', '.join(crashes)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
