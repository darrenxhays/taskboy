import base64
from unittest.mock import AsyncMock, patch

import pytest

from taskboy.dashboard.gitops import GitOpsError, commit_file


@pytest.mark.asyncio
async def test_commit_new_file_omits_sha():
    calls = []

    async def fake_request(method, url, token, payload=None):
        calls.append((method, url, payload))
        if method == "GET":
            return 404, {}
        return 201, {"commit": {"sha": "new123", "html_url": "https://github.com/c/new123"}}

    with patch("taskboy.dashboard.gitops._request", side_effect=fake_request):
        result = await commit_file("pat", "example-org/taskboy", "main", "config/personality_red.md", "hello", "msg", "boss@example.com")
    put_payload = calls[1][2]
    assert result["commit_sha"] == "new123"
    assert "sha" not in put_payload
    assert base64.b64decode(put_payload["content"]).decode() == "hello"


@pytest.mark.asyncio
async def test_commit_uses_configured_committer_identity():
    calls = []

    async def fake_request(method, url, token, payload=None):
        calls.append((method, url, payload))
        if method == "GET":
            return 404, {}
        return 201, {"commit": {"sha": "new123", "html_url": "https://github.com/c/new123"}}

    with patch("taskboy.dashboard.gitops._request", side_effect=fake_request):
        await commit_file("pat", "example-org/taskboy", "main", "config/personality_red.md", "hello", "msg", "boss@example.com", committer_name="Crimson Mission Control", committer_email="crimson@example.com")
    put_payload = calls[1][2]
    assert put_payload["committer"] == {"name": "Crimson Mission Control", "email": "crimson@example.com"}


@pytest.mark.asyncio
async def test_commit_update_sends_existing_sha():
    async def fake_request(method, url, token, payload=None):
        if method == "GET":
            return 200, {"sha": "old456", "content": base64.b64encode(b"previous").decode()}
        assert payload["sha"] == "old456"
        return 200, {"commit": {"sha": "upd789", "html_url": ""}}

    with patch("taskboy.dashboard.gitops._request", side_effect=fake_request):
        result = await commit_file("pat", "example-org/taskboy", "main", "config/config.yaml", "changed", "msg", "boss@example.com")
    assert result["commit_sha"] == "upd789"


@pytest.mark.asyncio
async def test_commit_short_circuits_when_content_matches():
    request = AsyncMock(return_value=(200, {"sha": "same", "content": base64.b64encode(b"identical").decode()}))
    with patch("taskboy.dashboard.gitops._request", new=request):
        result = await commit_file("pat", "example-org/taskboy", "main", "config/config.yaml", "identical", "msg", "boss@example.com")
    assert result["unchanged"] is True
    request.assert_awaited_once()


@pytest.mark.asyncio
async def test_commit_failure_raises():
    async def fake_request(method, url, token, payload=None):
        if method == "GET":
            return 404, {}
        return 403, {"message": "Resource not accessible by personal access token"}

    with patch("taskboy.dashboard.gitops._request", side_effect=fake_request):
        with pytest.raises(GitOpsError, match="status 403"):
            await commit_file("pat", "example-org/taskboy", "main", "config/config.yaml", "x", "msg", "boss@example.com")
