---
name: release
description: Cut a GitHub release of the agent's own repository ({{self_repo}}) with a new semver tag sized to the impact of every change since the last tag, and release notes summarizing them. Invoked as `@{{agent_name}} /release`.
---

# release

Publish a GitHub release of the agent's own repo (`{{self_repo}}`): a new `vX.Y.Z` tag plus release notes covering everything merged since the last tag.

## Step 1 — Find the last tag and what changed since it

Clone the repo if it isn't already in your workspace (`git clone https://github.com/{{self_repo}}`; auth is handled), then:

- `git fetch --tags origin`
- `git tag --list --sort=-v:refname | head -1` — the current latest tag, sorted by semver (not creation date or lexicographically). If there are no tags at all, treat `v0.0.0` as the baseline.
- `git log <last_tag>..origin/main --oneline` — every commit since it. If this is empty, there is nothing to release: say so and stop; don't create an empty release.

## Step 2 — Size the version bump

Read the commit summaries (and diffs, if a summary is ambiguous) for everything since the last tag. Pick the **highest** bump triggered by any single change in the batch:

- **x** (`vX.0.0`) — a major or large-scale change.
- **y** (`vX.Y.0`) — a new feature or another larger change.
- **z** (`vX.Y.Z+1`) — bugfixes and small changes only.

A z-bump resets nothing; a y-bump resets z to 0; an x-bump resets y and z to 0. Compute the new tag from the last tag using this rule.

## Step 3 — Write the release notes and publish

Summarize the changes since the last tag in a short, plain-language body — a few bullet points is usually enough, referencing issue or PR numbers where the commit messages have them. Match the terse, factual tone of existing tag messages (`git show <last_tag>`).

Call `mcp__github__create_release` with `repo: {{self_repo}}`, the computed `tag_name`, and the `body` you wrote. Leave `target_commitish` empty so GitHub cuts the tag from the default branch (`main`) rather than whatever branch you happen to have checked out.

## Final Report

The release URL and the tag it cut, e.g. `v0.1.0: https://github.com/{{self_repo}}/releases/tag/v0.1.0`. If there was nothing to release, say that instead. One short line per failure.
