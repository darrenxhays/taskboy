"""locations of the assets shipped inside the installed package (wheel or editable checkout).

templates/ holds the seed material the wizard and operators copy out (config example,
personalities, skill templates, slack manifest); deploy/ holds the host runtime files
(systemd units, env example, git credential helper); ui_dist/ is the built dashboard.
"""

import shutil
from importlib.resources import files
from pathlib import Path

PACKAGE_ROOT = Path(str(files("taskboy")))
TEMPLATES_ROOT = PACKAGE_ROOT / "templates"
DEPLOY_ROOT = PACKAGE_ROOT / "deploy"
UI_DIST = PACKAGE_ROOT / "ui_dist"
GIT_CRED_HELPER = DEPLOY_ROOT / "git-cred-helper.py"

EXTRACTABLE = {"templates": TEMPLATES_ROOT, "deploy": DEPLOY_ROOT}


def extract(name: str, destination: str) -> Path:
    """copy a packaged asset tree (templates | deploy) into destination and return the new path."""
    source = EXTRACTABLE[name]
    target = Path(destination) / name
    # pip byte-compiles installed .py files (the git credential helper); don't ship the cache
    shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return target
