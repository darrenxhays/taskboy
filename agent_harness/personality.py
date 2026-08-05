"""load the agent's operator-editable personality at the point of use."""

import hashlib
from pathlib import Path


def load(path: str | None) -> tuple[str, str] | None:
    if not path:
        return None
    try:
        content = Path(path).read_text()
    except (OSError, UnicodeError):
        return None
    text = content.strip()
    if not text:
        return None
    return text, hashlib.sha256(content.encode()).hexdigest()
