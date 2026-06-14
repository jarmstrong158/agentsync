#!/usr/bin/env python3
"""
agentsync — an MCP server for coordinating two (or more) agents working the
same git repository.

Model
-----
Coordination state lives in a single ``claims.json`` on a dedicated
``agentsync`` branch (separate from your code branches, so it never touches
main and is not blocked by main's branch protection). Each agent declares:

    task      what it's building
    touches   files/modules it will modify   -> the "get in the way" check
    requires  what it depends on             -> the "rely on their build" check
    branch    where its work lives
    status    planning | in-progress | done

Overlap is set intersection. Writes use a read-modify-write loop with
``git push`` as the compare-and-swap: on a rejected push the server re-fetches
the latest claims and re-evaluates, so a colliding peer claim is *observed*
before this agent's claim is committed. That is the mutual-exclusion guarantee.

All git operations run in a private worktree under ``.git/`` so the agent's
real working tree (its code branch) is never disturbed.

If the shared repo does not exist yet, call ``provision()`` once to create it
on GitHub (via the ``gh`` CLI), seed the coordination branch, and invite the
partner as a collaborator. After that, both people clone it and the
survey/claim protocol takes over.

Config (environment, set in the MCP client config)
--------------------------------------------------
    AGENTSYNC_REPO      absolute path to the local clone        (required)
    AGENTSYNC_AGENT_ID  this agent's id, e.g. "jonny"           (required)
    AGENTSYNC_REMOTE    remote name                             (default: origin)
    AGENTSYNC_BRANCH    coordination branch                     (default: agentsync)
    AGENTSYNC_PARTNER_GITHUB  partner's GitHub username, invited
                              as a collaborator by provision()   (optional)
"""

import json
import os
import subprocess
import time
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("agentsync")

CLAIMS_FILE = "claims.json"
PUSH_RETRIES = 5

# Any single git/gh invocation is bounded so a stuck network call or an
# un-answerable credential prompt fails fast instead of hanging the MCP server.
GIT_TIMEOUT = int(os.environ.get("AGENTSYNC_GIT_TIMEOUT", "25"))


def _noninteractive_env():
    """git env that refuses to block on a credential/login prompt. In an MCP
    subprocess there is no terminal or GUI to answer one, so a prompt would hang
    forever; these force git to error out immediately instead."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"     # never prompt on the terminal
    env["GCM_INTERACTIVE"] = "Never"     # Git Credential Manager: no popup dialog
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _log(msg):
    """Append a timestamped line to <repo>/.git/agentsync.log. Best-effort: a
    logging failure must never break a tool call. This is the breadcrumb trail
    that turns 'it hangs' into 'it hung on exactly this git command'."""
    try:
        repo = os.environ.get("AGENTSYNC_REPO")
        if not repo:
            return
        line = f"{datetime.now(timezone.utc).isoformat()} {msg}\n"
        with open(os.path.join(repo, ".git", "agentsync.log"), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
class ConfigError(RuntimeError):
    pass


def _cfg(require_git=True):
    repo = os.environ.get("AGENTSYNC_REPO")
    agent = os.environ.get("AGENTSYNC_AGENT_ID")
    if not repo or not agent:
        raise ConfigError(
            "AGENTSYNC_REPO and AGENTSYNC_AGENT_ID must be set in the MCP config."
        )
    repo = os.path.abspath(repo)
    if require_git and not os.path.isdir(os.path.join(repo, ".git")):
        raise ConfigError(
            f"{repo} is not a git repository (no .git directory). "
            "If the shared repo doesn't exist yet, call provision() first."
        )
    return {
        "repo": repo,
        "agent": agent,
        "remote": os.environ.get("AGENTSYNC_REMOTE", "origin"),
        "branch": os.environ.get("AGENTSYNC_BRANCH", "agentsync"),
        "partner_github": os.environ.get("AGENTSYNC_PARTNER_GITHUB", ""),
        "worktree": os.path.join(repo, ".git", "agentsync-wt"),
    }


# --------------------------------------------------------------------------- #
# git helpers
# --------------------------------------------------------------------------- #
def _git(args, cwd, check=True):
    t0 = time.time()
    try:
        p = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            env=_noninteractive_env(), timeout=GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,   # CRITICAL: never inherit the MCP stdio pipe.
            # Without this, git inherits the server's stdin (the JSON-RPC transport)
            # and any credential prompt blocks forever reading from it.
        )
    except subprocess.TimeoutExpired:
        _log(f"TIMEOUT after {GIT_TIMEOUT}s: git {' '.join(args)} (cwd={cwd})")
        raise RuntimeError(
            f"git {' '.join(args)} timed out after {GIT_TIMEOUT}s — likely a "
            "stuck network call or a credential prompt git couldn't answer. "
            "Verify `git fetch` and `git push` work from this clone "
            "(e.g. `gh auth setup-git`)."
        )
    _log(f"{time.time() - t0:5.2f}s rc={p.returncode}: git {' '.join(args)}")
    if check and p.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({p.returncode}): {p.stderr.strip()}"
        )
    return p


def _gh(args, cwd=None, check=True):
    """Run the GitHub CLI. Raises a friendly error if gh is missing or the
    command fails."""
    try:
        p = subprocess.run(
            ["gh", *args], cwd=cwd, capture_output=True, text=True,
            timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL,   # never inherit the MCP stdio pipe
        )
    except FileNotFoundError:
        raise RuntimeError(
            "The GitHub CLI ('gh') is not installed or not on PATH. Install it "
            "from https://cli.github.com and run `gh auth login`, then retry."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"gh {' '.join(args)} timed out after {GIT_TIMEOUT}s."
        )
    if check and p.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} failed ({p.returncode}): {p.stderr.strip()}"
        )
    return p


def _remote_has_branch(cfg):
    p = _git(["ls-remote", "--heads", cfg["remote"], cfg["branch"]], cfg["repo"])
    return bool(p.stdout.strip())


def _default_remote_head(cfg):
    # origin/HEAD -> origin/main (or whatever the default is)
    p = _git(
        ["symbolic-ref", "--short", f"refs/remotes/{cfg['remote']}/HEAD"],
        cfg["repo"],
        check=False,
    )
    if p.returncode == 0 and p.stdout.strip():
        return p.stdout.strip()
    return f"{cfg['remote']}/main"


def _ensure_worktree(cfg):
    """Guarantee a worktree at cfg['worktree'] checked out to the coordination
    branch, synced to the remote tip. Creates the branch on first use."""
    _git(["fetch", cfg["remote"], "--prune"], cfg["repo"], check=False)
    wt, branch, remote = cfg["worktree"], cfg["branch"], cfg["remote"]

    if not os.path.isdir(wt):
        if _remote_has_branch(cfg):
            _git(
                ["worktree", "add", "-B", branch, wt, f"{remote}/{branch}"],
                cfg["repo"],
            )
        else:
            # create the coordination branch off the default branch
            base = _default_remote_head(cfg)
            _git(["worktree", "add", "-b", branch, wt, base], cfg["repo"])
            path = os.path.join(wt, CLAIMS_FILE)
            with open(path, "w") as f:
                json.dump({"claims": {}}, f, indent=2)
            _git(["add", CLAIMS_FILE], wt)
            _git(["commit", "-m", "agentsync: initialize claims"], wt)
            _git(["push", "-u", remote, branch], wt)
        return

    # worktree exists -> hard-sync to remote tip if the branch is published
    if _remote_has_branch(cfg):
        _git(["fetch", remote, branch], wt, check=False)
        _git(["reset", "--hard", f"{remote}/{branch}"], wt, check=False)


def _read_claims(cfg):
    path = os.path.join(cfg["worktree"], CLAIMS_FILE)
    if not os.path.exists(path):
        return {"claims": {}}
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {"claims": {}}
    data.setdefault("claims", {})
    return data


def _now():
    return datetime.now(timezone.utc).isoformat()


def _commit_and_push(cfg, message):
    """Commit claims.json and push. Returns True on success, False if the push
    was rejected (someone else pushed first -> caller should retry)."""
    wt, remote, branch = cfg["worktree"], cfg["remote"], cfg["branch"]
    _git(["add", CLAIMS_FILE], wt)
    st = _git(["status", "--porcelain"], wt)
    if not st.stdout.strip():
        return True  # nothing changed; treat as success
    _git(["commit", "-m", message], wt)
    push = _git(["push", remote, branch], wt, check=False)
    if push.returncode == 0:
        return True
    # non-fast-forward / rejected: drop our commit, resync, signal retry
    _git(["reset", "--hard", f"{remote}/{branch}"], wt, check=False)
    return False


# --------------------------------------------------------------------------- #
# overlap logic
# --------------------------------------------------------------------------- #
def _overlap(my_touches, my_requires, peer):
    """Return reasons this agent's plan conflicts with a peer's active claim."""
    if peer.get("status") == "done":
        return []
    pt = set(peer.get("touches", []))
    reasons = []
    both_touch = set(my_touches) & pt
    if both_touch:
        reasons.append({"type": "shared_files", "files": sorted(both_touch)})
    dep_on_wip = set(my_requires) & pt
    if dep_on_wip:
        reasons.append(
            {"type": "depends_on_their_wip", "files": sorted(dep_on_wip)}
        )
    return reasons


# --------------------------------------------------------------------------- #
# provisioning (gh CLI)
# --------------------------------------------------------------------------- #
def _gh_login():
    p = _gh(["api", "user", "--jq", ".login"])
    return p.stdout.strip()


def _resolve_slug(repo_arg, repo_path):
    """Turn the repo argument into an 'owner/name' slug. Accepts 'owner/name',
    a bare 'name' (owner = current gh user), or '' (name from the repo path)."""
    if repo_arg and "/" in repo_arg:
        return repo_arg
    name = repo_arg or os.path.basename(repo_path.rstrip("/\\"))
    return f"{_gh_login()}/{name}"


def _gh_repo_exists(slug):
    return _gh(["repo", "view", slug, "--json", "name"], check=False).returncode == 0


def _ensure_git_identity(repo):
    """git can't commit without a configured author. Borrow gh's identity if the
    repo (and global config) have none."""
    have = _git(["config", "user.email"], repo, check=False).stdout.strip()
    if have:
        return
    login = _gh_login()
    _git(["config", "user.name", login], repo)
    _git(["config", "user.email", f"{login}@users.noreply.github.com"], repo)


VALID_PERMISSIONS = ("pull", "triage", "push", "maintain", "admin")


def _invite_collaborator(slug, user, permission="push"):
    """Invite `user` to `slug` at the given permission. Returns (ok, message)."""
    r = _gh(
        ["api", "-X", "PUT", f"repos/{slug}/collaborators/{user}",
         "-f", f"permission={permission}"],
        check=False,
    )
    if r.returncode == 0:
        return True, f"invited {user} ({permission})"
    return False, f"could not invite {user}: {r.stderr.strip()[:200]}"


def _repo_slug(repo_path):
    """Resolve the 'owner/name' of the GitHub repo behind this clone's origin."""
    p = _gh(
        ["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        cwd=repo_path, check=False,
    )
    return p.stdout.strip()


@mcp.tool()
def add_collaborator(github_username: str, permission: str = "push") -> str:
    """Invite someone as a collaborator on the shared repo so they can push to
    it. Use this when the repo already exists and you just want to grant a
    partner access (provision() does this too, but only as part of first-time
    setup).

    github_username : the GitHub user to invite (e.g. "jarmstrong158").
    permission      : pull | triage | push | maintain | admin  (default push).

    The invited user must accept the GitHub invitation before they can push.
    Requires the `gh` CLI authenticated with admin on the repo. Returns the
    clone URL to hand the new collaborator."""
    if permission not in VALID_PERMISSIONS:
        return json.dumps(
            {"error": f"permission must be one of {', '.join(VALID_PERMISSIONS)}"}
        )
    cfg = _cfg()
    slug = _repo_slug(cfg["repo"])
    if not slug:
        return json.dumps(
            {"error": "Could not determine the GitHub repo from this clone's "
             "'origin' remote. Is it a GitHub repo with a remote set?"}
        )
    ok, msg = _invite_collaborator(slug, github_username, permission)
    return json.dumps(
        {
            "status": "invited" if ok else "failed",
            "repo": slug,
            "collaborator": github_username,
            "permission": permission,
            "message": msg,
            "clone_url": f"https://github.com/{slug}.git",
            "next": (
                f"Tell {github_username} to accept the invite (GitHub email/"
                f"notification), then `git clone https://github.com/{slug}.git` "
                "and point their agentsync server at that clone."
            ) if ok else None,
        },
        indent=2,
    )


@mcp.tool()
def provision(
    repo: str = "",
    partner_github: str = "",
    private: bool = True,
    description: str = "",
) -> str:
    """Create the shared GitHub repository if it doesn't exist yet, then leave
    both collaborators ready to use the claim protocol. Run this ONCE, by one
    person, before anyone calls survey()/claim(). It is idempotent — safe to
    re-run; each step is skipped if already done.

    What it does, in order:
      1. Ensure AGENTSYNC_REPO is a local git repo with at least one commit
         (creates the directory + a starter README if empty).
      2. Create the repo on GitHub via `gh` (private unless private=False) and
         wire up the 'origin' remote, or reuse an existing remote/repo.
      3. Push the default branch.
      4. Seed the coordination branch (agentsync) with an empty claims.json.
      5. Invite the partner as a push collaborator, if a username is given here
         or via AGENTSYNC_PARTNER_GITHUB.

    repo            : 'owner/name', bare 'name' (owner = you), or '' to use the
                      AGENTSYNC_REPO folder name.
    partner_github  : partner's GitHub username to invite (overrides env).
    private         : create the repo private (default) or public.
    description     : optional GitHub repo description.

    Requires the `gh` CLI, authenticated (`gh auth login`) with 'repo' scope.
    Returns a summary of what was created plus the clone URL to send your
    partner."""
    cfg = _cfg(require_git=False)
    repo_path, remote = cfg["repo"], cfg["remote"]
    partner = partner_github or cfg["partner_github"]
    steps = []

    # 1. local repo + a commit so a default branch exists
    os.makedirs(repo_path, exist_ok=True)
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        _git(["init", "-b", "main"], repo_path)
        steps.append("git init")
    _ensure_git_identity(repo_path)
    has_commit = _git(["rev-parse", "HEAD"], repo_path, check=False).returncode == 0
    if not has_commit:
        readme = os.path.join(repo_path, "README.md")
        if not os.listdir(repo_path) or not os.path.exists(readme):
            if not os.path.exists(readme):
                with open(readme, "w") as f:
                    f.write(f"# {os.path.basename(repo_path)}\n")
        _git(["add", "-A"], repo_path)
        _git(["commit", "-m", "Initial commit"], repo_path)
        steps.append("initial commit")
    default_branch = _git(
        ["symbolic-ref", "--short", "HEAD"], repo_path
    ).stdout.strip() or "main"

    # 2. GitHub remote
    slug = _resolve_slug(repo, repo_path)
    have_remote = (
        _git(["remote", "get-url", remote], repo_path, check=False).returncode == 0
    )
    if not have_remote:
        if _gh_repo_exists(slug):
            url = f"https://github.com/{slug}.git"
            _git(["remote", "add", remote, url], repo_path)
            steps.append(f"linked existing remote {slug}")
        else:
            args = [
                "repo", "create", slug,
                "--private" if private else "--public",
                "--source", repo_path, "--remote", remote, "--push",
            ]
            if description:
                args += ["--description", description]
            _gh(args, cwd=repo_path)
            steps.append(f"created GitHub repo {slug} ({'private' if private else 'public'})")
    else:
        steps.append("remote already configured")

    # 3. push default branch (create=False path above already pushed on create)
    _git(["push", "-u", remote, default_branch], repo_path, check=False)

    # 4. seed the coordination branch
    seed_cfg = _cfg(require_git=True)
    _ensure_worktree(seed_cfg)
    steps.append(f"seeded coordination branch '{seed_cfg['branch']}'")

    # 5. invite the partner
    invited_ok = False
    if partner:
        invited_ok, invite = _invite_collaborator(slug, partner)
        steps.append(invite)

    clone_url = f"https://github.com/{slug}.git"
    return json.dumps(
        {
            "status": "provisioned",
            "repo": slug,
            "clone_url": clone_url,
            "default_branch": default_branch,
            "coordination_branch": seed_cfg["branch"],
            "steps": steps,
            "partner_invited": invited_ok,
            "next": (
                f"Send your partner: `git clone {clone_url}` (they accept the "
                "GitHub invite first), then both add the agentsync MCP server "
                "pointed at their own clone and call survey()."
            ),
        },
        indent=2,
    )


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def survey() -> str:
    """Pull the latest coordination state and report what every *other* agent
    has claimed: task, files touched, dependencies, branch, status, timestamp.
    Call this before planning and again after finishing work."""
    cfg = _cfg()
    _ensure_worktree(cfg)
    claims = _read_claims(cfg)["claims"]
    others = {k: v for k, v in claims.items() if k != cfg["agent"]}
    return json.dumps(
        {"me": cfg["agent"], "branch": cfg["branch"], "partners": others},
        indent=2,
    )


@mcp.tool()
def claim(
    task: str,
    touches: list[str],
    requires: list[str] | None = None,
    branch: str = "",
    force: bool = False,
) -> str:
    """Stake a claim on a unit of work.

    touches  : files/modules you will modify
    requires : files/modules you depend on (omit if none)
    branch   : the branch your work will live on
    force    : claim even if an overlap with an active peer claim is detected

    Refuses (status="blocked") if your plan collides with a peer's active claim,
    returning exactly what overlaps and with whom, unless force=True. The
    overlap is evaluated against freshly fetched state immediately before the
    push, so a peer who claimed first will be seen here."""
    cfg = _cfg()
    requires = requires or []
    for attempt in range(PUSH_RETRIES):
        _ensure_worktree(cfg)
        data = _read_claims(cfg)
        claims = data["claims"]

        if not force:
            blocks = {}
            for peer_id, peer in claims.items():
                if peer_id == cfg["agent"]:
                    continue
                reasons = _overlap(touches, requires, peer)
                if reasons:
                    blocks[peer_id] = {
                        "their_task": peer.get("task"),
                        "their_branch": peer.get("branch"),
                        "reasons": reasons,
                    }
            if blocks:
                return json.dumps(
                    {
                        "status": "blocked",
                        "message": "Overlap with an active peer claim. "
                        "Narrow `touches`, wait, or re-call with force=True.",
                        "conflicts": blocks,
                    },
                    indent=2,
                )

        claims[cfg["agent"]] = {
            "task": task,
            "touches": touches,
            "requires": requires,
            "branch": branch,
            "status": "in-progress",
            "updated_at": _now(),
            "note": None,
        }
        with open(os.path.join(cfg["worktree"], CLAIMS_FILE), "w") as f:
            json.dump(data, f, indent=2)
        if _commit_and_push(cfg, f"agentsync: {cfg['agent']} claims '{task}'"):
            return json.dumps(
                {"status": "claimed", "claim": claims[cfg["agent"]]}, indent=2
            )
        time.sleep(0.4 * (attempt + 1))  # contended; back off and retry
    return json.dumps(
        {"status": "retry_exhausted", "message": "Push kept losing the race; "
         "call survey() and try again."}
    )


@mcp.tool()
def check_conflicts(against_branch: str = "") -> str:
    """Detect conflicts between your branch and your partners' branches.

    Reports two levels:
      claim_overlap  : set intersection of touched files (intent level)
      merge_conflict : a real dry-run merge via `git merge-tree` (textual)

    against_branch lets you check one specific branch; default checks every
    branch named in a peer's active claim. Your own branch is taken from your
    current claim."""
    cfg = _cfg()
    _ensure_worktree(cfg)
    claims = _read_claims(cfg)["claims"]
    mine = claims.get(cfg["agent"])
    if not mine or not mine.get("branch"):
        return json.dumps(
            {"error": "No branch on your own claim. Call claim(...) first."}
        )
    my_branch = mine["branch"]
    my_touches = set(mine.get("touches", []))

    if against_branch:
        targets = [(None, against_branch, set())]
    else:
        targets = [
            (pid, p["branch"], set(p.get("touches", [])))
            for pid, p in claims.items()
            if pid != cfg["agent"] and p.get("status") != "done" and p.get("branch")
        ]
    if not targets:
        return json.dumps({"message": "No partner branches to check."})

    repo, remote = cfg["repo"], cfg["remote"]
    _git(["fetch", remote, "--prune"], repo, check=False)
    results = []
    for pid, br, their_touches in targets:
        overlap = sorted(my_touches & their_touches)
        # resolve refs (prefer remote-tracking) and dry-run merge
        ref_mine = f"{remote}/{my_branch}"
        ref_their = f"{remote}/{br}"
        mt = _git(
            ["merge-tree", "--write-tree", "--name-only", ref_mine, ref_their],
            repo,
            check=False,
        )
        if mt.returncode == 0:
            merge = {"conflict": False}
        elif mt.returncode == 1:
            # output: <tree-oid>, then conflicted paths, then informational text
            noise = ("Auto-merging", "CONFLICT", "warning:", "Already up")
            files = [
                l for l in mt.stdout.splitlines()[1:]
                if l.strip() and not l.startswith(noise)
            ]
            merge = {"conflict": True, "files": files}
        else:
            merge = {"conflict": "unknown", "detail": mt.stderr.strip()[:300]}
        results.append(
            {
                "partner": pid,
                "their_branch": br,
                "claim_overlap": overlap,
                "merge_conflict": merge,
            }
        )
    return json.dumps({"my_branch": my_branch, "results": results}, indent=2)


@mcp.tool()
def update_status(status: str, note: str = "") -> str:
    """Update your own claim's status (e.g. 'in-progress' -> 'done') and
    optionally attach a note for your partner. Pushes immediately."""
    if status not in {"planning", "in-progress", "done"}:
        return json.dumps(
            {"error": "status must be planning | in-progress | done"}
        )
    cfg = _cfg()
    for attempt in range(PUSH_RETRIES):
        _ensure_worktree(cfg)
        data = _read_claims(cfg)
        mine = data["claims"].get(cfg["agent"])
        if not mine:
            return json.dumps({"error": "No claim to update. Call claim() first."})
        mine["status"] = status
        mine["updated_at"] = _now()
        if note:
            mine["note"] = note
        with open(os.path.join(cfg["worktree"], CLAIMS_FILE), "w") as f:
            json.dump(data, f, indent=2)
        if _commit_and_push(cfg, f"agentsync: {cfg['agent']} -> {status}"):
            return json.dumps({"status": "updated", "claim": mine}, indent=2)
        time.sleep(0.4 * (attempt + 1))
    return json.dumps({"status": "retry_exhausted"})


if __name__ == "__main__":
    mcp.run()
