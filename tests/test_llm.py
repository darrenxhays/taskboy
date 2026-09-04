import json
import os
from types import SimpleNamespace

import pytest

from taskboy.llm import extract_usage, structured_call


@pytest.mark.asyncio
async def test_structured_call_uses_agent_sdk_and_cleans_temp_cwd(monkeypatch):
    captured = {}

    async def fake_query(*, prompt, options):
        captured["prompt"] = prompt
        captured["options"] = options
        captured["cwd_existed"] = os.path.isdir(options.cwd)
        usage = {"input_tokens": 12, "output_tokens": 4, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2}
        yield SimpleNamespace(structured_output={"answer": "ok"}, usage=usage, total_cost_usd=0.01)

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}

    result, usage = await structured_call("claude-haiku", "prompt", schema)

    assert result == {"answer": "ok"}
    assert captured["prompt"] == "prompt"
    assert captured["options"].model == "claude-haiku"
    assert captured["options"].output_format == {"type": "json_schema", "schema": schema}
    assert captured["options"].allowed_tools == []
    assert captured["cwd_existed"]
    assert not os.path.isdir(captured["options"].cwd)  # temp cwd removed after the call
    assert usage == {"input_tokens": 12, "output_tokens": 4, "cache_read_tokens": 3, "cache_write_tokens": 2, "cost_usd": 0.01}


@pytest.mark.asyncio
async def test_structured_call_parses_json_result_and_rejects_non_dict(monkeypatch):
    import claude_agent_sdk

    async def json_in_result(*, prompt, options):
        yield SimpleNamespace(structured_output=None, result='prose {"answer": "ok"} trailing', usage={}, total_cost_usd=None)

    monkeypatch.setattr(claude_agent_sdk, "query", json_in_result)
    result, _ = await structured_call("claude-haiku", "prompt", {"type": "object"})
    assert result == {"answer": "ok"}

    async def no_output(*, prompt, options):
        yield SimpleNamespace(structured_output=None, result="no json here", usage={}, total_cost_usd=None)

    monkeypatch.setattr(claude_agent_sdk, "query", no_output)
    with pytest.raises(ValueError):
        await structured_call("claude-haiku", "prompt", {"type": "object"})


@pytest.mark.asyncio
async def test_structured_call_retries_once_then_succeeds(monkeypatch):
    import claude_agent_sdk

    calls = {"count": 0}
    retry_options = {}

    async def flaky(*, prompt, options):
        calls["count"] += 1
        if calls["count"] == 1:
            raise Exception("boom")
        retry_options["output_format"] = options.output_format
        yield SimpleNamespace(structured_output={"answer": "ok"}, usage={}, total_cost_usd=None)

    monkeypatch.setattr(claude_agent_sdk, "query", flaky)
    result, _ = await structured_call("claude-haiku", "prompt", {"type": "object"})

    assert result == {"answer": "ok"}
    assert calls["count"] == 2
    assert retry_options["output_format"] is not None  # non-tolerated failures retry with output_format intact


@pytest.mark.asyncio
async def test_structured_call_falls_back_to_text_parsing_when_no_frame_received(monkeypatch):
    """#93: the no-frame retry drops output_format and asks for plain-text JSON."""
    import claude_agent_sdk

    calls = []

    async def sdk(*, prompt, options):
        calls.append((prompt, options))
        if len(calls) == 1:
            raise Exception("Claude Code returned an error result: success")
            yield  # unreachable; makes this an async generator so `async for` works
        yield SimpleNamespace(structured_output=None, result='{"answer": "ok"}', usage={}, total_cost_usd=None)

    monkeypatch.setattr(claude_agent_sdk, "query", sdk)
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}

    result, _ = await structured_call("claude-haiku", "prompt", schema)

    assert result == {"answer": "ok"}
    assert len(calls) == 2
    first_prompt, first_options = calls[0]
    second_prompt, second_options = calls[1]
    assert first_options.output_format == {"type": "json_schema", "schema": schema}
    assert second_options.output_format is None  # second attempt no longer relies on the misbehaving path
    assert first_prompt == "prompt"
    assert json.dumps(schema) in second_prompt


@pytest.mark.asyncio
async def test_structured_call_tolerates_error_result_success_with_structured_output(monkeypatch):
    """issue #80: a usable result frame followed by the benign SDK error is used, not discarded."""
    import claude_agent_sdk

    calls = {"count": 0}

    async def success_result_then_stream_error(*, prompt, options):
        calls["count"] += 1
        yield SimpleNamespace(
            structured_output={"answer": "ok"},
            usage={"input_tokens": 5, "output_tokens": 1},
            total_cost_usd=0.002,
        )
        raise Exception("Claude Code returned an error result: success")

    monkeypatch.setattr(claude_agent_sdk, "query", success_result_then_stream_error)
    result, usage = await structured_call("claude-haiku", "prompt", {"type": "object"})

    assert result == {"answer": "ok"}
    assert usage["cost_usd"] == 0.002
    assert calls["count"] == 1  # tolerated inline, no wasted retry


@pytest.mark.asyncio
async def test_structured_call_does_not_tolerate_genuine_error_result(monkeypatch):
    """a genuine error's exception text differs from the tolerated shape, so it still raises and retries."""
    import claude_agent_sdk

    calls = {"count": 0}

    async def genuine_error(*, prompt, options):
        calls["count"] += 1
        yield SimpleNamespace()
        raise Exception("Claude Code returned an error result: overloaded_error")

    monkeypatch.setattr(claude_agent_sdk, "query", genuine_error)

    with pytest.raises(Exception, match="overloaded_error"):
        await structured_call("claude-haiku", "prompt", {"type": "object"})

    assert calls["count"] == 2  # exactly one retry, not tolerated


@pytest.mark.asyncio
async def test_structured_call_tolerates_error_result_success_with_trailing_session_state_frame(monkeypatch):
    """the SDK's trailing session_state_changed marker after the result must not clobber the usable frame before it."""
    import claude_agent_sdk
    from claude_agent_sdk import SystemMessage

    async def success_result_then_trailer_then_stream_error(*, prompt, options):
        yield SimpleNamespace(structured_output={"answer": "ok"}, usage={}, total_cost_usd=None)
        yield SystemMessage(subtype="session_state_changed", data={})
        raise Exception("Claude Code returned an error result: success")

    monkeypatch.setattr(claude_agent_sdk, "query", success_result_then_trailer_then_stream_error)
    result, _ = await structured_call("claude-haiku", "prompt", {"type": "object"})

    assert result == {"answer": "ok"}


@pytest.mark.asyncio
async def test_structured_call_reraises_tolerated_text_when_no_frame_received(monkeypatch):
    """the tolerated error text must still surface if no usable frame ever arrived."""
    import claude_agent_sdk

    calls = {"count": 0}

    async def error_before_any_frame(*, prompt, options):
        calls["count"] += 1
        raise Exception("Claude Code returned an error result: success")
        yield  # unreachable; makes this an async generator so `async for` works

    monkeypatch.setattr(claude_agent_sdk, "query", error_before_any_frame)

    with pytest.raises(Exception, match="Claude Code returned an error result: success"):
        await structured_call("claude-haiku", "prompt", {"type": "object"})

    assert calls["count"] == 2  # exactly one retry, not tolerated


@pytest.mark.asyncio
async def test_structured_call_falls_back_to_text_parsing_on_schema_rejection_400(monkeypatch):
    """#124: a deterministic 400 rejecting the output_format tool schema must not be retried identically."""
    import claude_agent_sdk

    calls = []

    async def sdk(*, prompt, options):
        calls.append((prompt, options))
        if len(calls) == 1:
            raise Exception("API Error: 400 tools.9.custom.input_schema: input_schema does not support oneOf, allOf, or anyOf at the top level")
            yield  # unreachable; makes this an async generator so `async for` works
        yield SimpleNamespace(structured_output=None, result='{"answer": "ok"}', usage={}, total_cost_usd=None)

    monkeypatch.setattr(claude_agent_sdk, "query", sdk)
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}

    result, _ = await structured_call("claude-haiku", "prompt", schema)

    assert result == {"answer": "ok"}
    assert len(calls) == 2
    first_prompt, first_options = calls[0]
    second_prompt, second_options = calls[1]
    assert first_options.output_format == {"type": "json_schema", "schema": schema}
    assert second_options.output_format is None  # second attempt drops the rejected tool schema
    assert first_prompt == "prompt"
    assert json.dumps(schema) in second_prompt


@pytest.mark.asyncio
async def test_structured_call_two_failures_raises_with_diagnostics(monkeypatch):
    import claude_agent_sdk

    calls = {"count": 0}

    async def always_bad(*, prompt, options):
        calls["count"] += 1
        yield SimpleNamespace(structured_output=None, subtype="success", result="prose with no json", usage={}, total_cost_usd=None)

    monkeypatch.setattr(claude_agent_sdk, "query", always_bad)

    with pytest.raises(ValueError) as excinfo:
        await structured_call("claude-haiku", "prompt", {"type": "object"})

    assert calls["count"] == 2  # exactly one retry, not a retry loop
    message = str(excinfo.value)
    assert "subtype='success'" in message
    assert "prose with no json" in message


@pytest.mark.asyncio
async def test_structured_call_redacts_secrets_in_diagnostics(monkeypatch):
    import claude_agent_sdk

    async def leaky(*, prompt, options):
        yield SimpleNamespace(structured_output=None, subtype="success", result="token ghp_" + "a" * 36, usage={}, total_cost_usd=None)

    monkeypatch.setattr(claude_agent_sdk, "query", leaky)

    with pytest.raises(ValueError) as excinfo:
        await structured_call("claude-haiku", "prompt", {"type": "object"})

    assert "ghp_" not in str(excinfo.value)
    assert "[redacted]" in str(excinfo.value)


def test_extract_usage_shapes():
    class WithUsage:
        usage = {"input_tokens": 1, "output_tokens": 2}
        total_cost_usd = 0.01

    class Bare:
        pass

    assert extract_usage(WithUsage())["cost_usd"] == 0.01
    assert extract_usage(Bare()) is None
