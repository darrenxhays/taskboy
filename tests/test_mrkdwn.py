import pytest

from agent_harness.mrkdwn import to_mrkdwn


@pytest.mark.parametrize(
    "source,expected",
    [
        ("# Heading", "*Heading*"),
        ("**bold**", "*bold*"),
        ("- one\n  - two", "• one\n  • two"),
        ("[docs](https://example.com/a)", "<https://example.com/a|docs>"),
        ("*already mrkdwn*", "*already mrkdwn*"),
    ],
)
def test_markdown_to_mrkdwn(source, expected):
    assert to_mrkdwn(source) == expected


def test_code_fences_are_byte_identical():
    fence = "```python\n# not a heading\n**not bold**\n- not a bullet\n```"
    source = f"# Outside\n{fence}\n[link](https://example.com)"
    converted = to_mrkdwn(source)
    assert fence in converted
    assert converted == f"*Outside*\n{fence}\n<https://example.com|link>"
