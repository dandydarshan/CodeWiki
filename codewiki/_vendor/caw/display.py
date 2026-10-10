"""Rich console display for live agent output."""

from __future__ import annotations

import json
import os
import threading
from enum import Enum

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from codewiki._vendor.caw.models import TextBlock, ThinkingBlock, ToolUse, UsageStats

LOG_ENV_VAR = "CAW_LOG"


class DisplayMode(str, Enum):
    """Print modes for agent output."""

    FULL = "full"
    SHORT = "short"
    RESULT = "result"
    OFF = "off"


def _truncate(text: str, max_len: int = 40) -> str:
    """Truncate text to max_len chars, adding ellipsis if needed."""
    text = text.replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _first_n_lines(text: str, n: int = 3) -> str:
    """Return the first n lines of text."""
    lines = text.splitlines()
    if len(lines) <= n:
        return text
    return "\n".join(lines[:n]) + f"\n… ({len(lines) - n} more lines)"


class Display:
    """Mode-aware console display for agent events.

    Uses ``rich.text.Text`` objects (not markup strings) to avoid
    escaping issues with model output containing ``[brackets]``.
    """

    def __init__(self, mode: DisplayMode | str = DisplayMode.SHORT) -> None:
        if isinstance(mode, str):
            mode = DisplayMode(mode)
        self.mode = mode
        self.console = Console()
        self._lock = threading.RLock()
        self._last_result_text: str = ""
        self._pending_text: TextBlock | None = None

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def on_metadata(self, **kwargs: str) -> None:
        """Print metadata key-value pairs (agent, model, session, etc.)."""
        with self._lock:
            if self.mode == DisplayMode.OFF:
                return
            pairs = [f"{k}={v}" for k, v in kwargs.items() if v]
            if not pairs:
                return
            line = Text()
            line.append("[Metadata] ", style="dim bold")
            line.append("  ".join(pairs), style="dim")
            self.console.print(line)

    def _flush_pending_text(self, bold: bool = False) -> None:
        """Print any buffered text block. Called with bold=True for the final one."""
        if self._pending_text is None:
            return
        block = self._pending_text
        self._pending_text = None

        style = "bold" if bold else ""
        line = Text()
        line.append("[Assistant] ", style="bold blue")
        if self.mode == DisplayMode.FULL:
            line.append(block.text, style=style)
        else:
            line.append(_truncate(block.text), style=style)
        self.console.print(line)

    def on_user_message(self, message: str) -> None:
        """Print the user's message."""
        with self._lock:
            if self.mode in (DisplayMode.RESULT, DisplayMode.OFF):
                return

            line = Text()
            line.append("[User] ", style="bold green")
            if self.mode == DisplayMode.FULL:
                line.append(message, style="bold")
            else:
                line.append(_truncate(message), style="bold")
            self.console.print(line)

    def on_text(self, block: TextBlock) -> None:
        """Buffer an assistant text block (printed on next event or at turn end)."""
        with self._lock:
            if self.mode == DisplayMode.OFF:
                return

            if self.mode == DisplayMode.RESULT:
                self._last_result_text = block.text
                return

            # Flush any previous text block (not the final one, so not bold)
            self._flush_pending_text(bold=False)
            self._pending_text = block

    def on_thinking(self, block: ThinkingBlock) -> None:
        """Print a thinking block."""
        with self._lock:
            if self.mode in (DisplayMode.RESULT, DisplayMode.OFF):
                return

            line = Text()
            line.append("[Thinking] ", style="dim magenta")
            if self.mode == DisplayMode.FULL:
                line.append(block.text, style="dim")
            else:
                line.append(_truncate(block.text), style="dim")
            self.console.print(line)

    def on_tool_call(self, block: ToolUse) -> None:
        """Print a tool call (args only — result not yet known)."""
        with self._lock:
            if self.mode in (DisplayMode.RESULT, DisplayMode.OFF):
                return

            self._flush_pending_text(bold=False)

            line = Text()
            line.append("[Tool] ", style="bold yellow")
            line.append(block.name, style="bold cyan")
            line.append(" ")

            if self.mode == DisplayMode.FULL:
                args_str = json.dumps(block.arguments, indent=2)
                line.append(args_str, style="dim")
            else:
                args_str = json.dumps(block.arguments, separators=(",", ":"))
                line.append(_truncate(args_str), style="dim")
            self.console.print(line)

    def on_tool_result(self, block: ToolUse) -> None:
        """Print a tool result (output now available on the block)."""
        with self._lock:
            if self.mode in (DisplayMode.RESULT, DisplayMode.OFF):
                return

            tag_style = "bold red" if block.is_error else "bold yellow"
            line = Text()
            line.append("[Result] ", style=tag_style)
            line.append(block.name, style="bold cyan")

            output = block.output
            if output:
                line.append("\n")
                text = self.mode == DisplayMode.FULL and output or _first_n_lines(output)
                # Parse ANSI escapes so colorful tool output keeps its colors;
                # uncolored portions fall back to the dim base style.
                result_text = Text.from_ansi(text, style="dim")
                line.append_text(result_text)
            self.console.print(line)

    def on_turn_end(self, result: str, usage: UsageStats, duration_ms: int) -> None:
        """Print end-of-turn stats or deferred result text."""
        with self._lock:
            if self.mode == DisplayMode.OFF:
                return

            if self.mode == DisplayMode.RESULT:
                if self._last_result_text:
                    self.console.print(Panel(self._last_result_text, border_style="green", expand=False))
                    self._last_result_text = ""
                return

            # Flush the last text block as bold (it's the final assistant message)
            self._flush_pending_text(bold=True)

            # Stats as metadata
            tokens = f"{usage.input_tokens}in/{usage.output_tokens}out"
            meta: dict[str, str] = {
                "duration": f"{duration_ms}ms",
                "tokens": tokens,
            }
            if usage.cost_usd:
                meta["cost"] = f"${usage.cost_usd:.4f}"
            self.on_metadata(**meta)


# -- Global display singleton --------------------------------------------------

_global_display: Display | None = None
_global_display_resolved = False


def get_global_display() -> Display | None:
    """Return the global Display. Falls back to CAW_LOG env var on first call."""
    global _global_display, _global_display_resolved
    if _global_display is None and not _global_display_resolved:
        _global_display_resolved = True
        env_mode = os.environ.get(LOG_ENV_VAR, "short")
        _global_display = Display(mode=env_mode)
    return _global_display


def set_global_display(display: Display | None) -> None:
    """Set (or clear) the global Display instance."""
    global _global_display, _global_display_resolved
    _global_display = display
    _global_display_resolved = True
