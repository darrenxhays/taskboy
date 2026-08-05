"""the management edit engine: resolve targets, validate, diff, and write atomically.

each editable kind maps to one live file (what the service reads) and one repo path (what
gitops commits). validation runs the same loader the service uses, so a save that
passes here cannot brick the service.
"""

import difflib
import hashlib
import os
import stat
import tempfile
from pathlib import Path

import yaml

from agent_harness import personality, settings, skills, started_messages
from agent_harness.config import Config, load_config
from agent_harness.dashboard.render import SECRET_KEY
from agent_harness.redact import redactor

EDITABLE_KINDS = ("config", "personality", "reviewer_personality", "started", "skill", "conventions")


class EditorError(Exception):
    pass


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def target_for(config: Config, kind: str, name: str | None) -> tuple[Path, str, str]:
    """returns (live_path, repo_path, title). repo layout keeps config files under config/ and skills under skills/."""
    if kind == "config":
        return Path(settings.CONFIG_PATH), "config/config.yaml", "System configuration"
    if kind == "personality" and config.personality_path:
        return Path(config.personality_path), f"config/{Path(config.personality_path).name}", "Personality"
    if kind == "reviewer_personality" and config.reviewer.personality_path:
        return Path(config.reviewer.personality_path), f"config/{Path(config.reviewer.personality_path).name}", "Reviewer personality"
    if kind == "started" and config.slack.task_started_messages_path:
        return Path(config.slack.task_started_messages_path), f"config/{Path(config.slack.task_started_messages_path).name}", "Task Started messages"
    if kind == "conventions" and config.conventions_path:
        return Path(config.conventions_path), f"config/{Path(config.conventions_path).name}", "Engineering conventions"
    if kind == "skill" and name and name in skills.available(settings.SKILLS_ROOT):
        return Path(settings.SKILLS_ROOT) / name / "SKILL.md", f"skills/{name}/SKILL.md", f"Skill /{name}"
    raise EditorError("editable target not found")


def contains_secret_submission(kind: str, content: str) -> bool:
    if redactor.redact(content) != content:
        return True
    if kind != "config":
        return False
    try:
        value = yaml.safe_load(content)
    except yaml.YAMLError:
        return False

    def has_secret(item: object, key: str = "") -> bool:
        # only a non-empty string under a secret-named key is a real secret; numeric config like
        # usage_limits.*_tokens matches the "token" name but is a count, not a credential
        if SECRET_KEY.search(key) and isinstance(item, str) and item:
            return True
        if isinstance(item, dict):
            return any(has_secret(child, str(child_key)) for child_key, child in item.items())
        if isinstance(item, list):
            return any(has_secret(child, key) for child in item)
        return False

    return has_secret(value)


def validate(kind: str, name: str | None, content: str, target: Path) -> None:
    if contains_secret_submission(kind, content):
        raise ValueError("secret-looking values are not accepted here; use the configured secrets store")
    if kind == "config":
        handle, temp_name = tempfile.mkstemp(prefix=".dashboard-validate-", suffix=".yaml", dir=target.parent)
        os.close(handle)
        try:
            Path(temp_name).write_text(content)
            load_config(temp_name)
        finally:
            Path(temp_name).unlink(missing_ok=True)
    elif kind in ("personality", "reviewer_personality"):
        handle, temp_name = tempfile.mkstemp(prefix=".dashboard-validate-", suffix=".md", dir=target.parent)
        os.close(handle)
        try:
            Path(temp_name).write_text(content)
            if personality.load(temp_name) is None:
                raise ValueError("personality must contain non-whitespace text")
        finally:
            Path(temp_name).unlink(missing_ok=True)
    elif kind == "started":
        started_messages.validate_content(content)
    elif kind == "conventions":
        if not content.strip():
            raise ValueError("conventions must contain non-whitespace text")
    elif kind == "skill" and name:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / name / "SKILL.md"
            path.parent.mkdir()
            path.write_text(content)
            skills.load(root, name)
    else:
        raise ValueError("unsupported editor")


def unified_diff(previous: str, content: str, title: str) -> str:
    return "".join(difflib.unified_diff(previous.splitlines(keepends=True), content.splitlines(keepends=True), fromfile=f"{title} (current)", tofile=f"{title} (proposed)"))


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o640
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        temp_path.write_text(content)
        temp_path.chmod(existing_mode)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
