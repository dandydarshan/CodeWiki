import asyncio
import contextlib
import importlib.util
import io
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Tuple
from codewiki.src.be.mermaid_support import (
    DiagramResult,
    MermaidReport,
    is_parse_error,
    mermaid_blocks,
    quote_flowchart_labels,
)
import tiktoken


logger = logging.getLogger(__name__)


# PythonMonkey (used by mermaid_parser.parse_mermaid_py) is only safe on the
# process main thread. Importing it on another thread segfaults, and using it
# from a non-main thread's loop crashes Python at exit (seen on every version,
# Linux and macOS). The caw backend runs MCP tool calls on a FastMCP daemon
# thread, so it records the main loop here and _try_pythonmonkey_parse hands
# the parse (import included) back to it via asyncio.run_coroutine_threadsafe.
_main_loop: "asyncio.AbstractEventLoop | None" = None
_main_loop_thread_ident: int | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop, _main_loop_thread_ident
    _main_loop = loop
    _main_loop_thread_ident = threading.get_ident()


# ------------------------------------------------------------
# ---------------------- Complexity Check --------------------
# ------------------------------------------------------------


def is_complex_module(components: dict[str, any], core_component_ids: list[str]) -> bool:
    files = set()
    for component_id in core_component_ids:
        if component_id in components:
            files.add(components[component_id].file_path)

    result = len(files) > 1

    return result


# ------------------------------------------------------------
# ---------------------- Token Counting ---------------------
# ------------------------------------------------------------

enc = tiktoken.encoding_for_model("gpt-4")


def count_tokens(text: str) -> int:
    """
    Count the number of tokens in a text.
    """
    # source code may contain literal special tokens such as "<|endoftext|>"
    # (e.g. CLIP tokenizers); count them as plain text instead of raising
    length = len(enc.encode(text, disallowed_special=()))
    # logger.debug(f"Number of tokens: {length}")
    return length


# ------------------------------------------------------------
# ---------------------- Mermaid Validation -----------------
# ------------------------------------------------------------


def _diagram_message(message: str, diagram_num: int, line_start: int) -> str:
    # Translate parser-relative line numbers without dropping useful error text.
    message = re.sub(
        r"line (\d+)",
        lambda match: f"line {line_start + int(match.group(1))}",
        message,
    )
    return f"Diagram {diagram_num}: {message}"


async def validate_mermaid_content(content: str, *, repair: bool = False) -> MermaidReport:
    """Validate Markdown and optionally return only parser-confirmed repairs."""
    report = MermaidReport(content)
    replacements = []
    for number, block in enumerate(mermaid_blocks(content), 1):
        report.diagrams += 1
        result = await _validate_diagram(block.content)
        if (
            repair
            and block.closed
            and result.status == "invalid"
            and re.search(
                r"got ['\"]?(?:PS|PE|SQS|SQE|DIAMOND_START|DIAMOND_STOP)\b", result.message
            )
        ):
            candidate, changes = quote_flowchart_labels(block.content)
            if changes:
                checked = await _validate_diagram(candidate)
                if checked.status == "valid":
                    replacements.append((block.start, block.end, candidate))
                    report.repairs.extend(
                        f"Diagram {number} (fence line {block.line}): {old} -> {new}"
                        for old, new in changes
                    )
                    result = checked
                elif checked.status == "unavailable":
                    report.unavailable.append(
                        _diagram_message(
                            "repair not saved; revalidation unavailable: " + checked.message,
                            number,
                            block.line,
                        )
                    )
        if result.status == "invalid":
            report.errors.append(_diagram_message(result.message, number, block.line))
        elif result.status == "unavailable":
            report.unavailable.append(_diagram_message(result.message, number, block.line))
    for start, end, replacement in reversed(replacements):
        report.content = report.content[:start] + replacement + report.content[end:]
    return report


async def _validate_mermaid_file(md_file_path: str, relative_path: str, *, repair: bool) -> str:
    try:
        file_path = Path(md_file_path)
        if not file_path.exists():
            return f"Error: File '{md_file_path}' does not exist"
        original = file_path.read_bytes()
        report = await validate_mermaid_content(original.decode("utf-8"), repair=repair)
        if report.content.encode("utf-8") != original:
            # Validation may await a remote renderer. Never overwrite an edit
            # made while it was running, or claim an unsaved repair succeeded.
            if file_path.read_bytes() != original:
                return "Warning: File changed during Mermaid validation; re-run validation."
            file_path.write_bytes(report.content.encode("utf-8"))
        return report.format(relative_path)
    except Exception as e:
        return f"Warning: Mermaid validation unavailable for {relative_path}: {e}"


async def validate_mermaid_diagrams(md_file_path: str, relative_path: str) -> str:
    """Validate without changing the file, including after an undo operation."""
    return await _validate_mermaid_file(md_file_path, relative_path, repair=False)


async def validate_and_repair_mermaid_diagrams(md_file_path: str, relative_path: str) -> str:
    """Validate a successful write and persist only verified flowchart repairs."""
    return await _validate_mermaid_file(md_file_path, relative_path, repair=True)


def extract_mermaid_blocks(content: str) -> List[Tuple[int, str]]:
    """Return opening-fence line numbers and bodies (legacy read-only API)."""
    return [
        (block.line, block.content.removesuffix("\n").removesuffix("\r"))
        for block in mermaid_blocks(content)
    ]


# Set once PythonMonkey fails to import or loses its event loop; validation
# then falls back to system Node.js running the same parser bundle.
_PYTHONMONKEY_BROKEN = False
_NODE_TIMEOUT_SECONDS = 15.0

# Disabled: remote fallback through mermaid-py, which sends diagram content to
# https://mermaid.ink (or MERMAID_INK_SERVER). Kept commented out so it can be
# brought back: uncomment this, _parse_via_mermaid_py, the block at the end of
# validate_single_diagram's fallbacks, and mermaid-py in pyproject/requirements.
# _MERMAID_PY_BROKEN = os.environ.get("MERMAID_VALIDATE", "0").strip() != "1"

# Validation never touches the network: PythonMonkey, then Node, then skip.
_pm_unavailable_warned = False
_skip_warned = False


def _warn_pythonmonkey_unavailable_once() -> None:
    global _pm_unavailable_warned
    if not _pm_unavailable_warned:
        _pm_unavailable_warned = True
        logger.warning("PythonMonkey Mermaid validation is unavailable; trying system Node.js.")


def _warn_validation_skipped_once(reason: str) -> None:
    global _skip_warned
    if not _skip_warned:
        _skip_warned = True
        logger.warning("Mermaid validation skipped: %s", reason)


# PythonMonkey prints JS noise to stderr, so validation points sys.stderr at
# devnull. Concurrent agents validate at the same time; a plain save/restore
# per call interleaves and can leave stderr on a closed devnull, so the swap
# is reference-counted: the first caller swaps, the last one restores.
_stderr_lock = threading.Lock()
_stderr_users = 0
_saved_stderr = None


def _quiet_stderr_enter() -> None:
    global _stderr_users, _saved_stderr
    with _stderr_lock:
        if _stderr_users == 0:
            _saved_stderr = sys.stderr
            sys.stderr = open(os.devnull, "w")  # noqa: SIM115 — closed in _quiet_stderr_exit
        _stderr_users += 1


def _quiet_stderr_exit() -> None:
    global _stderr_users, _saved_stderr
    with _stderr_lock:
        _stderr_users -= 1
        if _stderr_users == 0:
            sys.stderr.close()
            sys.stderr = _saved_stderr
            _saved_stderr = None


async def _try_pythonmonkey_parse(diagram_content: str) -> str | None:
    """Attempt to parse via PythonMonkey/mermaid-parser-py.

    Returns the extracted parse-error message, "" on success, or None when
    PythonMonkey itself is unusable (failed import or lost event loop) so the
    caller can fall back to system Node.js.
    """
    global _PYTHONMONKEY_BROKEN
    if _PYTHONMONKEY_BROKEN:
        _warn_pythonmonkey_unavailable_once()
        return None

    on_main_thread = threading.current_thread() is threading.main_thread()
    via_main_loop = (
        not on_main_thread
        and _main_loop is not None
        and _main_loop.is_running()
        and _main_loop_thread_ident == threading.main_thread().ident
    )
    if not (on_main_thread or via_main_loop):
        # e.g. the web app's worker thread: no main-thread loop to hand off to.
        _warn_pythonmonkey_unavailable_once()
        return None
    if not await _run_in_daemon_thread(_pythonmonkey_probe_passes):
        _PYTHONMONKEY_BROKEN = True
        _warn_pythonmonkey_unavailable_once()
        return None

    _quiet_stderr_enter()
    try:
        if via_main_loop:
            fut = asyncio.run_coroutine_threadsafe(
                _parse_with_pythonmonkey(diagram_content), _main_loop
            )
            await asyncio.wrap_future(fut)
        else:
            await _parse_with_pythonmonkey(diagram_content)
        return ""
    except ImportError:
        _PYTHONMONKEY_BROKEN = True
        _warn_pythonmonkey_unavailable_once()
        return None
    except Exception as e:
        error_str = str(e)
        # Raised when called off PythonMonkey's home loop. Latch the failure
        # once so later diagrams skip straight to Node.
        if "cannot find a running Python event-loop" in error_str:
            _PYTHONMONKEY_BROKEN = True
            _warn_pythonmonkey_unavailable_once()
            return None
        match = re.search(r"Error:(.*?)(?=Stack Trace:|$)", error_str, re.DOTALL)
        if match and is_parse_error(match.group(0)):
            return match.group(0).strip()
        # Unknown error from the JS parser — fall back rather than surface it.
        return None
    finally:
        _quiet_stderr_exit()


# A PythonMonkey wheel built against other CPython headers crashes the process on its
# first awaited promise (e.g. Python 3.14.0-3.14.3 with wheels built for 3.14.4+, see
# https://github.com/Distributive-Network/PythonMonkey/issues/515). A native crash
# raises nothing to fall back on, so the real parse is tried once in a child process.
_PROBE_SCRIPT = """
import asyncio, contextlib, io
with contextlib.redirect_stdout(io.StringIO()):
    from mermaid_parser.parser import parse_mermaid_py
async def main():
    for source in ["flowchart TD\\n  A --> B\\n", "flowchart TD\\n  A --> --> B(\\n"] * 2:
        try:
            await parse_mermaid_py(source)
        except Exception:
            pass  # a rejected diagram is fine; only a crash matters
asyncio.run(main())
"""
_PROBE_TIMEOUT_SECONDS = 120.0  # PythonMonkey's first import takes seconds on Windows
_probe_lock = threading.Lock()
_probe_result: bool | None = None


def _native_crash(returncode: int) -> bool:
    """Killed by a signal (POSIX) or an NTSTATUS error such as an access violation (Windows)."""
    return returncode < 0 or (returncode & 0xFFFFFFFF) >= 0xC0000000


def _pythonmonkey_probe_passes() -> bool:
    """Whether PythonMonkey parses without crashing here, checked once per process."""
    global _probe_result
    with _probe_lock:
        if _probe_result is None:
            try:
                result = subprocess.run(
                    [sys.executable, "-c", _PROBE_SCRIPT],
                    capture_output=True,
                    timeout=_PROBE_TIMEOUT_SECONDS,
                )
                # Only a native crash counts: a Python error (say, PythonMonkey is not
                # installed) is raised and handled the same way in this process.
                _probe_result = not _native_crash(result.returncode)
                if not _probe_result:
                    logger.warning(
                        "PythonMonkey failed a test parse (exit code %#x) and would crash "
                        "CodeWiki: it does not support Python %s. Upgrade Python to the "
                        "latest patch release to use it; Mermaid diagrams are checked with "
                        "Node.js meanwhile.",
                        result.returncode & 0xFFFFFFFF,
                        platform.python_version(),
                    )
            except (OSError, subprocess.TimeoutExpired) as e:
                logger.warning("PythonMonkey test parse did not finish (%s); using Node.js.", e)
                _probe_result = False
        return _probe_result


async def _parse_with_pythonmonkey(diagram_content: str) -> None:
    """Parse on the main thread; importing here keeps the import there too."""
    try:
        # mermaid_parser/__init__ imports mermaid-py, which print()s "IPython is not
        # installed" on import (it's about its Jupyter magic, which we never use).
        with contextlib.redirect_stdout(io.StringIO()):
            from mermaid_parser.parser import parse_mermaid_py
    except Exception as e:
        raise ImportError("PythonMonkey is unavailable") from e
    await parse_mermaid_py(diagram_content)


def _resolve_node_executable() -> str | None:
    override = os.environ.get("CODEWIKI_NODE_PATH", "").strip()
    if override:
        return override if Path(override).is_file() else None
    return shutil.which("node")


# Reads the diagram on stdin, exits 0 if it parses, else 1 with the error on stderr.
_NODE_PARSE_SCRIPT = (
    'const parse = require(process.argv[1]); let s = "";'
    'process.stdin.on("data", (d) => (s += d)).on("end", () => parse(s).then('
    "() => process.exit(0),"
    "(e) => { process.stderr.write(String((e && e.message) || e)); process.exit(1); }));"
)


def _parse_via_node(diagram_content: str) -> str | None:
    """Validate with system Node.js running mermaid-parser-py's own bundle.

    Returns the parse-error message, "" on success, or None (after one
    warning) when Node or the bundle is missing or the run fails.
    """
    node = _resolve_node_executable()
    if node is None:
        _warn_validation_skipped_once(
            "Node.js not found on PATH (set CODEWIKI_NODE_PATH to point at it)."
        )
        return None
    # find_spec does not run mermaid_parser/__init__, which imports PythonMonkey.
    spec = importlib.util.find_spec("mermaid_parser")
    bundle = Path(spec.origin).parent / "js" / "parser.bundle.js" if spec and spec.origin else None
    if bundle is None or not bundle.is_file():
        _warn_validation_skipped_once("mermaid-parser-py's parser.bundle.js was not found.")
        return None
    try:
        result = subprocess.run(
            [node, "-e", _NODE_PARSE_SCRIPT, str(bundle)],
            input=diagram_content,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_NODE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _warn_validation_skipped_once(f"Node.js validator failed to run ({e}).")
        return None
    if result.returncode == 0:
        return ""
    return result.stderr.strip() or "Mermaid parse failed (no message from Node.js)"


# def _parse_via_mermaid_py(diagram_content: str) -> str:
#     """Validate via mermaid-py. Returns parse-error text, or "" if valid.
#
#     mermaid-py raises MermaidError on parse failure and returns an SVG body
#     on success — we must drive the result off the exception, not the body
#     text, otherwise a successful SVG gets reported as a parse error.
#     """
#     import mermaid as md
#
#     try:
#         md.Mermaid(diagram_content)
#         return ""
#     except Exception as e:
#         return str(e)


def _run_in_daemon_thread(fn, *args) -> "asyncio.Future":
    """Run fn in a daemon thread and return an awaitable for its result.

    Unlike asyncio.to_thread, a thread left hanging after wait_for times out
    does not block interpreter exit.
    """
    import threading

    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def _set(setter, value):
        if not fut.done():
            setter(value)

    def _worker():
        try:
            setter, value = fut.set_result, fn(*args)
        except BaseException as e:
            setter, value = fut.set_exception, e
        try:
            loop.call_soon_threadsafe(_set, setter, value)
        except RuntimeError:
            pass  # loop already closed; nobody is waiting any more

    threading.Thread(target=_worker, daemon=True).start()
    return fut


async def _validate_diagram(diagram_content: str) -> DiagramResult:
    """Local parsers only: PythonMonkey on the main thread, else system Node.js. Diagrams are
    never sent to a web service (mermaid.ink); with neither parser, validation is unavailable."""
    core_error = await _try_pythonmonkey_parse(diagram_content)
    if core_error is None:
        try:
            core_error = await asyncio.wait_for(
                _run_in_daemon_thread(_parse_via_node, diagram_content),
                timeout=_NODE_TIMEOUT_SECONDS + 5.0,
            )
        except asyncio.TimeoutError:
            core_error = None
            _warn_validation_skipped_once("Node.js validator timed out.")
    if core_error is None:
        return DiagramResult("unavailable", "no local Mermaid parser (PythonMonkey or Node.js)")
    if not core_error:
        return DiagramResult("valid")
    if is_parse_error(core_error):
        return DiagramResult("invalid", core_error)
    return DiagramResult("unavailable", core_error)


async def validate_single_diagram(diagram_content: str, diagram_num: int, line_start: int) -> str:
    """Legacy string API: empty only on success; unavailable is an explicit warning."""
    result = await _validate_diagram(diagram_content)
    if result.status == "valid":
        return ""
    message = _diagram_message(result.message, diagram_num, line_start)
    if result.status == "unavailable":
        return "Warning: Mermaid validation unavailable: " + message
    return message
