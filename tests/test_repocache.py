from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from taskboy import repocache
from taskboy.redact import Redactor


@pytest.mark.asyncio
async def test_refresh_clones_then_fetches_and_token_is_env_only(monkeypatch, tmp_path):
    broker = SimpleNamespace(read_token=AsyncMock(return_value=("secret-token", 9999999999.0)))
    run_git = AsyncMock(return_value="")
    monkeypatch.setattr(repocache, "_run_git", run_git)

    assert (await repocache.refresh_one(broker, tmp_path, "org/service")).ok is True
    args = run_git.call_args.args[0]
    env = run_git.call_args.kwargs["env"]
    assert args == ["clone", "--mirror", "https://github.com/org/service.git", str(tmp_path / "org" / "service.git")]
    assert "secret-token" not in " ".join(args)
    assert "secret-token" not in str(tmp_path / "org" / "service.git")
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert run_git.call_args.kwargs["timeout"] == 300

    (tmp_path / "org" / "service.git").mkdir()
    run_git.reset_mock()
    assert (await repocache.refresh_one(broker, tmp_path, "org/service")).ok is True
    assert run_git.call_args.args[0] == ["-C", str(tmp_path / "org" / "service.git"), "fetch", "--prune"]
    assert run_git.call_args.kwargs["timeout"] == 300


@pytest.mark.asyncio
async def test_clone_always_uses_300s_regardless_of_fetch_timeout(monkeypatch, tmp_path):
    # a short per-task fetch cap shouldn't also squeeze a slow first-time `clone --mirror` (issue #129)
    broker = SimpleNamespace(read_token=AsyncMock(return_value=("secret-token", 9999999999.0)))
    run_git = AsyncMock(return_value="")
    monkeypatch.setattr(repocache, "_run_git", run_git)

    result = await repocache.refresh_one(broker, tmp_path, "org/service", timeout=60)
    assert result == repocache.RefreshResult(ok=True, existed=False)
    assert run_git.call_args.args[0][0] == "clone"
    assert run_git.call_args.kwargs["timeout"] == 300

    (tmp_path / "org" / "service.git").mkdir()
    run_git.reset_mock()
    result = await repocache.refresh_one(broker, tmp_path, "org/service", timeout=60)
    assert result == repocache.RefreshResult(ok=True, existed=True)
    assert run_git.call_args.args[0][:2] == ["-C", str(tmp_path / "org" / "service.git")]
    assert run_git.call_args.kwargs["timeout"] == 60


@pytest.mark.asyncio
async def test_refresh_one_reports_existed_and_git_error_text(monkeypatch, tmp_path):
    broker = SimpleNamespace(read_token=AsyncMock(return_value=("secret-token", 9999999999.0)))
    monkeypatch.setattr(repocache, "_run_git", AsyncMock(side_effect=RuntimeError("git command failed (128): fatal: could not read Username")))

    # no mirror on disk yet: a failed first-time clone leaves nothing behind
    result = await repocache.refresh_one(broker, tmp_path, "org/service")
    assert result.ok is False
    assert result.existed is False
    assert result.error is not None and "fatal: could not read Username" in result.error
    assert not (tmp_path / "org" / "service.git").exists()

    # a mirror already exists: a failed fetch leaves it in place, still usable
    (tmp_path / "org" / "service.git").mkdir(parents=True)
    result = await repocache.refresh_one(broker, tmp_path, "org/service")
    assert result.ok is False
    assert result.existed is True
    assert result.error is not None and "fatal: could not read Username" in result.error
    assert (tmp_path / "org" / "service.git").exists()  # stale mirror survives the failed refresh


def test_mirror_last_fetch_reads_fetch_head_mtime_or_none(tmp_path):
    assert repocache.mirror_last_fetch(tmp_path, "org/service") is None
    mirror = tmp_path / "org" / "service.git"
    mirror.mkdir(parents=True)
    # a mirror with no FETCH_HEAD yet (e.g. never successfully fetched) reports unknown, not the dir's mtime
    assert repocache.mirror_last_fetch(tmp_path, "org/service") is None
    fetch_head = mirror / "FETCH_HEAD"
    fetch_head.write_text("")
    assert repocache.mirror_last_fetch(tmp_path, "org/service") == pytest.approx(fetch_head.stat().st_mtime)


@pytest.mark.asyncio
async def test_refresh_unregisters_each_token_and_basic_value(monkeypatch, tmp_path):
    test_redactor = Redactor()
    tokens = iter(["first-secret-token", "second-secret-token"])

    async def read_token(repositories):
        token = next(tokens)
        test_redactor.register(token)
        return token, 9999999999.0

    broker = SimpleNamespace(read_token=read_token)
    monkeypatch.setattr(repocache, "redactor", test_redactor)
    monkeypatch.setattr(repocache, "_run_git", AsyncMock(return_value=""))

    assert (await repocache.refresh_one(broker, tmp_path, "org/one")).ok is True
    assert (await repocache.refresh_one(broker, tmp_path, "org/two")).ok is True
    assert test_redactor._values == set()


def test_prune_removed_deletes_only_off_list_mirrors(tmp_path):
    keep = tmp_path / "org" / "keep.git"
    remove = tmp_path / "org" / "remove.git"
    keep.mkdir(parents=True)
    remove.mkdir()
    assert repocache.prune_removed(tmp_path, ["org/keep"]) == 1
    assert keep.exists()
    assert not remove.exists()


@pytest.mark.asyncio
async def test_disk_guard_skips_all_fetches(monkeypatch, tmp_path):
    monkeypatch.setattr(repocache.shutil, "disk_usage", lambda path: SimpleNamespace(free=9 * 1024**3))
    run_git = AsyncMock()
    monkeypatch.setattr(repocache, "_run_git", run_git)
    broker = SimpleNamespace(read_token=AsyncMock())
    result = await repocache.refresh_all(object(), broker, tmp_path, ["org/a"])
    assert result["skipped"] is True
    broker.read_token.assert_not_awaited()
    run_git.assert_not_awaited()


@pytest.mark.asyncio
async def test_clone_from_mirror_missing_or_success(monkeypatch, tmp_path):
    assert await repocache.clone_from_mirror(tmp_path, "org/service", tmp_path / "workspace" / "service") is False
    (tmp_path / "org" / "service.git").mkdir(parents=True)
    run_git = AsyncMock(return_value="")
    monkeypatch.setattr(repocache, "_run_git", run_git)
    destination = tmp_path / "workspace" / "service"
    assert await repocache.clone_from_mirror(tmp_path, "org/service", destination) is True
    assert run_git.await_count == 2
    assert run_git.await_args_list[0].args[0] == ["clone", str(tmp_path / "org" / "service.git"), str(destination)]
    assert run_git.await_args_list[1].args[0][-2:] == ["origin", "https://github.com/org/service.git"]
