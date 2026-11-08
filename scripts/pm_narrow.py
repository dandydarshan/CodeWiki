"""Narrow a PythonMonkey crash to one subsystem. Each step runs in a fresh process.

Run:  python scripts/pm_narrow.py
Steps that await a JS promise make PythonMonkey look up the running asyncio loop
by reading CPython's thread state at a compiled-in offset; a wheel built against
other Python headers reads the wrong field there (seen: 3.14.3 vs 3.14.4+).
"""

import subprocess
import sys

STEPS = {
    "eval_number": "import pythonmonkey as pm; print(pm.eval('1 + 1'))",
    "call_js_function": "import pythonmonkey as pm; print(pm.eval('(x) => x + 1')(1))",
    "await_resolved": (
        "import asyncio, pythonmonkey as pm\n"
        "async def m(): return await pm.eval('Promise.resolve(1)')\n"
        "print(asyncio.run(m()))"
    ),
    "await_async_fn": (
        "import asyncio, pythonmonkey as pm\n"
        "async def m(): return await pm.eval('(async () => 2)')()\n"
        "print(asyncio.run(m()))"
    ),
    "await_setTimeout": (
        "import asyncio, pythonmonkey as pm\n"
        "async def m(): return await pm.eval('new Promise(r => setTimeout(() => r(3), 10))')\n"
        "print(asyncio.run(m()))"
    ),
}


def crashed(returncode: int) -> bool:
    """A native crash: killed by a signal (POSIX) or an NTSTATUS error code (Windows)."""
    return returncode < 0 or (returncode & 0xFFFFFFFF) >= 0xC0000000


def main() -> None:
    print(sys.version)
    crashes = []
    for name, code in STEPS.items():
        r = subprocess.run([sys.executable, "-X", "faulthandler", "-c", code],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            status = "ok"
        else:
            kind = "CRASH" if crashed(r.returncode) else "error"
            status = f"{kind} {r.returncode & 0xFFFFFFFF:#010x}"
            if kind == "CRASH":
                crashes.append(name)
        out = (r.stdout.strip().splitlines() or [""])[-1][:60]
        err = "" if r.returncode == 0 else (r.stderr.strip().splitlines() or [""])[-1][:100]
        print(f"{name:22} {status:18} {out} {err}")
    if crashes:
        print(f"Native crash in: {', '.join(crashes)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
