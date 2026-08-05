"""token-free task-started message selection from agent and reviewer yaml pools."""

import random
from pathlib import Path

import yaml


def validate_content(content: str) -> dict[str, list[str]]:
    try:
        pools = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise ValueError(f"task-started messages are not valid yaml: {e}") from e
    if not isinstance(pools, dict) or set(pools) != {"agent", "reviewer"}:
        raise ValueError("task-started messages must be a yaml mapping with 'agent' and 'reviewer' lists")
    for pool, choices in pools.items():
        if not isinstance(choices, list) or not choices or not all(isinstance(item, str) and item.strip() for item in choices):
            raise ValueError(f"task-started messages for {pool!r} must be a non-empty list of non-empty strings")
    return pools


def pick(path: str | None, pool: str | None) -> str | None:
    if not path or not pool:
        return None
    try:
        pools = yaml.safe_load(Path(path).read_text())
        choices = pools.get(pool) if isinstance(pools, dict) else None
        if not isinstance(choices, list):
            return None
        valid = [item.strip() for item in choices if isinstance(item, str) and item.strip()]
        return random.choice(valid) if valid else None
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
