# Engineering Conventions

<!--
This file is your organization's ground truth for how code should be written, tested,
and shipped. When a task targets one of your repositories, the harness copies this file
into the agent's workspace as CONVENTIONS.md and instructs the agent to read and follow
it before writing, changing, or reviewing any code. The /review skill judges pull
requests against it.

How to set it up:
  1. Copy this file to config/conventions.md (or any path you like).
  2. Point conventions.file at it in config.yaml (path is relative to config.yaml).
  3. Fill in the sections below. Delete any section that doesn't apply.

Writing tips:
  - Be concrete. "Use pytest, tests live in tests/, mirror the source layout" beats
    "write good tests".
  - State the *why* for rules that would otherwise be argued with.
  - Keep it under ~250 lines — the agent reads this on every repo task.
-->

## Stack & shared libraries

<!-- The languages, frameworks, and internal libraries your services are built on,
     with pinned versions where they matter. Example:
     "Services are Python 3.12 + FastAPI. All services use our shared `acme-lib`
     (pin the latest tag, currently 2.4.1) for auth, logging, and DB sessions." -->

## Code style & values

<!-- The taste rules that make code reviewable in your org. Example:
     "Keep it simple: write logic out longhand rather than clever or DRY.
     Comments are short, lowercase, and explain why — not what the line does." -->

## Project layout

<!-- What a well-formed repo looks like: the directory skeleton, what goes where,
     and when to add (or not add) new folders. -->

## Testing

<!-- Which test tiers exist, which are required, how they run, and what a good test
     looks like. Example: "Every service ships end-to-end tests under
     tests/e2e/ that hit a live deployed instance; unit tests only for pure logic." -->

## Checks & CI

<!-- The commands that must pass before pushing (e.g. `make check`), and what CI
     enforces on pull requests. -->

## Review bar

<!-- What a reviewer (human or agent) should block on vs. let slide.
     Example: "Block on correctness, security, and missing tests. Don't block on
     style that the formatter accepts." -->

## Branching, releases & deployment

<!-- Branch naming, protected branches, how releases are cut, and where things deploy.
     Example: "PRs target develop; a v*.*.* tag on main deploys to production." -->
