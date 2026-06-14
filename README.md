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
| `AGENTSYNC_PARTNER_GITHUB`| no       | —           | partner's GitHub user, invited by `provision()` |

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

**`survey()`** — pull the latest state and report what every *other* agent has
claimed: task, files, dependencies, branch, status, timestamp. Run it before
planning and again after finishing.

**`claim(task, touches, requires=None, branch="", force=False)`** — stake a
unit of work. Refuses with `status: "blocked"` if your `touches` hits a
partner's active files (you'd get in their way) or your `requires` hits their
in-progress files (you'd build on unstable ground), returning exactly what
overlaps and with whom. The overlap is checked against freshly-fetched state
immediately before the push. Pass `force=True` to claim anyway (e.g. same large
file, disjoint regions).

**`check_conflicts(against_branch="")`** — after building, diff your branch
against your partners' branches at two levels:
- `claim_overlap` — declared-file intersection (intent).
- `merge_conflict` — a real `git merge-tree` dry-run merge (textual). Catches
  collisions the claims didn't predict.

Defaults to every branch named in an active peer claim; pass `against_branch`
to check one specific branch.

**`update_status(status, note="")`** — set your own claim's status
(`planning` | `in-progress` | `done`) and optionally leave a note for your
partner. Pushes immediately.

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

## Test

```bash
python3 test_agentsync.py     # spins up real git repos and drives every path
```

The suite (17 cases, isolated per test) covers the protocol (claim/block on
shared files and dependency-on-WIP, force override, done-claims-don't-block,
status validation), conflict detection (textual conflict and clean-merge),
the **compare-and-swap guarantee** (a peer claim landing mid-flight both
survives our retry and is observed in time to block a collision), error paths,
and provisioning (create + seed + invite, partner-from-env, existing-remote
skip, invite-failure reporting) with the `gh` CLI stubbed so no real GitHub repo
is touched. CI runs it on every push via [GitHub Actions](.github/workflows/test.yml).

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
