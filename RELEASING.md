# Releasing TaskBoy to GitHub and PyPI

How a change in this repository becomes a published `taskboy==X.Y.Z` package that operators can install. The short version: **land changes on `develop`, then `git hf release finish` — hubflow merges to `main`, tags `vX.Y.Z`, and pushes; the tag push triggers GitHub Actions**, which builds the dashboard UI, builds the wheel, verifies the packaged data, and publishes to PyPI with [trusted publishing](https://docs.pypi.org/trusted-publishers/) (no API tokens anywhere).

Operators never install from this repo directly. They pin `taskboy==X.Y.Z` in a private deployment repo created from the `taskboy-shell` template, and upgrade with a one-line version bump there.

---

## Branching model (HubFlow)

Both repos follow GitFlow via [HubFlow](https://datasift.github.io/gitflow/):

- `develop` — the integration branch. All feature PRs target it; CI runs the full check suite on every PR (`.github/workflows/pull_request_checks.yaml` triggers on PRs to both `develop` and `main`).
- `main` — release history only. It moves **only** when `git hf release finish` or `git hf hotfix finish` merges and pushes it. Nothing is deployed or published from `develop`.
- `feature/*`, `release/*`, `hotfix/*` — standard hubflow prefixes.

One-time per clone, initialize hubflow and set the version tag prefix so finished releases produce the `vX.Y.Z` tags the release workflow listens for:

```bash
git hf init
# production branch:  main
# integration branch: develop
# prefixes:           accept the defaults (feature/, release/, hotfix/, support/)
# Version tag prefix: v          ← important: makes `git hf release finish 0.1.0` tag v0.1.0
```

(If you skip the prefix, name your releases with the `v` included — `git hf release start v0.1.0` — so the tag still matches `v*.*.*`.)

---

## How versioning works

- The version comes **only from the git tag**. `setuptools-scm` reads the `vX.Y.Z` tag at build time and stamps the wheel (`pyproject.toml` declares `dynamic = ["version"]`; there is no version string to edit anywhere in the code).
- Tags must match `v*.*.*` (e.g. `v0.1.0`) — that pattern is what triggers `.github/workflows/release.yaml`.
- Only a **wheel** is published, never an sdist. This is deliberate: the built dashboard (`taskboy/ui_dist/`) is gitignored, so an sdist built from the git file list would ship without the UI. Don't "fix" this by adding an sdist step.

---

## Part 1 — One-time GitHub setup

### 1.1 Push the application repository

This checkout's remote is `https://github.com/darrenxhays/taskboy.git`. Push both hubflow branches:

```bash
cd taskboy            # this repository
git push -u origin main develop
```

(If the repo doesn't exist yet: `gh repo create darrenxhays/taskboy --public --source . --push`, then push `develop` too.)

> Whatever owner/repo you end up with, **write it down** — the PyPI trusted publisher in Part 2 must name it exactly.

### 1.2 Protect the branches

In the GitHub repo, **Settings → Branches** — the two branches need *different* rules, because hubflow pushes `main` directly (a require-PR rule on `main` would reject every `git hf release finish`):

- **`develop`**: require a pull request before merging; require the `checks` and `ui` status checks (the jobs in `pull_request_checks.yaml` — flake8, black/isort, mypy, full pytest, dashboard build, wheel package-data verification).
- **`main`**: block direct pushes for everyone *except* the release manager — use a ruleset with a bypass list (or admin bypass) rather than a require-PR rule. Required status checks are fine to keep; PR requirement is not.

### 1.3 Push the shell template repository

`taskboy-shell` (sibling directory to this repo) is the deployment template operators start from. Its remote is `https://github.com/darrenxhays/taskboy-shell.git`:

```bash
cd ../taskboy-shell
git push -u origin main develop
```

Then in that repo's GitHub **Settings → General**, check **Template repository**. Operators use "Use this template" (not a fork) to create their private deployment repo, per its `SETUP.md`. Apply the same branch-protection split as 1.2. The shell's deploy workflow fires on pushes to `main` (which hubflow release/hotfix finishes produce) but is gated on the `DEPLOY_ENVIRONMENT` repository variable — instances set it during setup, so the template repo itself never attempts a deploy.

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

### 3.1 Make sure `develop` is green

Everything you want to ship must be merged to `develop` (via `git hf feature` branches + PRs) and passing CI. To double-check locally (same commands CI runs):

```bash
make check          # dockerized lint + type + format + test
# or, without docker, in a 3.12 venv with `pip install -e '.[dev]'`:
flake8 taskboy tests && black --check taskboy tests && isort --check taskboy tests && mypy taskboy && pytest -q
cd ui && npm ci && npm run build     # dashboard must build
```

### 3.2 Choose the version

Semver, without the `v` (the tag prefix adds it). There are no tags yet, so the first release is whatever you want to call it — `0.1.0` is the conventional opener. After that:

- **patch** (`0.1.1`) — fixes, no config surface changes
- **minor** (`0.2.0`) — new features, new config keys with safe defaults
- **major** — breaking config or behavior changes an operator must act on

### 3.3 Run the hubflow release

```bash
cd taskboy
git hf update && git checkout develop && git hf pull
git hf release start 0.1.0
# release/0.1.0 now exists — do release-only polish here if needed (notes, last-minute fixes);
# anything committed on the release branch reaches BOTH main and develop at finish
git hf release finish 0.1.0
```

`finish` merges `release/0.1.0` into `main` **and** back into `develop`, creates the `v0.1.0` tag (via the `v` version-tag prefix from `git hf init`), pushes all of it, and deletes the release branch. The tag push is the deploy button — the `release` workflow now runs:

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

### 3.5 Hotfixes

For an urgent fix that can't wait for the next develop release, branch off `main`:

```bash
git hf hotfix start 0.1.1
# commit the fix on hotfix/0.1.1
git hf hotfix finish 0.1.1     # merges to main + develop, tags v0.1.1, pushes → publishes
```

### 3.6 If a release fails

- **Workflow failed before the publish step** (lint, build, package-data check): fix on `develop` via the normal flow, then cut the *next* patch release. Don't delete and re-push a tag that ran CI — versions are cheap, ambiguity isn't.
- **`invalid-publisher` at the publish step**: the PyPI trusted publisher fields don't match the repo owner/name/workflow filename — fix them on PyPI and re-run the job.
- **Version already exists on PyPI**: PyPI never accepts re-uploads of the same version. Cut the next patch.
- **Tag came out without the `v` prefix** (hubflow init skipped the prefix): the workflow won't trigger. Re-check `git config hubflow.prefix.versiontag`, then push a correctly-prefixed tag on the same commit: `git tag -a v0.1.0 <sha> && git push origin v0.1.0`.

---

## Part 4 — Shipping the release to operators

Operator deployments live in shell repos and pin an exact version. The shell repos run hubflow too — a merge to a shell repo's `main` (i.e. finishing a shell release) is what actually deploys. To roll your own deployment forward:

```bash
cd <your shell repo>
git hf feature start bump-taskboy-0.2.0
sed -i '' 's/^taskboy==.*/taskboy==0.2.0/' requirements.txt
git commit -am "bump taskboy to 0.2.0"
git hf feature finish bump-taskboy-0.2.0     # or push and PR into develop
git hf release start 0.2.0 && git hf release finish 0.2.0   # merge to main = deploy
```

The shell's PR checks validate `config/` against the pinned version before anything merges, and its deploy workflow ships the pinned version and your `config/` + `skills/` to the host on the `main` push (see the shell's `SETUP.md`, section 3c).

**Release-notes discipline:** when a release adds or changes config keys, say so in the GitHub Release notes with the exact YAML the operator should add. For example, `0.4.0` adds `mcp__github__update_pull_request` to the `standard`/`deep` profile allowlists, switches `models.fable` to the bare `fable` alias, needs the `files:read` Slack scope, and requires a `taskboy:account_id` key in each shell Pulumi stack file.

---

## Quick reference

```bash
# one-time
git hf init                                   # production=main, integration=develop, version tag prefix=v
git push -u origin main develop               # both repos on GitHub
# + branch protection (PRs on develop; push-bypass on main), shell template repo,
#   PyPI pending publisher (Parts 1–2)

# day to day
git hf feature start <name> … git hf feature finish <name>   # PRs into develop

# every release
git hf release start X.Y.Z
git hf release finish X.Y.Z                   # ← merges to main+develop, tags vX.Y.Z, pushes = publishes
gh run watch                                  # verify Actions
pip install taskboy==X.Y.Z                    # verify PyPI

# emergency
git hf hotfix start X.Y.Z … git hf hotfix finish X.Y.Z
```
