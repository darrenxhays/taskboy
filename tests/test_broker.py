import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from taskboy import workspace
from taskboy.adapters.github_api import GitHubStatusError
from taskboy.broker import PROFILE_PERMISSIONS, CredentialBroker
from taskboy.models import QUEUED, RECEIVED
from taskboy.redact import redactor

APPROVED = ["org/service-a", "org/service-b"]


@pytest.fixture(scope="module")
def rsa_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8, encryption_algorithm=serialization.NoEncryption())
    public_pem = key.public_key().public_bytes(encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo)
    return private_pem.decode(), public_pem.decode()


@pytest.fixture
def broker(rsa_key):
    import shutil
    import tempfile

    # af_unix paths are capped at ~104 chars on macos; pytest's tmp_path is too deep
    socket_dir = tempfile.mkdtemp(prefix="ar-brk-")
    private_pem, _ = rsa_key
    b = CredentialBroker("12345", "678", private_pem, str(Path(socket_dir) / "b.sock"), "/opt/taskboy/bin/git-cred-helper")
    b._post_github = AsyncMock(return_value={"token": "ghs_mintedtoken000000000000000000000000", "expires_at": "2099-01-01T00:00:00Z"})
    yield b
    shutil.rmtree(socket_dir, ignore_errors=True)


def routed(store, make_task, profile="standard", targets=None):
    task = make_task()
    return store.transition(task.task_id, RECEIVED, QUEUED, "classified", profile=profile, classification_json=json.dumps({"target_repos": targets or []}))


def test_register_scopes_token_to_profile_and_targets(store, make_task, broker):
    task = routed(store, make_task, profile="read_only", targets=["org/service-a"])
    env = broker.register_task(task, APPROVED, hooks_path="/ws/t1/githooks")
    grant = broker.grants[task.task_id]
    assert grant.permissions == PROFILE_PERMISSIONS["read_only"]
    assert grant.repositories == ["service-a"]
    assert env["TASKBOY_TASK_NONCE"] == grant.nonce
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert env["GIT_CONFIG_KEY_1"] == "core.hooksPath"
    assert env["GIT_CONFIG_VALUE_1"] == "/ws/t1/githooks"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_register_task_env_fires_workspace_pre_push_hook(store, make_task, broker, tmp_path):
    """a real git push through the injected env must run the workspace hook."""
    ws = workspace.create(str(tmp_path / "ws"), "t1")
    env = broker.register_task(routed(store, make_task), APPROVED, hooks_path=str(workspace.hooks_dir(ws)))
    remote, clone = tmp_path / "remote.git", tmp_path / "clone"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-qm", "x"], cwd=clone, check=True)
    git_env = {**os.environ, **{key: value for key, value in env.items() if key.startswith("GIT_")}}
    denied = subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=clone, env=git_env, capture_output=True, text=True)
    allowed = subprocess.run(["git", "push", "origin", "HEAD:agent/t1-ok"], cwd=clone, env=git_env, capture_output=True, text=True)
    assert denied.returncode != 0
    assert "may only push agent/ branches" in denied.stderr
    assert allowed.returncode == 0, allowed.stderr


def test_register_ignores_unapproved_targets(store, make_task, broker):
    task = routed(store, make_task, targets=["org/not-approved"])
    broker.register_task(task, APPROVED, hooks_path="/ws/t1/githooks")
    # nothing approved matched: falls back to the full approved list, never the unapproved repo
    assert broker.grants[task.task_id].repositories == ["service-a", "service-b"]


def test_register_includes_operator_granted_repos_in_token_scope(store, make_task, broker):
    task = routed(store, make_task, profile="read_only", targets=["org/service-a"])
    broker.register_task(task, APPROVED, granted_repos=["org/service-b"], hooks_path="/ws/t1/githooks")
    # the operator-granted repo joins the classification target in the minted token's scope, so
    # mid-session git ops against it authenticate instead of 403ing (GIT-014, §8.4)
    assert broker.grants[task.task_id].repositories == ["service-a", "service-b"]


@pytest.mark.asyncio
async def test_tokens_are_cached_and_refreshed_near_expiry(store, make_task, broker):
    task = routed(store, make_task)
    broker.register_task(task, APPROVED, hooks_path="/ws/t1/githooks")
    token1 = await broker.token_for_task(task.task_id)
    token2 = await broker.token_for_task(task.task_id)
    assert token1 == token2
    assert broker._post_github.call_count == 1
    broker.grants[task.task_id].expires_at = time.time() + 60  # inside the 10-minute refresh margin
    await broker.token_for_task(task.task_id)
    assert broker._post_github.call_count == 2


@pytest.mark.asyncio
async def test_minted_tokens_are_redacted_and_released(store, make_task, broker):
    task = routed(store, make_task)
    broker.register_task(task, APPROVED, hooks_path="/ws/t1/githooks")
    token = await broker.token_for_task(task.task_id)
    assert redactor.redact(f"log line with {token}") == "log line with [redacted]"
    broker.release_task(task.task_id)
    assert task.task_id not in broker.grants
    with pytest.raises(PermissionError):
        await broker.token_for_task(task.task_id)


@pytest.mark.asyncio
async def test_unknown_nonce_is_refused(broker):
    with pytest.raises(PermissionError):
        await broker.credentials_for_nonce("not-a-real-nonce")


@pytest.mark.asyncio
async def test_read_token_is_fetch_only_repo_scoped_and_redacted(broker):
    token, expires_at = await broker.read_token(["org/service-a", "org/service-b"])
    payload = broker._post_github.call_args.args[2]
    assert payload == {"permissions": {"contents": "read", "metadata": "read"}, "repositories": ["service-a", "service-b"]}
    assert redactor.redact(f"token={token}") == "token=[redacted]"
    assert expires_at == datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp()


@pytest.mark.asyncio
async def test_read_token_accepts_explicit_permissions(broker):
    await broker.read_token(APPROVED, permissions={"pull_requests": "read", "metadata": "read"})
    assert broker._post_github.call_args.args[2]["permissions"] == {"pull_requests": "read", "metadata": "read"}


@pytest.mark.asyncio
async def test_read_token_falls_back_to_55_minute_expiry_when_field_absent(broker):
    broker._post_github = AsyncMock(return_value={"token": "ghs_notimeleft00000000000000000000000"})
    before = time.time()
    _, expires_at = await broker.read_token(APPROVED)
    assert 55 * 60 - 5 <= expires_at - before <= 55 * 60 + 5


@pytest.mark.asyncio
async def test_app_slug_is_fetched_once_and_cached(broker):
    broker._get_github = AsyncMock(return_value={"slug": "red-app"})
    assert await broker.app_slug() == "red-app"
    assert await broker.app_slug() == "red-app"
    broker._get_github.assert_awaited_once()


def test_app_jwt_is_valid_rs256(rsa_key, broker):
    _, public_pem = rsa_key
    claims = jwt.decode(broker._app_jwt(), public_pem, algorithms=["RS256"])
    assert claims["iss"] == "12345"
    assert claims["exp"] - claims["iat"] == 600


@pytest.mark.asyncio
async def test_socket_roundtrip_and_helper_script(store, make_task, broker, tmp_path):
    task = routed(store, make_task)
    env = broker.register_task(task, APPROVED, hooks_path="/ws/t1/githooks")
    await broker.start()
    try:
        # raw socket round-trip
        reader, writer = await asyncio.open_unix_connection(broker.socket_path)
        writer.write((json.dumps({"op": "git-credentials", "nonce": env["TASKBOY_TASK_NONCE"]}) + "\n").encode())
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        assert response == {"username": "x-access-token", "password": "ghs_mintedtoken000000000000000000000000"}

        # the real credential helper script, exactly as git would invoke it
        helper = Path(__file__).parents[1] / "taskboy" / "deploy" / "git-cred-helper.py"
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, str(helper), "get"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            text=True,
            env={"TASKBOY_BROKER_SOCKET": broker.socket_path, "TASKBOY_TASK_NONCE": env["TASKBOY_TASK_NONCE"]},
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert "username=x-access-token" in result.stdout
        assert "password=ghs_mintedtoken" in result.stdout

        # a bad nonce gets an error and a non-zero exit
        bad = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, str(helper), "get"],
            input="\n",
            capture_output=True,
            text=True,
            env={"TASKBOY_BROKER_SOCKET": broker.socket_path, "TASKBOY_TASK_NONCE": "stolen-or-stale"},
            timeout=10,
        )
        assert bad.returncode == 1
        assert "password" not in bad.stdout
    finally:
        await broker.stop()


@pytest.mark.asyncio
async def test_verify_discovers_accessible_repos_and_warns_on_drift(broker, caplog):
    broker._get_github = AsyncMock(return_value={"repositories": [{"name": "alpha", "full_name": "org/alpha"}]})

    await broker.verify(["org/alpha", "org/beta"])

    assert broker.accessible_repos == {"alpha"}
    assert "org/beta" in caplog.text
    assert "not accessible" in caplog.text


@pytest.mark.asyncio
async def test_verify_leaves_accessible_repos_none_when_listing_fails(broker, caplog):
    broker._get_github = AsyncMock(side_effect=RuntimeError("github api GET /installation/repositories failed: 500 — boom"))

    await broker.verify(["org/alpha"])  # the mint probe still succeeds; only the listing failed

    assert broker.accessible_repos is None
    assert "drift check disabled" in caplog.text


@pytest.mark.asyncio
async def test_mint_422_retries_with_only_accessible_repos(broker):
    broker.accessible_repos = {"alpha"}
    error = GitHubStatusError(422, 'github token mint failed: 422 — {"message":"There is at least one repository that does not exist or is not accessible to the parent installation."}')
    broker._post_github = AsyncMock(side_effect=[error, {"token": "ghs_retried00000000000000000000000000", "expires_at": "2099-01-01T00:00:00Z"}])

    data = await broker._mint_token({"permissions": PROFILE_PERMISSIONS["standard"], "repositories": ["alpha", "core"]})

    assert data["token"] == "ghs_retried00000000000000000000000000"
    retry_payload = broker._post_github.call_args.args[2]
    assert retry_payload["repositories"] == ["alpha"]


@pytest.mark.asyncio
async def test_mint_422_all_inaccessible_raises_actionable_error(broker):
    broker.accessible_repos = {"alpha"}
    error = GitHubStatusError(422, 'github token mint failed: 422 — {"message":"There is at least one repository that does not exist or is not accessible to the parent installation."}')
    broker._post_github = AsyncMock(side_effect=error)

    with pytest.raises(RuntimeError, match="not installed on core"):
        await broker._mint_token({"permissions": PROFILE_PERMISSIONS["standard"], "repositories": ["core"]})


@pytest.mark.asyncio
async def test_mint_422_reraises_unchanged_when_fallback_does_not_apply(broker):
    not_422 = GitHubStatusError(500, "github token mint failed: 500 — server error")
    broker._post_github = AsyncMock(side_effect=not_422)
    broker.accessible_repos = {"alpha"}
    with pytest.raises(GitHubStatusError):
        await broker._mint_token({"permissions": PROFILE_PERMISSIONS["standard"], "repositories": ["core"]})

    no_marker = GitHubStatusError(422, "github token mint failed: 422 — some other reason entirely")
    broker._post_github = AsyncMock(side_effect=no_marker)
    with pytest.raises(GitHubStatusError):
        await broker._mint_token({"permissions": PROFILE_PERMISSIONS["standard"], "repositories": ["core"]})

    accessible_unknown = GitHubStatusError(422, 'github token mint failed: 422 — {"message":"There is at least one repository that does not exist or is not accessible to the parent installation."}')
    broker._post_github = AsyncMock(side_effect=accessible_unknown)
    broker.accessible_repos = None
    with pytest.raises(GitHubStatusError):
        await broker._mint_token({"permissions": PROFILE_PERMISSIONS["standard"], "repositories": ["core"]})
