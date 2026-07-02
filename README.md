# agentsync

An MCP server that lets two (or more) AI agents collaborate on the **same git
repository** without stepping on each other. Each agent declares what it's
building before it builds, sees what its partner has claimed, and detects
conflicts when work lands — all coordinated through the repo itself, with no
lock server and no requirement that both agents be online at once.

## How it works (one paragraph)

Coordination state is a single `claims.json` living on a dedicated `agentsync`
branch (kept out of `main`, so it never pollutes your code history and isn't
blocked by `main`'s branch protection). Each agent's claim declares the work,
the files it will **touch**, what it **requires**, its branch, and a status.
Overlap is plain set intersection. Writes use a read-modify-write loop with
`git push` as a **compare-and-swap**: if the push is rejected, the server
re-fetches the latest claims and re-evaluates, so a colliding peer claim is
*observed before* this agent's claim is committed. All git work happens in a
private worktree under `.git/`, so your agent's actual code branch is never
disturbed.

See **DESIGN.md** for the architecture rationale and **AGENTS.md** for the
playbook your agent follows to drive the tools.

## Install

```bash
pip install -r requirements.txt      # just `mcp`
```

For repo provisioning you also need the [GitHub CLI](https://cli.github.com),
authenticated with `repo` scope:

```bash
gh auth login        # one-time; check with `gh auth status`
```

## Configure

Both collaborators add the server to their MCP client, each with their own
agent id and their own local clone. See `mcp.config.example.json`:

```json
{
  "mcpServers": {
    "agentsync": {
      "command": "python3",
      "args": ["/abs/path/to/agentsync_server.py"],
      "env": {
        "AGENTSYNC_REPO": "/abs/path/to/your/clone",
        "AGENTSYNC_AGENT_ID": "jonny"
      }
    }
  }
}
```

| env var                   | required | default     | meaning                                    |
|---------------------------|----------|-------------|--------------------------------------------|
| `AGENTSYNC_REPO`          | yes      | —           | path to your local clone                   |
| `AGENTSYNC_AGENT_ID`      | yes      | —           | your unique agent id                       |
| `AGENTSYNC_REMOTE`        | no       | `origin`    | git remote name                            |
| `AGENTSYNC_BRANCH`        | no       | `agentsync` | coordination branch name                   |
| `AGENTSYNC_PARTNER_GITHUB`| no       | —           | partner GitHub user(s) to invite (comma/space-separated) |
| `AGENTSYNC_STALE_HOURS`   | no       | `24`        | age after which an in-progress claim is flagged `stale` |

The `agentsync` branch is created automatically on the first `survey()` or
`claim()` call — no manual setup.

## Starting from nothing (no repo yet)

If the shared repo doesn't exist on GitHub yet, **one** person runs `provision()`
once. Point `AGENTSYNC_REPO` at the folder you want the project in (it can be
empty or not yet created) and call:

```
provision(repo="you/our-project", partner_github="their-username")
```

This creates the GitHub repo (private by default), makes the first commit, seeds
the `agentsync` coordination branch, and invites your partner as a push
collaborator. It's idempotent — safe to re-run. Then send your partner the
`clone_url` it returns; once they accept the invite and clone, both of you point
the MCP server at your own clones and the normal protocol below takes over.

## Tools

**`provision(repo="", partner_github="", private=True, description="")`** —
one-time bootstrap when the shared repo doesn't exist yet. Creates the GitHub
repo via `gh`, makes the first commit, seeds the `agentsync` branch, and invites
the partner as a push collaborator. Idempotent. Returns the `clone_url` to hand
your partner. (Needs the `gh` CLI authenticated with `repo` scope.)

**`add_collaborator(github_username, permission="push")`** — invite **one or
more** people (comma/space-separated) to the **existing** shared repo so they can
push (`pull`|`triage`|`push`|`maintain`|`admin`). Use this when the repo already
exists and you just want to grant access — this is how you build a team of more
than two. They must accept the GitHub invite, then clone. (Needs `gh` with admin
on the repo.)

**`survey()`** — pull the latest state and report what every *other* agent has
claimed: task, files, dependencies, branch, status, timestamp. Works for any
number of collaborators. Each partner entry is annotated with `age_hours` and a
`stale` flag (in-progress and older than `AGENTSYNC_STALE_HOURS`, default 24h),
and a top-level `stale_claims` list — so you can spot a partner who crashed or
walked away still holding files. Run it before planning and after finishing.

**`claim(task, touches, requires=None, branch="", force=False)`** — stake a
unit of work. Refuses with `status: "blocked"` if your `touches` hits a
partner's active files (you'd get in their way) or your `requires` hits their
in-progress files (you'd build on unstable ground), returning exactly what
overlaps and with whom. **Overlap is path-aware**: exact match, directory
containment (`src/api` vs `src/api/routes.py`), and globs (`src/**`, `*.py`) all
collide, and paths are normalized first (`./auth.py` == `auth.py`). The overlap
is checked against freshly-fetched state immediately before the push. Pass
`force=True` to claim anyway (e.g. same large file, disjoint regions). If two
people share an agent id, the result carries a `warning`.

**`release(note="")`** — abandon your current claim **without** marking it done,
freeing the files for a partner to take over. Use it when you drop a task or step
away — otherwise a crashed/abandoned claim blocks those files until someone does
manual git surgery. Pushes immediately.

**`check_conflicts(against_branch="")`** — after building, diff your branch
against your partners' branches at two levels:
- `claim_overlap` — declared-path intersection (intent, path-aware).
- `merge_conflict` — a real `git merge-tree` dry-run merge (textual). Catches
  collisions the claims didn't predict.

Defaults to every branch named in an active peer claim; pass `against_branch`
to check one specific branch.

**`update_status(status, note="")`** — set your own claim's status
(`planning` | `in-progress` | `done`) and optionally leave a note for your
partner. Pushes immediately. (To drop a claim without finishing it, use
`release()`.)

## The workflow (what your agent does)

0. `provision(...)` — **once, only if the repo doesn't exist yet** (then both clone)
1. `survey()` — what's my partner working on, if anything?
2. plan a slice that doesn't overlap their active work
3. `claim(...)` — if blocked, narrow the slice or wait
4. build on your branch
5. `survey()` again — where are they now?
6. `check_conflicts()` — does their landed work collide with mine?
7. reconcile (rebase/merge or flag) → `update_status("done", note=...)`

The full prompt your agent should run is in **AGENTS.md**.

## More than two agents

Nothing here is limited to two. `claims.json` is keyed by agent id, `claim()`
checks your plan against *every* peer, and the compare-and-swap only ever edits
your own key — so three, four, or more agents coordinate safely. To run a team:

- Invite everyone: `add_collaborator("alice, bob, carol")` (or list them in
  `provision(partner_github=...)`).
- **Give every person a unique `AGENTSYNC_AGENT_ID`.** Two people sharing an id
  overwrite each other's claim; `claim()` returns a `warning` when it detects
  this, but a unique id per person avoids it entirely.
- Contention stays cheap for a handful of agents; with *many* simultaneous
  claimers a `claim()` can return `retry_exhausted` — just call `survey()` and
  retry.

## Test

```bash
python3 test_agentsync.py     # unit + protocol suite (real git repos)
python3 test_workflow.py      # two-person lifecycle + real MCP stdio transport
```

`test_agentsync.py` (29 cases, isolated per test) covers the protocol (claim/
block on shared files and dependency-on-WIP, force override, done-claims-don't-
block, status validation), **path-aware overlap** (directory containment, globs,
normalization, disjoint-dirs-are-clean), conflict detection (textual conflict and
clean-merge), the **compare-and-swap guarantee** (a peer claim landing mid-flight
both survives our retry and is observed in time to block a collision), liveness
(`release`, `stale` flagging, the duplicate-id warning), error paths, and
provisioning + `add_collaborator` (single and multi-invite, partner-from-env,
existing-remote skip, invite-failure reporting, bad-permission, no-remote) with
the `gh` CLI stubbed so no real GitHub repo is touched. `test_workflow.py` (5
cases) drives the full two-person lifecycle and the real MCP stdio transport as a
subprocess. CI runs both on every push via [GitHub Actions](.github/workflows/test.yml).

## Limitations

- **Textual, not semantic.** `check_conflicts` catches files that won't merge;
  it does *not* catch "their API signature change breaks my caller." That
  reasoning is the agent's job (read both diffs) — the server gives it the
  signal, not the judgment.
- **Both sides must opt in.** This only works fully if your partner's agent
  runs the same server and honors the same claim protocol. Without that, yours
  degrades to branch inspection: it sees what has *landed*, not what's *planned*.
- **Advisory locks.** Claims prevent collisions by convention, not enforcement;
  `force=True` exists precisely because some overlaps are fine.

## License

[PolyForm Noncommercial License 1.0.0](LICENSE.md) — free to use, modify, and
share for any **noncommercial** purpose. Commercial use requires a separate
license.
