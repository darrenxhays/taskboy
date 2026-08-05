"""small markdown-to-Slack-mrkdwn conversion for lifecycle posts."""

import re


def to_mrkdwn(text: str) -> str:
    """markdown -> slack mrkdwn. code fences pass through untouched.

    Inline-code spans are intentionally not shielded from bold and link conversion.
    """
    parts = re.split(r"(```.*?```)", text, flags=re.S)
    for index in range(0, len(parts), 2):
        part = parts[index]
        part = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", part, flags=re.MULTILINE)
        part = re.sub(r"\*\*(.+?)\*\*", r"*\1*", part)
        part = re.sub(r"^(\s*)-\s+", r"\1• ", part, flags=re.MULTILINE)
        part = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", part)
        parts[index] = part
    return "".join(parts)
