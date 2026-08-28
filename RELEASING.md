# Releasing TaskBoy to GitHub and PyPI

How a change in this repository becomes a published `taskboy==X.Y.Z` package that operators can install. The short version: **merge to `main`, push a `vX.Y.Z` tag, and GitHub Actions does the rest** — it builds the dashboard UI, builds the wheel, verifies the packaged data, and publishes to PyPI with [trusted publishing](https://docs.pypi.org/trusted-publishers/) (no API tokens anywhere).

Operators never install from this repo directly. They pin `taskboy==X.Y.Z` in a private deployment repo created from the `taskboy-shell` template, and upgrade with a one-line version bump there.

---

## How versioning works

- The version comes **only from the git tag**. `setuptools-scm` reads the `vX.Y.Z` tag at build time and stamps the wheel (`pyproject.toml` declares `dynamic = ["version"]`; there is no version string to edit anywhere in the code).
- Tags must match `v*.*.*` (e.g. `v0.1.0`) — that pattern is what triggers `.github/workflows/release.yaml`.
- Only a **wheel** is published, never an sdist. This is deliberate: the built dashboard (`taskboy/ui_dist/`) is gitignored, so an sdist built from the git file list would ship without the UI. Don't "fix" this by adding an sdist step.

---

## Part 1 — One-time GitHub setup

### 1.1 Push the application repository

This checkout already has a remote: `https://github.com/darrenxhays/taskboy.git`. If that's the repo you want to publish from, just push:

```bash
cd taskboy            # this repository
git push origin main
```

If you'd rather publish under a different name/org, create the repo first and repoint the remote:

```bash
gh repo create <owner>/taskboy --public --source . --push
# or manually:
git remote set-url origin https://github.com/<owner>/<repo>.git
git push -u origin main
```

> Whatever owner/repo you end up with, **write it down** — the PyPI trusted publisher in Part 2 must name it exactly.

### 1.2 Protect `main`

In the GitHub repo: **Settings → Branches → Add branch ruleset** (or classic protection rule) for `main`:

- Require a pull request before merging.
- Require status checks to pass: `checks` and `ui` (the two jobs in `.github/workflows/pull_request_checks.yaml` — they run flake8, black/isort, mypy, the full pytest suite, the dashboard build, and a wheel package-data verification).

### 1.3 Push the shell template repository

`taskboy-shell` (sibling directory to this repo) is the deployment template operators start from. It has no remote yet:

```bash
cd ../taskboy-shell
gh repo create <owner>/taskboy-shell --public --source . --push
# or create it in the UI, then:
git remote add origin https://github.com/<owner>/taskboy-shell.git
git push -u origin main
```

Then in that repo's GitHub **Settings → General**, check **Template repository**. Operators use "Use this template" (not a fork) to create their private deployment repo, per its `SETUP.md`.

---

## Part 2 — One-time PyPI setup (trusted publishing)

The release workflow authenticates to PyPI with GitHub's OIDC identity (`permissions: id-token: write` + `pypa/gh-action-pypi-publish`). No token or secret is ever created. PyPI just needs to be told, once, which workflow is allowed to publish `taskboy`.

The name `taskboy` is currently **unclaimed on PyPI**, so use the *pending publisher* flow, which reserves the name and authorizes the workflow in one step:

1. Create an account at <https://pypi.org> and enable 2FA (required for publishing).
2. Go to **your account → Publishing** (<https://pypi.org/manage/account/publishing/>) → **Add a new pending publisher** → GitHub tab, and enter exactly:

   | Field | Value |
   |---|---|
   | PyPI project name | `taskboy` |
   | Owner | `darrenxhays` *(or the owner you chose in 1.1)* |
   | Repository name | `taskboy` *(or the repo you chose in 1.1)* |
   | Workflow name | `release.yaml` |
   | Environment name | *(leave blank — the workflow doesn't use one)* |

3. That's it. The **first successful run** of the release workflow creates the `taskboy` project on PyPI and converts the pending publisher into a normal trusted publisher automatically.

If you ever rename the GitHub repo or move it to an org, update the trusted publisher on PyPI (**project → Settings → Publishing**) to match, or publishing will fail with `invalid-publisher`.

> **Optional hardening (recommended once things work):** create a GitHub Actions environment named `pypi`, add `environment: pypi` to the `publish` job in `release.yaml`, and set the same environment name in the PyPI trusted publisher. This lets you add required reviewers in front of every publish.

---

## Part 3 — Cutting a release

### 3.1 Make sure `main` is green

Everything you want to ship must be merged to `main` and passing CI. To double-check locally (same commands CI runs):

```bash
make check          # dockerized lint + type + format + test
# or, without docker, in a 3.12 venv with `pip install -e '.[dev]'`:
flake8 taskboy tests && black --check taskboy tests && isort --check taskboy tests && mypy taskboy && pytest -q
cd ui && npm ci && npm run build     # dashboard must build
```

### 3.2 Choose the version

Semver, prefixed with `v`. There are no tags yet, so the first release is whatever you want to call it — `v0.1.0` is the conventional opener. After that:

- **patch** (`v0.1.1`) — fixes, no config surface changes
- **minor** (`v0.2.0`) — new features, new config keys with safe defaults
- **major** — breaking config or behavior changes an operator must act on

### 3.3 Tag and push

```bash
cd taskboy
git checkout main && git pull
git tag -a v0.1.0 -m "v0.1.0: first public release"
git push origin v0.1.0
```

Pushing the tag is the deploy button. The `release` workflow now runs:

1. checkout with full history (setuptools-scm needs the tag),
2. `npm ci && npm run build` → dashboard into `taskboy/ui_dist/`,
3. `python -m build --wheel`,
4. **package-data verification** — asserts the wheel contains the config/service templates, skill templates, deploy files, and the built dashboard; a miss fails the release before anything is published,
5. `pypa/gh-action-pypi-publish` → PyPI via OIDC.

### 3.4 Verify

- Watch the run: repo → **Actions → release** (or `gh run watch`).
- Check <https://pypi.org/project/taskboy/> shows the new version.
- Smoke-test the artifact exactly like an operator would:

```bash
python3.12 -m venv /tmp/tb && source /tmp/tb/bin/activate
pip install taskboy==0.1.0
mkdir /tmp/tb-inst && cd /tmp/tb-inst
taskboy setup --local && taskboy run   # then, in another shell: taskboy inject "say hi" --watch
```

- Optionally create a GitHub Release from the tag with human-readable notes: `gh release create v0.1.0 --generate-notes` (PyPI publishing does not depend on this).

### 3.5 If a release fails

- **Workflow failed before the publish step** (lint, build, package-data check): fix on `main` via PR, then tag the *next* patch version. Don't delete and re-push a tag that ran CI — versions are cheap, ambiguity isn't.
- **`invalid-publisher` at the publish step**: the PyPI trusted publisher fields don't match the repo owner/name/workflow filename — fix them on PyPI and re-run the job.
- **Version already exists on PyPI**: PyPI never accepts re-uploads of the same version. Tag the next patch.

---

## Part 4 — Shipping the release to operators

Operator deployments live in shell repos and pin an exact version. To roll your own deployment forward:

```bash
cd <your shell repo>
# requirements.txt: taskboy==0.1.0  →  taskboy==0.2.0
git checkout -b bump-taskboy-0.2.0
sed -i '' 's/^taskboy==.*/taskboy==0.2.0/' requirements.txt
git commit -am "bump taskboy to 0.2.0" && git push
```

Open a PR; on merge to `main`, the shell repo's deploy workflow ships the pinned version and your `config/` + `skills/` to the host (see the shell's `SETUP.md`, section 3c).

**Release-notes discipline:** when a release adds or changes config keys, say so in the GitHub Release notes with the exact YAML the operator should add. For example, the current unreleased head requires dashboard operators to set `dashboard.expected_alb_arn` (the app now pins the ALB OIDC header's signer and fails closed), and newly supports an optional `help.file` and `retention.errors_days` / `retention.blocked_task_*` keys.

---

## Quick reference

```bash
# one-time
git push origin main                          # app repo on GitHub
# + branch protection, shell template repo, PyPI pending publisher (Parts 1–2)

# every release
git checkout main && git pull                 # green main
git tag -a vX.Y.Z -m "vX.Y.Z: summary"
git push origin vX.Y.Z                        # ← this publishes
gh run watch                                  # verify Actions
pip install taskboy==X.Y.Z                    # verify PyPI
```
