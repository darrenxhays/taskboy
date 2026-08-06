from unittest.mock import AsyncMock

import pytest

from taskboy.adapters.confluence import ConfluenceAdapter


@pytest.fixture
def adapter(store, make_task):
    value = ConfluenceAdapter(store, make_task(), "https://example.atlassian.net", "red@example.com", "token", ["ENG", "OPS"])
    value._request = AsyncMock()
    return value


@pytest.mark.asyncio
async def test_search_pages_applies_space_allowlist(adapter):
    adapter._request.return_value = {"results": [{"id": "12", "title": "Runbook", "space": {"key": "OPS"}, "version": {"number": 3}}]}
    result = await adapter.search_pages({"cql": 'type = "page"', "max_results": 100})
    params = adapter._request.call_args.kwargs["params"]
    assert 'space in ("ENG", "OPS")' in params["cql"]
    assert params["limit"] == 25
    assert "12: Runbook [OPS] v3" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_get_page_refuses_off_allowlist_space(adapter):
    adapter._request.return_value = {"id": "1", "title": "Secret", "space": {"key": "HR"}, "body": {"storage": {"value": "<p>private</p>"}}}
    result = await adapter.get_page({"page_id": "1"})
    assert result["isError"] is True
    assert "not on the approved list" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_get_page_strips_html_redacts_and_bounds(adapter):
    adapter._request.return_value = {
        "id": "2",
        "title": "Guide",
        "space": {"key": "ENG"},
        "version": {"number": 4},
        "body": {"storage": {"value": "<h1>Deploy</h1><p>token ghp_abcdefghijklmnopqrstuvwxyz0123456789</p><ul><li>" + "x" * 5000 + "</li></ul>"}},
    }
    result = await adapter.get_page({"page_id": "2"})
    text = result["content"][0]["text"]
    assert "<h1>" not in text
    assert "Deploy" in text
    assert "ghp_" not in text
    assert len(text) <= 4000
