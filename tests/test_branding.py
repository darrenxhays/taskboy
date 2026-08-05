"""guard against the old branding leaking back into shipped code, config, templates, or deploy.

tests/ are excluded on purpose: fixtures may use arbitrary configured names (that's the point —
the names come from config). binary and generated files are skipped.
"""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]

SCANNED_DIRS = ["agent_harness", "config", "templates", "skills", "deploy", "infrastructure", ".github", "ui/src", "spike"]
SCANNED_SUFFIXES = {".py", ".yaml", ".yml", ".md", ".sh", ".service", ".path", ".ts", ".tsx", ".html", ".css", ".json", ".example", ""}

FORBIDDEN = [
    re.compile(r"redzone", re.IGNORECASE),
    re.compile(r"agent[-_]red", re.IGNORECASE),
    re.compile(r"\bagentred\b", re.IGNORECASE),
    re.compile(r"x-red-dashboard"),
    re.compile(r"AGENT_RED_"),
    re.compile(r"rz-agent"),
]

# the persona discriminator must be "reviewer", never the old literal
PERSONA_LITERAL = re.compile(r"""persona\s*(?:==|!=|=)\s*["']blue["']""")


def scannable_files():
    for directory in SCANNED_DIRS:
        base = ROOT / directory
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in SCANNED_SUFFIXES and "node_modules" not in path.parts and "dist" not in path.parts and "__pycache__" not in path.parts:
                yield path


def test_no_old_branding_in_shipped_files():
    offenders = []
    for path in scannable_files():
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in FORBIDDEN + [PERSONA_LITERAL]:
            match = pattern.search(text)
            if match:
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line}: {match.group(0)!r}")
    assert not offenders, "old branding found:\n" + "\n".join(offenders)


def test_example_config_has_no_real_tenancy():
    text = (ROOT / "config" / "config.example.yaml").read_text()
    for forbidden in ("redzone", "T2X15FK1C", "713387502796", "360112297542", "838717548546"):
        assert forbidden not in text, f"example config leaks {forbidden!r}"
