---
name: review
description: Review a GitHub pull request for simplicity (KISS + YAGNI) and adversarial scrutiny (correctness, edge cases, security, and tests) against the organization's engineering conventions. Invoked as `@{{agent_name}} /review {pr_url}`.
---

# review

Review a pull request through two lenses: **is it as simple as it can be, and does it hold up under adversarial scrutiny?**

The first GitHub PR URL in the arguments is the PR to review. If it is missing, call `report_blocked` asking for the PR URL, then stop.

Don't pre-check auth or tool availability — just call the tool. If it fails, note it in the Final Report.

Never call `report_progress` during a review. Post the PR comments first, then let the completed response be the only Slack message.

## The lens: keep it simple

- **Did the author do the least?** Could this touch fewer lines, files, or concepts and still satisfy the intent?
- **No premature structure.** New abstractions, files, folders, config, options "for later," layers of indirection — all suspect; flag what the change doesn't need yet. Repetitive if/else beats a slick abstraction.
- **No fluff.** Defensive scaffolding, speculative error handling, dead code, drive-by reformatting — all should go.
- **Longhand over clever/DRY.** Written-out logic is *good*, not a finding — don't push toward cleverness or over-DRYing.
- **Consistent with the organization's engineering conventions.** Deviations from the conventions document are findings.
- **Err hard toward "this could be simpler."**

The organization's engineering conventions are defined in `{{conventions_file}}`. At runtime the same document is injected into the task workspace as `CONVENTIONS.md`; read it before judging convention adherence when present, and continue without it if absent.

### Comments & docstrings — give every one its own pass

Scrutinize every added or changed comment and docstring. The bar: short, lowercase, explain *why* or *what's next* — never what the code obviously does; docstrings only on non-obvious methods, one line.

**Cut** (say "delete this comment"): restates what the next line plainly does; narrates structure the code already shows (section banners, re-listed params, "this function does X" on an obvious method); duplicates a nearby comment, docstring, or name; changelog/"refactored from…"/TODO-for-this-PR notes; commented-out code.

**Keep** (do *not* flag): a short line explaining a non-obvious *why*, a gotcha, a sentinel, or a unit/edge case.

**Simplify, don't just delete.** When a bloated comment carries one real nugget of *why*, say "cut to one line: `<the nugget>`" rather than demanding removal.

## Step 1 — Read the PR

Fetch the PR with `mcp__github__get_pull_request` (title, state, author, head), the changed files with `mcp__github__list_pr_files`, and existing comments and review threads with `mcp__github__list_pr_comments` — one call is enough to see what other human and bot reviewers have already raised. For the full diff and surrounding code, clone the repo with git — auth is handled — and fetch the PR head:

    git clone https://github.com/{owner}/{repo}
    cd {repo} && git fetch origin pull/{number}/head:pr-{number} && git diff {base_branch}...pr-{number}

Read enough of the touched files and their neighbors to judge whether the change fits existing patterns and whether a simpler in-repo approach was available. Skim any linked ticket/thread in the PR body for the actual intent.

## Step 2 — Scrutinize for simplicity and robustness

For each finding, be concrete: **where** (`file:line` in the new code), **what** (the over-engineering / fluff / convention deviation), **simpler** (the smaller way, ideally the specific edit). Rank findings by impact — how much simplicity they'd buy back or how badly the failure would bite. If the PR is already about as simple as it can be, say so plainly — don't manufacture findings.

**Be exhaustive on this pass.** Go file by file through Step 1's changed-files list and check every lens in this step — simplicity, comments and docstrings, and the dimensions below — against each one.

Then actively try to break the change. Scrutinize it for:

- correctness bugs and silent behavior changes;
- unhandled inputs, boundary cases, partial failures, and recovery paths;
- concurrency, ordering, lifecycle, caching, and state-consistency hazards;
- authorization, data exposure, injection, credential, and other security risks;
- missing, weakened, misleading, or insufficiently isolated tests;
- incompatibility with existing APIs, migrations, configuration, or deployed data;
- departures from established repository patterns and the organization's engineering conventions.

Trace important paths end to end. Check assumptions against the code rather than trusting names or comments. For each finding, identify the current file and line, explain the concrete failure mode and impact, and propose the smallest reliable correction. Don't manufacture findings, but never drop a supported finding for politeness.

Create one comment per underlying issue. For a rename or repeated pattern, comment once at the defining or clearest location and say it applies everywhere it is used; never repeat the same finding at each occurrence. If another reviewer already raised the same finding, skip it instead of restating it.

## Step 3 — Reconcile your prior review comments

Before adding anything new, check every comment you left on an earlier pass against the current code. Call `mcp__github__list_pr_comments` with `author: "me"` to pull every prior comment you left, instead of refetching everyone's from Step 1 — keep following the truncation marker's suggested `offset` until it stops appearing, so a long history of your own comments doesn't silently skip reconciliation. Previews are capped at 400 chars and can cut off mid-sentence; when a preview doesn't give you enough to judge, fetch that comment's full body with `comment_id`. Each line's `(resolved)`/`(unresolved)` marker, a thread going "outdated" the moment lines shift, and an author reply of "done" all fail to prove a finding was fixed — check the current code for each:

- **Actually fixed** → resolve the thread with `mcp__github__resolve_pr_thread` (pass the comment id) **without replying**, unless it's already marked `(resolved)`.
- **Not actually fixed, or the fix introduced a new issue** → reply on the thread saying exactly what is still wrong (or what new problem the change created), and re-flag it in this pass's review comments at the current location. There is no unresolve tool, so if the thread is already marked `(resolved)` you cannot and should not try to reopen it — just reply and re-flag. Never resolve a thread you just re-flagged.

The rule: "outdated" ≠ resolved, and "done" ≠ done. A finding only closes when the code proves it.

## Step 4 — Post the review

Post one review with `mcp__github__create_pr_review`: on PRs authored by {{agent_name}}, the only two outcomes are `REQUEST_CHANGES` (any blocking finding) and `APPROVE` (nothing blocking). Cosmetic means comment/docstring wording, naming that isn't a convention deviation, or formatting only; everything else — including simplicity, over-engineering, and any convention deviation — is blocking, and anything ambiguous counts as blocking too. If only cosmetic findings exist (or none), post `APPROVE`; cosmetic findings ride along on the approval as non-blocking notes.

On any other PR, use `REQUEST_CHANGES` when a finding is substantive, otherwise `COMMENT`; never `APPROVE`.

Include a terse summary body and the findings as inline comments via `comments_json` — one entry per finding, anchored to its file and line.

Each inline comment must be one or two plain, high-level sentences: state the problem and the required fix, nothing else. No context narration, multi-paragraph explanations, hedging, softening, or praise padding. If the PR is already simple, post a single short review saying so.

## Step 5 — Final response

In `## Reply`, use 1–3 casual sentences confirming the review is complete, giving only the high-level gist, and linking the PR. Do not give a rundown of findings; this overrides the general 2–6 sentence Reply guidance.

In `## Final Report`, state the number of comments left (or that the PR was already simple), which review outcome was posted, the number of prior threads resolved, the PR link, and any previously-"done" findings that are still open. Use one short line per tool failure. Nothing else.
