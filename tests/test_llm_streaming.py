"""call_llm streams OpenAI-compatible completions (no read timeout on long answers)."""

from types import SimpleNamespace

import httpx
import pytest
from openai import BadRequestError

from codewiki.src.be import llm_services


def chunk(content=None, finish_reason=None, usage=None):
    choices = [] if content is None and finish_reason is None else [
        SimpleNamespace(delta=SimpleNamespace(content=content), finish_reason=finish_reason)
    ]
    return SimpleNamespace(choices=choices, usage=usage)


def bad_request(message):
    request = httpx.Request("POST", "http://test/v1/chat/completions")
    response = httpx.Response(400, request=request)
    return BadRequestError(message, response=response, body={"error": {"message": message}})


class FakeCompletions:
    """Records each create() call and answers with the next scripted result."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def run(monkeypatch):
    def _run(*results):
        completions = FakeCompletions(*results)
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        monkeypatch.setattr(llm_services, "create_openai_client", lambda config: client)
        config = SimpleNamespace(
            main_model="m", llm_base_url="http://test/v1", llm_api_key="k", max_tokens=100,
            provider="openai-compatible",
        )
        llm_services.pop_last_usage()
        return llm_services.call_llm("hi", config), completions.calls

    return _run


def test_streams_and_assembles_content_and_usage(run):
    usage = SimpleNamespace(prompt_tokens=3, completion_tokens=4, total_tokens=7)
    stream = [chunk("Hel"), chunk("lo"), chunk(finish_reason="stop"), chunk(usage=usage)]
    text, calls = run(iter(stream))
    assert text == "Hello"
    assert calls[0]["stream"] is True
    assert calls[0]["stream_options"] == {"include_usage": True}
    assert llm_services.pop_last_usage() == {
        "prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7,
    }


def test_rejected_stream_options_retries_streaming_without_them(run):
    text, calls = run(bad_request("Unsupported parameter: stream_options"), iter([chunk("ok")]))
    assert text == "ok"
    assert calls[1]["stream"] is True and "stream_options" not in calls[1]


def test_rejected_streaming_falls_back_to_a_plain_request(run):
    plain = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="plain"), finish_reason="stop")],
        usage=None,
    )
    text, calls = run(bad_request("streaming is not supported"), plain)
    assert text == "plain"
    assert "stream" not in calls[1]


def test_empty_stream_returns_none(run):
    text, _ = run(iter([chunk(finish_reason="length")]))
    assert text is None


def test_other_bad_requests_still_raise(run):
    with pytest.raises(BadRequestError):
        run(bad_request("maximum context length exceeded"))


def test_broken_stream_logs_where_it_stopped_and_raises(run, caplog):
    def broken():
        yield chunk("partial answer")
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")

    with pytest.raises(httpx.RemoteProtocolError):
        run(broken())
    assert "broke off after" in caplog.text and "14 answer and 0 reasoning characters" in caplog.text


def test_empty_stop_reply_is_retried_once(run):
    text, calls = run(iter([chunk(finish_reason="stop")]), iter([chunk("second try")]))
    assert text == "second try" and len(calls) == 2


def test_empty_reply_twice_returns_none_after_one_retry(run):
    text, calls = run(iter([chunk(finish_reason="stop")]), iter([chunk(finish_reason="stop")]))
    assert text is None and len(calls) == 2


def test_reply_cut_off_at_max_tokens_is_not_retried(run):
    text, calls = run(iter([chunk(finish_reason="length")]))
    assert text is None and len(calls) == 1
