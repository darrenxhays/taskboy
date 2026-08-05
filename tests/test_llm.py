import os
from types import SimpleNamespace

import pytest

from agent_harness.llm import extract_usage, structured_call


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

    async def flaky(*, prompt, options):
        calls["count"] += 1
        if calls["count"] == 1:
            raise Exception("Claude Code returned an error result: success")
        yield SimpleNamespace(structured_output={"answer": "ok"}, usage={}, total_cost_usd=None)

    monkeypatch.setattr(claude_agent_sdk, "query", flaky)
    result, _ = await structured_call("claude-haiku", "prompt", {"type": "object"})

    assert result == {"answer": "ok"}
    assert calls["count"] == 2


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
