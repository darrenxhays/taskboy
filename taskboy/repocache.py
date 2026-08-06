"""best-effort bare mirrors for fast task workspace seeding."""

import asyncio
import base64
import logging
import os
import shutil
from pathlib import Path

from taskboy.redact import redactor

logger = logging.getLogger("taskboy.repocache")

MIN_FREE_GB = 10


def mirror_path(repos_root, repo) -> Path:
    org, name = repo.split("/", 1)
    return Path(repos_root) / org / f"{name}.git"


def disk_ok(repos_root, min_free_gb=MIN_FREE_GB) -> bool:
    root = Path(repos_root)
    root.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(root).free >= min_free_gb * 1024**3


async def refresh_all(store, broker, repos_root, approved_repos) -> dict:
    _ = store  # housekeeping has no synthetic task id to attach an audit event to
    root = Path(repos_root)
    root.mkdir(parents=True, exist_ok=True)
    if not disk_ok(root):
        free_gb = shutil.disk_usage(root).free / 1024**3
        logger.warning("repo cache refresh skipped: %.1f GB free is below the %s GB guard", free_gb, MIN_FREE_GB)
        return {"skipped": True, "free_gb": free_gb}
    result: dict = {"pruned": prune_removed(root, approved_repos)}
    for repo in approved_repos:
        try:
            result[repo] = await refresh_one(broker, root, repo)
        except Exception:
            logger.exception("repo mirror refresh failed for %s", repo)
            result[repo] = False
    return result


async def refresh_one(broker, repos_root, repo, timeout=300) -> bool:
    path = mirror_path(repos_root, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    token, _ = await broker.read_token([repo])
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    redactor.register(basic)
    try:
        env = os.environ.copy()
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
            }
        )
        existed = path.exists()
        try:
            if existed:
                await _run_git(["-C", str(path), "fetch", "--prune"], env=env, timeout=timeout)
            else:
                await _run_git(["clone", "--mirror", f"https://github.com/{repo}.git", str(path)], env=env, timeout=timeout)
            return True
        except Exception:
            logger.exception("repo mirror refresh failed for %s", repo)
            if not existed and path.exists():
                shutil.rmtree(path)
            return False
    finally:
        redactor.unregister(basic)
        redactor.unregister(token)


def prune_removed(repos_root, approved_repos) -> int:
    root = Path(repos_root)
    approved = {mirror_path(root, repo) for repo in approved_repos}
    removed = 0
    for path in root.glob("*/*.git") if root.exists() else []:
        if path not in approved:
            shutil.rmtree(path)
            removed += 1
    return removed


async def clone_from_mirror(repos_root, repo, dest: Path) -> bool:
    source = mirror_path(repos_root, repo)
    if not source.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        await _run_git(["clone", str(source), str(dest)], timeout=60)
        await _run_git(["-C", str(dest), "remote", "set-url", "origin", f"https://github.com/{repo}.git"], timeout=60)
        return True
    except Exception:
        logger.exception("workspace clone from mirror failed for %s", repo)
        if dest.exists():
            shutil.rmtree(dest)
        return False


async def _run_git(args, env=None, timeout=300):
    process = await asyncio.create_subprocess_exec("git", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("git command timed out")
    if process.returncode:
        message = redactor.redact(stderr.decode(errors="replace"))[:500]
        raise RuntimeError(f"git command failed ({process.returncode}): {message}")
    return stdout.decode(errors="replace")
