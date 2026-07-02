# agentsync — design

This document records *why* the system is built the way it is, so the decisions
survive past the code.

## Problem

Two people each run their own AI agent against the same GitHub project. One
agent needs to: fetch the project, see what the partner's agent is working on
(if anything), build a requested task **only if** it doesn't depend on or
collide with the partner's in-flight work, then re-check on completion and
either reconcile against the partner's finished work or post its own and update.

That is a multi-agent coordination problem over a shared, mutable artifact.

## Decision 1 — git *is* the coordination bus

The one substrate both agents are guaranteed to reach is the remote (GitHub).
So coordination metadata lives in the repo, not in a separate service. Git
already records what is **done** (commits, branches). The single thing it does
not give you is **intent before action** — what a partner is *about to* touch
before they've committed. So the whole system is one thin layer on top of git:
an intent/claim registry. No broker, no always-on server, works async.

Rejected alternative: direct agent-to-agent messaging (A2A). It's lower-latency
but requires both agents online and network-reachable across two people's
machines (NAT, firewalls, uptime). The user's own description is a polling
model ("check again when you finish"), which is stigmergic, not real-time. A2A
(and an observability tool like skein over it) remains the right choice for the
real-time variant; it is not the right default here.

## Decision 2 — a single shared `claims.json`, not one file per agent

Two layouts were considered:

- **Per-agent files** (`claims/jonny.json`, `claims/partner.json`): no write
  contention, always merges cleanly — but gives **no atomic mutual exclusion**.
  Two agents can each claim the same work concurrently; the collision is only
  noticed afterward and resolved by timestamp yield.
- **Single shared `claims.json`** (chosen): concurrent claim writes *contend*,
  and that contention is exactly what produces a compare-and-swap. The
  read-modify-write retry forces an agent to re-read the freshest state — and
  therefore observe a peer's just-landed claim — before its own write commits.

For two agents, contention is negligible, so the single-file layout buys real
mutual exclusion at no practical cost. That is the better trade.

## Decision 3 — `git push` as compare-and-swap

The claim/update path is optimistic concurrency:

```
loop:
  fetch + hard-reset worktree to remote tip
  read claims
  (for claim) re-check overlap against this fresh state; block if a peer got there first
  write my entry; commit
  push
    success -> done
    rejected (non-fast-forward) -> reset to remote tip; retry
```

A rejected push means a peer pushed between our fetch and our push. We drop our
commit, resync, and re-evaluate. Because each agent only edits its own entry,
the re-applied write never loses the peer's entry — verified by the
"forced rejection" path in the integration test, where a peer's out-of-band
note survives our retry.

## Decision 4 — dedicated branch + private worktree

Claims live on a dedicated `agentsync` branch, not `main`:
- keeps coordination commits out of code history;
- dodges `main`'s branch protection (you can push claims freely).

The server operates that branch through a worktree under `.git/agentsync-wt`,
so the agent's real working tree — checked out to its *code* branch — is never
disturbed. The branch is created off the remote's default branch on first use
(a normal branch, not orphan, for git-version robustness).

## Decision 5 — provisioning is a pre-step, not part of the protocol

The coordination model assumes a shared repo already exists — that *is* the bus.
But "two people start a project together" often begins with **no repo at all**.
That creates a bootstrap gap: you can't coordinate through an artifact that
doesn't exist yet.

`provision()` fills the gap and is deliberately kept *outside* the claim loop:

- It runs **once, by one person**, before anyone surveys or claims. There is
  nothing to coordinate during creation — the repo is the thing being created —
  so the CAS machinery doesn't apply.
- It uses the **`gh` CLI** rather than the GitHub REST API directly: the user
  has almost certainly already run `gh auth login`, so we inherit their auth and
  add no token handling of our own. The cost is a dependency on `gh` being
  installed and on PATH (we detect its absence and say so).
- Every step is **idempotent** — init-if-needed, commit-if-empty,
  create-if-missing, link-if-present, invite-if-given. Re-running it is safe, so
  a half-finished bootstrap (e.g. repo created but invite failed) is fixed by
  just calling it again rather than by manual cleanup.
- It reuses the existing `_ensure_worktree` to seed the `agentsync` branch, so
  the very first `survey()` after provisioning sees a ready coordination branch
  instead of lazily creating one.

What it does **not** do: accept the partner's invite for them, or clone on their
machine. Those are the partner's side. `provision()` returns the `clone_url` and
an explicit "next" instruction so the human hand-off is unambiguous.

## The overlap model

A claim carries:
- `touches` — files/modules this agent will modify;
- `requires` — files/modules this agent depends on.

Against a peer's **active** (non-done) claim:
- `my.touches ∩ peer.touches` → **shared_files** (we'd get in each other's way);
- `my.requires ∩ peer.touches` → **depends_on_their_wip** (we'd build on their
  unstable, unfinished work).

Either makes `claim()` return `blocked` with the specifics, unless `force=True`.
This is the literal encoding of "as long as it doesn't rely on or get in the
way of what they build."

**The intersection is path-aware, not string-equality.** Plain `set(a) & set(b)`
was a silent-wrong trap: a directory claim `src/api` would *not* register against
`src/api/routes.py`, and `./auth.py` read as different from `auth.py`. So `∩` is
implemented by `_paths_overlap`, which (after normalizing slashes / `./` / dup
separators / trailing `/`) matches on three grounds: exact equality, directory
containment (one path is an ancestor of the other), and glob (`src/**`, `*.py`)
in either direction. This makes coarse, natural claims (`"src/api"`, `"**/*.sql"`)
actually protect what they name instead of quietly protecting nothing.

## Decision 6 — liveness: release, staleness, and the shared-id guard

The claim registry is only useful if it reflects *live* intent. Three failure
modes erode that, each with a minimal countermeasure:

- **Abandoned claims.** An agent that crashes or wanders off leaves its files
  locked forever — `done` was the only exit. `release()` adds the missing verb:
  drop a claim without finishing it, freeing the files. It just deletes your own
  key and pushes (same CAS loop as every other write).
- **Silent staleness.** Even un-abandoned, a claim sitting untouched for days is
  a smell. Rather than auto-expire (which would race a slow-but-alive partner),
  `survey()` *surfaces* age: every entry gets `age_hours` + a `stale` flag
  (in-progress and older than `AGENTSYNC_STALE_HOURS`, default 24h), plus a
  top-level `stale_claims` list. The judgment call — nudge them, or `force` past
  it — stays with the agent; the server only makes the staleness visible.
- **Shared agent id.** Two people who both set `AGENTSYNC_AGENT_ID="claude"`
  write to the same key and clobber each other with no error. Each server process
  stamps a random `instance` token on the claims it writes; `claim()` warns (non-
  fatally) when the id it's about to take is already held in-progress by a
  *different* instance. It's advisory, not a hard block, so a legitimate server
  restart doesn't wedge you — it just points at the real fix: unique ids.

## Conflict detection — two levels

1. **Intent / claim overlap** — set intersection of declared files. Version-
   independent, always available, predicts collisions before code lands.
2. **Textual** — `git merge-tree --write-tree` performs a real dry-run merge
   between the two branches and reports conflicted paths without touching either
   working tree. Catches conflicts the declarations didn't predict.

What is deliberately *not* automated: **semantic** conflict ("their signature
change breaks my caller"). That requires reading both diffs and reasoning about
them — the agent's job. The server surfaces the signal; the agent supplies the
judgment. This is the natural seam for a decision-memory tool (e.g.
context-keeper): when the agent reviews a partner's finished work, the diff says
*what* changed and the decision records say *why*, which is what makes the
"do I need to update?" call intelligent rather than purely mechanical.

## Concurrency / failure notes

- **Claim race** → resolved by the CAS above; the loser re-evaluates and blocks.
- **Stale worktree** → every tool call fetches and hard-resets to the remote tip
  before reading, so a tool call never acts on stale claims.
- **Partner not cooperating** → degrades to branch inspection only (what landed,
  not what's planned). Functional unilaterally, blind to in-flight intent.
- **Advisory, not enforced** → `force=True` is intentional; some overlaps (same
  large file, disjoint regions) are fine and shouldn't be hard-blocked.

## Possible extensions (not built)

- Region-level claims (line ranges) instead of whole-file `touches`.
- An A2A transport for the real-time variant, observable via skein.
- Auto-rebase on `check_conflicts` when the merge is clean.
