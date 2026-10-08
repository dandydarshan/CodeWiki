"""Keep agent requests inside the model's context window.

An agent's message history grows with every tool result (read_code_components,
file views, ...) and nothing else bounds it, so a long module run eventually
sends more input than the model accepts — the provider answers HTTP 400
"maximum context length ... exceeded" and the whole module fails.

Before each request, :func:`fit_messages` makes the history fit an input-token
budget: it first replaces the oldest tool results with a short note (the agent
can re-read them), and only as a last resort truncates the largest remaining
part. The stored history is never mutated; trimming is applied per request.

The window comes from ``Config.max_context_tokens`` or is learned from the
provider's context-length error, and is remembered per (endpoint, model).
"""

import logging
import re
from dataclasses import dataclass, replace

from pydantic_ai.messages import (
    ModelRequest,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from codewiki.src.be.utils import count_tokens

logger = logging.getLogger(__name__)

# Our tokenizer (tiktoken) differs from the provider's; keep this much of the
# window free until a provider error tells us the real ratio.
SAFETY_FACTOR = 0.9
# Rough per-part overhead of the chat format (role tags, tool-call ids, ...).
PART_OVERHEAD_TOKENS = 8
# Tool results smaller than this are cheaper to keep than to re-read.
MIN_ELIDE_TOKENS = 200
# Never truncate a part below this many tokens.
MIN_TRUNCATED_TOKENS = 256
# When the provider rejects a request without stating its limit, retry at
# this fraction of the request's estimated size.
UNKNOWN_LIMIT_SHRINK = 0.75

ELIDED_TOOL_RESULT = (
    "[Earlier tool output removed to fit the model's context window. "
    "Call the tool again if you still need this content.]"
)
TRUNCATED_NOTE = (
    "\n... [content truncated to fit the model's context window — use your "
    "tools to read the rest if you need it]"
)

_CONTEXT_ERROR = re.compile(
    r"context[ _]length|context window|maximum context|prompt is too long"
    r"|input is too long|too many (?:input )?tokens",
    re.IGNORECASE,
)
_LIMIT_PATTERNS = (
    # vLLM / OpenAI: "maximum context length of 262144 tokens" / "... is 128000 tokens"
    re.compile(r"maximum context length (?:is|of) (\d+)", re.IGNORECASE),
    re.compile(r"context (?:window|length) (?:is|of) (\d+)", re.IGNORECASE),
    # Anthropic: "prompt is too long: 210000 tokens > 200000 maximum"
    re.compile(r"\d+ tokens? > (\d+) maximum", re.IGNORECASE),
)
_INPUT_PATTERNS = (
    re.compile(r"(\d+) tokens from the input", re.IGNORECASE),  # vLLM
    re.compile(r"messages resulted in (\d+) tokens", re.IGNORECASE),  # OpenAI
    re.compile(r"prompt is too long: (\d+) tokens", re.IGNORECASE),  # Anthropic
)


@dataclass
class ContextLimit:
    """What we know about one (endpoint, model) pair's context window."""

    window: int = 0
    # Provider tokens per tiktoken token, learned from an error message (>= 1).
    token_ratio: float = 1.0


# Module-level: sub-agents re-create model instances per tool call, and the
# window should only have to be learned once per endpoint/model.
_LEARNED_LIMITS: dict[tuple[str, str], ContextLimit] = {}


@dataclass
class ContextError:
    limit: int | None
    input_tokens: int | None


def parse_context_error(body) -> ContextError | None:
    """Return the context-length details in a provider error body, or None."""
    text = str(body or "")
    if not _CONTEXT_ERROR.search(text):
        return None

    def first(patterns) -> int | None:
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                return int(match.group(1))
        return None

    return ContextError(limit=first(_LIMIT_PATTERNS), input_tokens=first(_INPUT_PATTERNS))


def get_limit(key: tuple[str, str]) -> ContextLimit:
    return _LEARNED_LIMITS.get(key, ContextLimit())


def learn_limit(key: tuple[str, str], error: ContextError, estimated_tokens: int) -> None:
    learned = _LEARNED_LIMITS.setdefault(key, ContextLimit())
    if error.limit:
        learned.window = error.limit
    if error.input_tokens and estimated_tokens:
        learned.token_ratio = max(learned.token_ratio, error.input_tokens / estimated_tokens)


def input_budget(window: int, max_output_tokens: int, token_ratio: float = 1.0) -> int:
    """Input tokens (in our tokenizer) a request may use, or 0 if unknown."""
    if window <= 0:
        return 0
    available = (window - max_output_tokens) * SAFETY_FACTOR
    return max(int(available / max(token_ratio, 1.0)), 0)


def _part_text(part) -> str | None:
    """The text a part contributes to the request, or None if not textual."""
    if isinstance(part, ToolReturnPart):
        return part.model_response_str()
    if isinstance(part, ToolCallPart):
        return part.args_as_json_str()
    content = getattr(part, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return "\n".join(item for item in content if isinstance(item, str))
    return None


def _part_tokens(part) -> int:
    text = _part_text(part)
    return PART_OVERHEAD_TOKENS + (count_tokens(text) if text else 0)


def estimate_tokens(messages: list) -> int:
    """Estimate the input tokens of a pydantic-ai message history."""
    return sum(_part_tokens(part) for message in messages for part in message.parts)


def _truncate(text: str, tokens: int, keep_tokens: int) -> str:
    keep_chars = max(int(len(text) * keep_tokens / max(tokens, 1)), 0)
    return text[:keep_chars] + TRUNCATED_NOTE


def fit_messages(messages: list, budget: int) -> list:
    """Return *messages* trimmed to about *budget* input tokens.

    Returns the same list object when nothing had to change, so callers can
    tell whether trimming happened. Pairing between tool calls and their
    results is preserved: results are shortened, never dropped.
    """
    if budget <= 0 or not messages:
        return messages
    parts = [list(message.parts) for message in messages]
    sizes = [[_part_tokens(part) for part in message_parts] for message_parts in parts]
    total = sum(map(sum, sizes))
    if total <= budget:
        return messages

    changed: set[int] = set()
    note_tokens = _part_tokens(ToolReturnPart(tool_name="", content=ELIDED_TOOL_RESULT))

    # 1. Elide the oldest tool results first; the latest request (what the
    #    model is about to respond to) is left intact.
    for i in range(len(parts) - 1):
        if total <= budget:
            break
        if not isinstance(messages[i], ModelRequest):
            continue
        for j, part in enumerate(parts[i]):
            if total <= budget:
                break
            if isinstance(part, ToolReturnPart) and sizes[i][j] > MIN_ELIDE_TOKENS:
                parts[i][j] = replace(part, content=ELIDED_TOOL_RESULT)
                total -= sizes[i][j] - note_tokens
                sizes[i][j] = note_tokens
                changed.add(i)

    # 2. Still too big: truncate the largest text parts (tool results, user
    #    prompts, retry prompts). System prompts and model output are kept.
    truncatable = (ToolReturnPart, UserPromptPart, RetryPromptPart)
    while total > budget:
        candidates = [
            (sizes[i][j], i, j)
            for i, message_parts in enumerate(parts)
            for j, part in enumerate(message_parts)
            if isinstance(part, truncatable)
            and isinstance(getattr(part, "content", None), str)
            and sizes[i][j] > MIN_TRUNCATED_TOKENS + PART_OVERHEAD_TOKENS
        ]
        if not candidates:
            break
        size, i, j = max(candidates)
        part = parts[i][j]
        keep = max(size - (total - budget) - PART_OVERHEAD_TOKENS, MIN_TRUNCATED_TOKENS)
        content = _truncate(part.content, size - PART_OVERHEAD_TOKENS, keep)
        parts[i][j] = replace(part, content=content)
        new_size = _part_tokens(parts[i][j])
        if new_size >= size:
            break
        total -= size - new_size
        sizes[i][j] = new_size
        changed.add(i)

    if not changed:
        return messages
    return [
        replace(message, parts=parts[i]) if i in changed else message
        for i, message in enumerate(messages)
    ]
