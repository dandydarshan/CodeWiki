import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Tuple

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


async def validate_mermaid_diagrams(md_file_path: str, relative_path: str) -> str:
    """
    Validate all Mermaid diagrams in a markdown file.

    Args:
        md_file_path: Path to the markdown file to check
        relative_path: Relative path to the markdown file
    Returns:
        "All mermaid diagrams are syntax correct" if all diagrams are valid,
        otherwise returns error message with details about invalid diagrams
    """

    try:
        # Read the markdown file
        file_path = Path(md_file_path)
        if not file_path.exists():
            return f"Error: File '{md_file_path}' does not exist"

        content = file_path.read_text(encoding="utf-8")

        # Extract all mermaid code blocks
        mermaid_blocks = extract_mermaid_blocks(content)

        if not mermaid_blocks:
            return "No mermaid diagrams found in the file"

        # Validate each mermaid diagram sequentially to avoid segfaults
        errors = []
        for i, (line_start, diagram_content) in enumerate(mermaid_blocks, 1):
            error_msg = await validate_single_diagram(diagram_content, i, line_start)
            if error_msg:
                errors.append("\n")
                errors.append(error_msg)

        # if errors:
        #     logger.debug(f"Mermaid syntax errors found in file: {md_file_path}: {errors}")

        if errors:
            return (
                "Mermaid syntax errors found in file: " + relative_path + "\n" + "\n".join(errors)
            )
        else:
            return "All mermaid diagrams in file: " + relative_path + " are syntax correct"

    except Exception as e:
        return f"Error processing file: {str(e)}"


def extract_mermaid_blocks(content: str) -> List[Tuple[int, str]]:
    """
    Extract all mermaid code blocks from markdown content.

    Returns:
        List of tuples containing (line_number, diagram_content)
    """
    mermaid_blocks = []
    lines = content.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        # Look for mermaid code block start
        if line == "```mermaid" or line.startswith("```mermaid"):
            start_line = i + 1
            diagram_lines = []
            i += 1

            # Collect lines until we find the closing ```
            while i < len(lines):
                if lines[i].strip() == "```":
                    break
                diagram_lines.append(lines[i])
                i += 1

            if diagram_lines:  # Only add non-empty diagrams
                diagram_content = "\n".join(diagram_lines)
                mermaid_blocks.append((start_line, diagram_content))

        i += 1

    return mermaid_blocks


# Set once PythonMonkey fails to import or loses its event loop; validation
# then falls back to the Node script, then mermaid-py.
_PYTHONMONKEY_BROKEN = True

# Local Node validator: subprocess to codewiki/mermaid_node/validate.mjs.
# Runs when PythonMonkey returns None (unavailable), not on PM syntax errors.
# Node binary: PATH or CODEWIKI_NODE_PATH.
_node_deps_lock = threading.Lock()
_node_validator_broken = False
_MERMAID_VALIDATE_TIMEOUT_SECONDS = 15.0

_pm_unavailable_warned = False
_node_missing_warned = False
_mermaid_remote_disabled_warned = False

# mermaid-py validates diagrams by sending them to a remote rendering service
# (https://mermaid.ink by default, overridable via the MERMAID_INK_SERVER env
# var understood by mermaid-py itself). Disabled by default; set MERMAID_VALIDATE=1
# to enable. A 15s timeout latches _MERMAID_PY_BROKEN for the rest of the process.
_MERMAID_PY_BROKEN = os.environ.get("MERMAID_VALIDATE", "0").strip() != "1"
_MERMAID_PY_PROBED = True  # Skip probing — rely on env var


def _warn_pythonmonkey_unavailable_once() -> None:
    global _pm_unavailable_warned
    if _pm_unavailable_warned:
        return
    _pm_unavailable_warned = True
    logger.warning(
        "PythonMonkey Mermaid validation is unavailable; trying local Node, "
        "then remote validation if MERMAID_VALIDATE=1."
    )


def _warn_node_missing_once() -> None:
    global _node_missing_warned
    if _node_missing_warned:
        return
    _node_missing_warned = True
    logger.warning(
        "Node.js was not found on PATH (override with CODEWIKI_NODE_PATH); "
        "local Node Mermaid validation cannot run."
    )


def _warn_mermaid_remote_disabled_once() -> None:
    global _mermaid_remote_disabled_warned
    if _mermaid_remote_disabled_warned:
        return
    _mermaid_remote_disabled_warned = True
    logger.warning(
        "Remote Mermaid validation (mermaid.ink) failed or timed out; "
        "skipping further remote validation for this run."
    )


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
    caller can fall back to the Node validator, then mermaid-py.
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
        # once so later diagrams skip straight to Node / mermaid-py.
        if "cannot find a running Python event-loop" in error_str:
            _PYTHONMONKEY_BROKEN = True
            _warn_pythonmonkey_unavailable_once()
            return None
        match = re.search(r"Error:(.*?)(?=Stack Trace:|$)", error_str, re.DOTALL)
        if match:
            return match.group(0).strip()
        # Unknown error from the JS parser — fall back rather than surface it.
        return None
    finally:
        _quiet_stderr_exit()


async def _parse_with_pythonmonkey(diagram_content: str) -> None:
    """Parse on the main thread; importing here keeps the import there too."""
    try:
        from mermaid_parser.parser import parse_mermaid_py
    except Exception as e:
        raise ImportError("PythonMonkey is unavailable") from e
    await parse_mermaid_py(diagram_content)


def _mermaid_node_dir() -> Path:
    import codewiki

    return Path(codewiki.__file__).resolve().parent / "mermaid_node"


def _resolve_node_executable() -> str | None:
    override = os.environ.get("CODEWIKI_NODE_PATH", "").strip()
    if override:
        path = Path(override)
        return str(path) if path.is_file() else None
    return shutil.which("node")


def _ensure_mermaid_node_dependencies(node_dir: Path) -> bool:
    global _node_validator_broken
    if _node_validator_broken:
        return False
    marker = node_dir / "node_modules" / "mermaid"
    if marker.is_dir():
        return True
    with _node_deps_lock:
        if marker.is_dir():
            return True
        if _node_validator_broken:
            return False
        npm = shutil.which("npm")
        if npm is None:
            logger.debug("npm not found; cannot install bundled mermaid_node dependencies")
            return False
        for cmd in (
            [npm, "ci", "--omit=dev"],
            [npm, "install", "--omit=dev"],
        ):
            try:
                result = subprocess.run(
                    cmd,
                    cwd=node_dir,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.debug("npm install for mermaid_node failed: %s", exc)
                continue
            if result.returncode == 0 and marker.is_dir():
                return True
        logger.debug(
            "Could not install mermaid npm package under %s; Node validation unavailable",
            node_dir,
        )
        _node_validator_broken = True
        return False


def _parse_via_node_validator(diagram_content: str) -> str | None:
    """Validate via Node + validate.mjs. None if Node/script cannot run."""
    global _node_validator_broken
    if _node_validator_broken:
        return None
    node = _resolve_node_executable()
    if node is None:
        return None
    node_dir = _mermaid_node_dir()
    script = node_dir / "validate.mjs"
    if not script.is_file():
        logger.debug("Missing %s; skipping Node mermaid validation", script)
        return None
    if not _ensure_mermaid_node_dependencies(node_dir):
        return None
    try:
        result = subprocess.run(
            [node, str(script)],
            cwd=node_dir,
            input=diagram_content,
            capture_output=True,
            text=True,
            timeout=_MERMAID_VALIDATE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return ""
    err = (result.stderr or result.stdout or "").strip()
    if err:
        return err
    return "Mermaid parse failed (no error message from Node validator)"


async def _try_node_parse(diagram_content: str) -> str | None:
    try:
        return await asyncio.wait_for(
            _run_in_daemon_thread(_parse_via_node_validator, diagram_content),
            timeout=_MERMAID_VALIDATE_TIMEOUT_SECONDS + 5.0,
        )
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None


def _parse_via_mermaid_py(diagram_content: str) -> str:
    """Validate via mermaid-py. Returns parse-error text, or "" if valid.

    mermaid-py raises MermaidError on parse failure and returns an SVG body
    on success — we must drive the result off the exception, not the body
    text, otherwise a successful SVG gets reported as a parse error.
    """
    import mermaid as md

    try:
        md.Mermaid(diagram_content)
        return ""
    except Exception as e:
        return str(e)


def _run_in_daemon_thread(fn, *args) -> "asyncio.Future":
    """Run fn in a daemon thread and return an awaitable for its result.

    Unlike asyncio.to_thread, a thread left hanging after wait_for times out
    (mermaid-py's requests.get has no timeout) does not block interpreter exit.
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


async def validate_single_diagram(diagram_content: str, diagram_num: int, line_start: int) -> str:
    """
    Validate a single mermaid diagram.

    Args:
        diagram_content: The mermaid diagram content
        diagram_num: Diagram number for error reporting
        line_start: Starting line number in the file

    Returns:
        Error message if invalid, empty string if valid
    """
    global _MERMAID_PY_BROKEN
    core_error = await _try_pythonmonkey_parse(diagram_content)
    if core_error is None:
        if _resolve_node_executable() is None:
            _warn_node_missing_once()
        node_error = await _try_node_parse(diagram_content)
        if node_error is not None:
            core_error = node_error
    if core_error is None:
        if _MERMAID_PY_BROKEN:
            # Validation disabled/unavailable is not a syntax error — a
            # non-empty return here would make callers report valid diagrams
            # as broken and send agents into fix loops.
            return ""
        try:
            core_error = await asyncio.wait_for(
                _run_in_daemon_thread(_parse_via_mermaid_py, diagram_content),
                timeout=_MERMAID_VALIDATE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            # Inconclusive, not a parse failure. Latch so the remaining
            # diagrams (and later calls) don't each block 15s on the same
            # unreachable rendering service.
            _MERMAID_PY_BROKEN = True
            _warn_mermaid_remote_disabled_once()
            return ""
        except Exception as e:
            return f"  Diagram {diagram_num}: Exception during validation - {str(e)}"

    if not core_error:
        return ""

    line_match = re.search(r"line (\d+)", core_error)
    if line_match:
        error_line_in_diagram = int(line_match.group(1))
        actual_line_in_file = line_start + error_line_in_diagram
        newline = "\n"
        return f"Diagram {diagram_num}: Parse error on line {actual_line_in_file}:{newline}{newline.join(core_error.split(newline)[1:])}"
    return f"Diagram {diagram_num}: {core_error}"


if __name__ == "__main__":
    # Test with the provided file
    import asyncio

    test_file = "output/docs/SWE_agent-docs/agent_hooks.md"
    result = asyncio.run(validate_mermaid_diagrams(test_file, "agent_hooks.md"))
    print(result)
