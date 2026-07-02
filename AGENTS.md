# AGENTS.md — collaboration playbook

The server gives your agent four primitives (`survey`, `claim`,
`check_conflicts`, `update_status`). This file is the **orchestration** — the
sequence that turns those primitives into the collaboration behavior. Paste the
block below into your agent's system prompt / project instructions (alongside
whatever git tooling it already has for clone/branch/commit/push).

---

## Paste-ready instruction block

> You are collaborating on a shared git repository with another developer's
> agent. You coordinate through the `agentsync` MCP server. Never assume what
> the other agent is doing — always query it. Follow this protocol for every
> task:
>
> **0. Bootstrap the repo (only if it doesn't exist yet).**
> If the shared GitHub repo hasn't been created, exactly one of you runs
> `provision(repo="owner/name", partner_github="<partner>")` once. It creates
> the repo, seeds the `agentsync` branch, and invites your partner. Send them
> the returned `clone_url`; they accept the invite and clone. Skip this step
> entirely if the repo already exists — it's a one-time setup, not per-task.
>
> **1. Get the project & look around.**
> Make sure you have the latest of the shared repo (fetch/pull). Call
> `survey()`. Read every entry under `partners`: their `task`, `touches`,
> `requires`, `branch`, `status`.
>
> **2. Plan a non-conflicting slice.**
> Choose how to implement the requested task so that the files you'll modify do
> **not** overlap any partner's active `touches`, and so that you do **not**
> depend on files a partner is mid-change on (their active `touches`). If the
> only sensible implementation overlaps their in-flight work, prefer to wait or
> build an independent slice first.
>
> **3. Claim before you build.**
> Call `claim(task, touches=[...], requires=[...], branch="<your-branch>")`.
> - If it returns `claimed`, proceed.
> - If it returns `blocked`, read `conflicts`. Either narrow `touches` to a
>   non-overlapping slice and re-claim, or hold and re-`survey()` later. Only
>   use `force=True` if you're certain the overlap is benign (e.g. the same
>   large file but clearly disjoint regions) and say so in the task.
>
> **4. Build.**
> Do the work on your claimed branch. Commit and push your branch (your normal
> git tools, not the agentsync server).
>
> **5. Re-check the partner.**
> Call `survey()` again. The partner is now in one of two states:
>
> **5a. Partner is `done`.**
> Call `check_conflicts()`. For each result:
> - `merge_conflict.conflict == false` and empty `claim_overlap`: you're clear.
> - Otherwise read both diffs (your branch vs their branch). Resolve textual
>   conflicts by rebasing/merging. Critically, also check for **semantic**
>   breakage the merge won't show — e.g. they changed a function signature,
>   constant, schema, or interface you consume. If your code needs to adapt,
>   update it. The merge passing is necessary, not sufficient.
> Then `update_status("done", note="<what you changed / reconciled>")`, or
> `finish(note=...)` to mark done AND open a PR in one step. Either way the
> claim is auto-annotated with `changed_files` (your branch's diffstat) so the
> partner reconciles against real data, not just your note. To see what a
> partner has been doing over time, `history()`.
>
> **5b. Partner is still `in-progress`.**
> Don't block on them. Push your branch, then `update_status("done", note=...)`
> (or `finish(...)` to open a PR). The note is how they'll reconcile against
> your work when they finish; `changed_files` is captured automatically.
>
> **Rules.**
> - Query, never assume: a fresh `survey()` beats memory of an earlier one.
> - One unit of work = one claim. Re-claim for the next unit.
> - A blocked claim is information, not failure — it tells you exactly what to
>   route around.
> - If you abandon a task instead of finishing it, call `release()` so you don't
>   leave the files locked behind you.
> - Watch `survey()`'s `stale_claims`: if a partner has sat on files well past
>   `AGENTSYNC_STALE_HOURS`, they may have crashed — check in before you assume
>   the lock is live.
> - Every collaborator needs a **unique** `AGENTSYNC_AGENT_ID`. If `claim()`
>   returns a `warning` about a shared id, stop and fix the ids before continuing.
> - You only see a partner's *intent* if their agent also runs agentsync. If a
>   partner has no claim but you see active branches, treat those branches as
>   landed work and rely on `check_conflicts` at merge time.

---

## Worked example (matches the original ask)

Task: "build the reporting endpoint."

1. `survey()` → partner claim: `task: "auth", touches: ["auth.py","models/user.py"], status: "in-progress"`.
2. Reporting needs a new `reports.py` and reads `models/user.py`. `models/user.py`
   is in their active `touches` → depending on it would be building on WIP.
   Plan a slice that defines its own read interface and doesn't import their
   in-flight model yet.
3. `claim("reporting endpoint", touches=["reports.py","api/routes.py"], branch="me/reporting")`
   → `claimed`.
4. Build on `me/reporting`, push.
5. `survey()` → partner now `done`.
   `check_conflicts()` → no textual conflict, but their diff renamed
   `User.uid` → `User.id`. Reporting reads that field → update `reports.py`.
   `update_status("done", note="reporting reads User.id (was uid after auth merge)")`.
