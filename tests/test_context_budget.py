"""Tests for keeping agent requests inside the model's context window."""

import asyncio

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from codewiki.src.be import context_budget
from codewiki.src.be.context_budget import (
    ELIDED_TOOL_RESULT,
    TRUNCATED_NOTE,
    estimate_tokens,
    fit_messages,
    input_budget,
    parse_context_error,
)
from codewiki.src.be.llm_services import _CACHE_UNSUPPORTED, CachingOpenAIModel

KIMI_ERROR = (
    'litellm.BadRequestError: Hosted_vllmException - {"object":"error","message":'
    "\"Requested token count exceeds the model's maximum context length of 262144 tokens. "
    "You requested a total of 266540 tokens: 233772 tokens from the input messages and "
    '32768 tokens for the completion."}'
)

BIG = "export class Foo { bar() { return 42; } }\n" * 400  # several thousand tokens


def _history(tool_results: int) -> list:
    messages = [
        ModelRequest(
            parts=[
                SystemPromptPart(content="You are a documentation agent."),
                UserPromptPart(content="Document module X."),
            ]
        )
    ]
    for n in range(tool_results):
        messages.append(
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="read_code_components", args="{}", tool_call_id=f"c{n}")
                ]
            )
        )
        messages.append(
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="read_code_components", content=BIG, tool_call_id=f"c{n}"
                    )
                ]
            )
        )
    return messages


def test_parse_kimi_vllm_error() -> None:
    error = parse_context_error({"message": KIMI_ERROR})
    assert error is not None
    assert error.limit == 262144
    assert error.input_tokens == 233772


def test_parse_openai_and_anthropic_errors() -> None:
    openai = parse_context_error(
        "This model's maximum context length is 128000 tokens. However, your messages "
        "resulted in 130512 tokens."
    )
    assert (openai.limit, openai.input_tokens) == (128000, 130512)
    anthropic = parse_context_error("prompt is too long: 210000 tokens > 200000 maximum")
    assert (anthropic.limit, anthropic.input_tokens) == (200000, 210000)


def test_unrelated_errors_are_not_context_errors() -> None:
    assert parse_context_error("bad cache_control") is None
    assert parse_context_error("rate limited") is None


def test_input_budget_reserves_output_and_margin() -> None:
    assert input_budget(0, 32768) == 0
    budget = input_budget(262144, 32768)
    assert 0 < budget < 262144 - 32768
    assert input_budget(262144, 32768, token_ratio=1.2) < budget


def test_history_that_fits_is_returned_unchanged() -> None:
    messages = _history(2)
    assert fit_messages(messages, estimate_tokens(messages) + 1) is messages


def test_oldest_tool_results_are_elided_first() -> None:
    messages = _history(4)
    one_result = estimate_tokens(messages) - estimate_tokens(_history(3))
    trimmed = fit_messages(messages, estimate_tokens(messages) - one_result)

    assert trimmed is not messages
    contents = [
        part.content
        for message in trimmed
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert contents[0] == ELIDED_TOOL_RESULT
    assert contents[-1] == BIG  # the result the model is about to read is kept
    assert estimate_tokens(trimmed) <= estimate_tokens(messages) - one_result
    # Tool-call pairing survives, and the stored history is untouched.
    assert [p.tool_call_id for m in trimmed for p in m.parts if isinstance(p, ToolReturnPart)] == [
        "c0",
        "c1",
        "c2",
        "c3",
    ]
    assert messages[2].parts[0].content == BIG


def test_oversized_prompt_is_truncated_as_last_resort() -> None:
    messages = [ModelRequest(parts=[UserPromptPart(content=BIG * 4)])]
    budget = estimate_tokens(messages) // 4
    trimmed = fit_messages(messages, budget)
    assert trimmed[0].parts[0].content.endswith(TRUNCATED_NOTE)
    assert estimate_tokens(trimmed) <= budget * 1.05


def _make_model(context_window: int = 0) -> CachingOpenAIModel:
    return CachingOpenAIModel(
        model_name="kimi-test",
        prompt_caching=True,
        cache_registry_key="http://kimi-endpoint",
        context_window=context_window,
        provider=OpenAIProvider(base_url="http://localhost:1/v1", api_key="test-key"),
    )


def _run(model, messages, settings):
    return asyncio.run(
        model._completions_create(messages, False, settings, ModelRequestParameters())
    )


def test_context_error_is_learned_and_request_retried_trimmed() -> None:
    _CACHE_UNSUPPORTED.clear()
    context_budget._LEARNED_LIMITS.clear()
    messages = _history(6)
    full = estimate_tokens(messages)
    max_output = 1000
    # A window that the full history overflows but a trimmed one fits in.
    window = int(full * 0.6) + max_output
    sent = []

    async def fake_create(self, msgs, stream, model_settings, model_request_parameters):
        sent.append(estimate_tokens(msgs))
        if estimate_tokens(msgs) + max_output > window:
            raise ModelHTTPError(
                status_code=400,
                model_name=self.model_name,
                body={
                    "message": f"maximum context length of {window} tokens. "
                    f"{estimate_tokens(msgs)} tokens from the input messages"
                },
            )
        return "ok"

    original = OpenAIChatModel._completions_create
    OpenAIChatModel._completions_create = fake_create
    try:
        settings = {"max_tokens": max_output}
        assert _run(_make_model(), messages, settings) == "ok"
        assert sent[0] == full and sent[-1] + max_output <= window
        # A context error is not blamed on prompt-caching markers.
        assert not _CACHE_UNSUPPORTED

        # The learned window applies to new model instances: one call, pre-trimmed.
        sent.clear()
        assert _run(_make_model(), messages, settings) == "ok"
        assert len(sent) == 1
    finally:
        OpenAIChatModel._completions_create = original
        _CACHE_UNSUPPORTED.clear()
        context_budget._LEARNED_LIMITS.clear()


def test_configured_window_trims_before_the_first_request() -> None:
    context_budget._LEARNED_LIMITS.clear()
    messages = _history(6)
    full = estimate_tokens(messages)
    sent = []

    async def fake_create(self, msgs, stream, model_settings, model_request_parameters):
        sent.append(estimate_tokens(msgs))
        return "ok"

    original = OpenAIChatModel._completions_create
    OpenAIChatModel._completions_create = fake_create
    try:
        model = _make_model(context_window=full // 2 + 1000)
        assert _run(model, messages, {"max_tokens": 1000}) == "ok"
        assert len(sent) == 1 and sent[0] < full // 2
    finally:
        OpenAIChatModel._completions_create = original


def test_context_error_that_cannot_be_trimmed_is_raised() -> None:
    context_budget._LEARNED_LIMITS.clear()

    async def always_too_long(self, msgs, stream, model_settings, model_request_parameters):
        raise ModelHTTPError(
            status_code=400, model_name=self.model_name, body="maximum context length is 10 tokens"
        )

    tiny = [ModelRequest(parts=[UserPromptPart(content="hi")])]
    original = OpenAIChatModel._completions_create
    OpenAIChatModel._completions_create = always_too_long
    try:
        raised = False
        try:
            _run(_make_model(), tiny, {"max_tokens": 5})
        except ModelHTTPError:
            raised = True
        assert raised
    finally:
        OpenAIChatModel._completions_create = original
        context_budget._LEARNED_LIMITS.clear()
