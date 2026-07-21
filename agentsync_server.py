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

import contextlib
import fnmatch
import json
import os
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("agentsync")

CLAIMS_FILE = "claims.json"
PUSH_RETRIES = 5

# A random token per running server process. Stamped onto every claim this
# process writes so we can detect when a second collaborator has picked the same
# AGENTSYNC_AGENT_ID (they'd otherwise silently overwrite each other's entry).
INSTANCE = uuid.uuid4().hex[:8]

# An in-progress claim older than this many hours is flagged 'stale' by survey()
# — the signal that a partner may have crashed or wandered off holding files.
STALE_HOURS = float(os.environ.get("AGENTSYNC_STALE_HOURS", "24"))

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


def _xylem_active_project_file():
    """Path to the shared Xylem session pointer (overridable for tests)."""
    override = os.environ.get("XYLEM_ACTIVE_PROJECT_FILE")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".xylem", "active_project.json")


def _xylem_session_project():
    """The session's project path from the shared Xylem pointer the SessionStart
    hook writes, or None. Lets a persistent server follow whichever project the
    session is in when AGENTSYNC_REPO is not pinned. Never raises."""
    try:
        with open(_xylem_active_project_file(), encoding="utf-8") as f:
            proj = json.load(f).get("project")
    except (OSError, ValueError, AttributeError):
        return None
    return proj if isinstance(proj, str) and os.path.isdir(proj) else None


def _cfg(require_git=True):
    # AGENTSYNC_REPO (explicit pin) wins; otherwise follow the session's project
    # recorded by the Xylem SessionStart hook, so one global config coordinates
    # whichever repo the session is in instead of a frozen install-time path.
    repo = os.environ.get("AGENTSYNC_REPO") or _xylem_session_project()
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
            encoding="utf-8", errors="replace",   # never decode with the cp1252
            # locale default: a cp1252-undefined byte (e.g. a smart quote in a
            # commit subject) would otherwise crash the reader and leave stdout None.
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
            encoding="utf-8", errors="replace",   # never decode with the cp1252 locale default
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


def _ref_exists(repo, ref):
    """True if `ref` resolves to a commit in `repo`. Used to tell 'branch not
    pushed yet' apart from a real merge conflict in check_conflicts."""
    return _git(
        ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo, check=False
    ).returncode == 0


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


def _branch_changes(cfg, branch):
    """The files a claimed branch changed vs the default branch (three-dot, so
    only this branch's own work). Returns a list of {status, path}, or None if
    the branch isn't pushed yet / can't be diffed. Used to auto-fill a 'done'
    claim with real data instead of a hand-written summary."""
    repo, remote = cfg["repo"], cfg["remote"]
    _git(["fetch", remote, "--prune"], repo, check=False)
    tip = f"{remote}/{branch}"
    if not _ref_exists(repo, tip):
        return None
    base = _default_remote_head(cfg)
    p = _git(["diff", "--name-status", f"{base}...{tip}"], repo, check=False)
    if p.returncode != 0:
        return None
    changes = []
    for line in p.stdout.splitlines():
        line = line.rstrip()
        if not line:
            continue
        parts = line.split("\t")
        changes.append({"status": parts[0], "path": parts[-1]})
    return changes


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
            _write_claims_atomic(path, {"claims": {}})
            _git(["add", CLAIMS_FILE], wt)
            _git(["commit", "-m", "agentsync: initialize claims"], wt)
            _git(["push", "-u", remote, branch], wt)
        return

    # worktree exists -> hard-sync to remote tip if the branch is published
    if _remote_has_branch(cfg):
        _git(["fetch", remote, branch], wt, check=False)
        _git(["reset", "--hard", f"{remote}/{branch}"], wt, check=False)


def _write_claims_atomic(path, data):
    """Write claims.json airtight: dump to a unique temp file in the same
    directory, then os.replace() over the target. os.replace is atomic on both
    POSIX and Windows, so a reader (or a crash mid-write) never sees a truncated
    file. Same-directory temp keeps the rename on one filesystem. The temp name
    is unique so concurrent writers in the same worktree don't clobber it, and
    _commit_and_push only `git add`s CLAIMS_FILE, so a stray temp is never staged."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".claims-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_claims(cfg):
    path = os.path.join(cfg["worktree"], CLAIMS_FILE)
    if not os.path.exists(path):
        return {"claims": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        # Contract preserved (treat as no claims), but make the failure visible —
        # a corrupt claims.json should not silently read as "nobody holds anything".
        _log(f"WARNING: could not parse {path}: {e!r} — treating as empty")
        data = {"claims": {}}
    data.setdefault("claims", {})
    return data


def _now():
    return datetime.now(timezone.utc).isoformat()


def _age_hours(entry):
    """Hours since a claim was last updated, or None if the timestamp is
    missing/unparseable (so test fixtures with placeholder stamps don't crash)."""
    ts = entry.get("updated_at")
    if not ts:
        return None
    try:
        then = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600.0


def _annotate(entry):
    """Return a shallow copy of a claim with derived age/stale fields for
    display — never mutates the stored claim."""
    view = dict(entry)
    age = _age_hours(entry)
    view["age_hours"] = None if age is None else round(age, 1)
    view["stale"] = bool(
        age is not None and age > STALE_HOURS and entry.get("status") != "done"
    )
    return view


def _split_users(s):
    """Parse one or more GitHub usernames from a comma/space/newline-separated
    string. Lets a single tool arg invite a whole team at once."""
    if not s:
        return []
    parts = s.replace(",", " ").split()
    seen, out = set(), []
    for u in parts:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


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
_GLOB_CHARS = set("*?[")


def _norm_path(p):
    """Normalize a claimed path for comparison: forward slashes, no leading
    './', no trailing '/', no duplicate separators. This is what makes
    'auth.py', './auth.py', and 'src//auth.py' compare as the same thing."""
    p = (p or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    while "//" in p:
        p = p.replace("//", "/")
    return p.rstrip("/")


def _is_glob(p):
    return any(c in _GLOB_CHARS for c in p)


def _glob_match(pattern, path):
    # fnmatch's '*' already spans '/', so a directory glob like 'src/**' matches
    # 'src/a/b.py'. Collapse '**' to '*' so an explicit '**' behaves the same.
    pat = pattern.replace("**", "*")
    return fnmatch.fnmatch(path, pat)


def _paths_overlap(a, b):
    """True if two claimed paths refer to overlapping work. Beyond exact match
    this catches directory containment ('src/api' vs 'src/api/routes.py') and
    globs ('src/**', '*.py') in either direction — the cases plain string
    intersection silently missed."""
    a, b = _norm_path(a), _norm_path(b)
    if not a or not b:
        return False
    if a == b:
        return True
    # directory containment, either direction
    if b.startswith(a + "/") or a.startswith(b + "/"):
        return True
    # glob, either direction
    if _is_glob(a) and _glob_match(a, b):
        return True
    if _is_glob(b) and _glob_match(b, a):
        return True
    return False


def _match_files(mine, theirs):
    """The declared paths (from both sides) that overlap — the specifics we
    report back so the caller sees exactly what collided."""
    hits = set()
    for m in mine:
        for t in theirs:
            if _paths_overlap(m, t):
                hits.add(m)
                hits.add(t)
    return sorted(hits)


def _overlap(my_touches, my_requires, peer):
    """Return reasons this agent's plan conflicts with a peer's active claim."""
    if peer.get("status") == "done":
        return []
    pt = list(peer.get("touches", []))
    reasons = []
    both_touch = _match_files(my_touches, pt)
    if both_touch:
        reasons.append({"type": "shared_files", "files": both_touch})
    dep_on_wip = _match_files(my_requires, pt)
    if dep_on_wip:
        reasons.append({"type": "depends_on_their_wip", "files": dep_on_wip})
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
    """Invite one or more people as collaborators on the shared repo so they can
    push to it. Use this when the repo already exists and you just want to grant
    partners access (provision() does this too, but only as part of first-time
    setup). This is how you build a team of more than two.

    github_username : one or more GitHub users, comma- or space-separated
                      (e.g. "jarmstrong158" or "alice, bob, carol").
    permission      : pull | triage | push | maintain | admin  (default push).

    Each invited user must accept the GitHub invitation before they can push.
    Requires the `gh` CLI authenticated with admin on the repo. Returns the
    clone URL to hand the new collaborators."""
    if permission not in VALID_PERMISSIONS:
        return json.dumps(
            {"error": f"permission must be one of {', '.join(VALID_PERMISSIONS)}"}
        )
    users = _split_users(github_username)
    if not users:
        return json.dumps({"error": "No GitHub username given."})
    cfg = _cfg()
    slug = _repo_slug(cfg["repo"])
    if not slug:
        return json.dumps(
            {"error": "Could not determine the GitHub repo from this clone's "
             "'origin' remote. Is it a GitHub repo with a remote set?"}
        )
    results = []
    for user in users:
        ok, msg = _invite_collaborator(slug, user, permission)
        results.append({"collaborator": user, "invited": ok, "message": msg})
    any_ok = any(r["invited"] for r in results)
    clone_url = f"https://github.com/{slug}.git"
    return json.dumps(
        {
            "status": "invited" if any_ok else "failed",
            "repo": slug,
            "permission": permission,
            "results": results,
            "clone_url": clone_url,
            "next": (
                f"Tell each invitee to accept the GitHub invite, then "
                f"`git clone {clone_url}` and point their agentsync server at "
                "that clone with a UNIQUE AGENTSYNC_AGENT_ID."
            ) if any_ok else None,
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
      5. Invite the partner(s) as push collaborators, if any usernames are given
         here or via AGENTSYNC_PARTNER_GITHUB.

    repo            : 'owner/name', bare 'name' (owner = you), or '' to use the
                      AGENTSYNC_REPO folder name.
    partner_github  : one or more GitHub usernames to invite, comma- or
                      space-separated (overrides env). Supports a whole team.
    private         : create the repo private (default) or public.
    description     : optional GitHub repo description.

    Requires the `gh` CLI, authenticated (`gh auth login`) with 'repo' scope.
    Returns a summary of what was created plus the clone URL to send your
    partners."""
    cfg = _cfg(require_git=False)
    repo_path, remote = cfg["repo"], cfg["remote"]
    partners = _split_users(partner_github or cfg["partner_github"])
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
                with open(readme, "w", encoding="utf-8") as f:
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

    # 5. invite the partner(s)
    invited = []
    for partner in partners:
        ok, invite = _invite_collaborator(slug, partner)
        invited.append({"collaborator": partner, "invited": ok, "message": invite})
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
            "partners_invited": invited,
            "partner_invited": any(p["invited"] for p in invited),
            "next": (
                f"Send each partner: `git clone {clone_url}` (they accept the "
                "GitHub invite first), then everyone adds the agentsync MCP "
                "server pointed at their own clone with a UNIQUE "
                "AGENTSYNC_AGENT_ID and calls survey()."
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
    Works for any number of collaborators, not just one.

    Each partner entry is annotated with `age_hours` and a `stale` flag (an
    in-progress claim older than AGENTSYNC_STALE_HOURS, default 24h) so you can
    spot a partner who may have crashed or wandered off still holding files.
    Call this before planning and again after finishing work."""
    cfg = _cfg()
    _ensure_worktree(cfg)
    claims = _read_claims(cfg)["claims"]
    others = {k: _annotate(v) for k, v in claims.items() if k != cfg["agent"]}
    stale = sorted(k for k, v in others.items() if v["stale"])
    return json.dumps(
        {
            "me": cfg["agent"],
            "branch": cfg["branch"],
            "partners": others,
            "stale_claims": stale,
        },
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

        # duplicate-id guard: our id already holds an active claim stamped by a
        # *different* server process -> likely two people sharing one agent id.
        prior = claims.get(cfg["agent"])
        dup_warn = None
        if (prior and prior.get("instance") and prior.get("instance") != INSTANCE
                and prior.get("status") == "in-progress"):
            dup_warn = (
                f"Agent id '{cfg['agent']}' already holds an in-progress claim "
                "written by a different agentsync instance. If a teammate is "
                "using the same AGENTSYNC_AGENT_ID, give each person a unique id "
                "— otherwise you overwrite each other's claims."
            )

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
            "instance": INSTANCE,
            "note": None,
        }
        _write_claims_atomic(os.path.join(cfg["worktree"], CLAIMS_FILE), data)
        if _commit_and_push(cfg, f"agentsync: {cfg['agent']} claims '{task}'"):
            result = {"status": "claimed", "claim": claims[cfg["agent"]]}
            if dup_warn:
                result["warning"] = dup_warn
            return json.dumps(result, indent=2)
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
        overlap = _match_files(my_touches, their_touches)
        # resolve refs (prefer remote-tracking) and dry-run merge
        ref_mine = f"{remote}/{my_branch}"
        ref_their = f"{remote}/{br}"
        # A missing ref (branch not pushed yet) makes merge-tree compare against
        # nothing and return a misleading result. Report the real state instead of
        # a phantom conflict — the claim_overlap above still tells you the intent.
        if not _ref_exists(repo, ref_their):
            merge = {"conflict": "branch_not_pushed",
                     "note": f"partner branch '{br}' is not on '{remote}' yet — "
                             "nothing to merge-test (claim_overlap still applies)"}
            results.append({"partner": pid, "their_branch": br,
                            "claim_overlap": overlap, "merge_conflict": merge})
            continue
        if not _ref_exists(repo, ref_mine):
            merge = {"conflict": "branch_not_pushed",
                     "note": f"your branch '{my_branch}' is not on '{remote}' yet — "
                             "push it, then re-run check_conflicts"}
            results.append({"partner": pid, "their_branch": br,
                            "claim_overlap": overlap, "merge_conflict": merge})
            continue
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
            if files:
                merge = {"conflict": True, "files": files}
            else:
                # rc=1 but every line matched the noise filter: we can't name a
                # conflicting path, so a bare conflict:true would be misleading.
                # Downgrade to 'unknown' and surface the raw output to inspect.
                merge = {"conflict": "unknown",
                         "note": "merge-tree signalled a conflict but no file "
                                 "paths survived filtering; inspect raw output",
                         "raw": mt.stdout.strip()[:300]}
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
def release(note: str = "") -> str:
    """Abandon your current claim WITHOUT marking it done, freeing the files you
    were holding so a partner can take them over. Use this when you're dropping
    the task or stepping away — otherwise a crashed or abandoned claim blocks
    those files indefinitely (the only other exits are 'done' or manual git
    surgery). Pushes immediately."""
    cfg = _cfg()
    for attempt in range(PUSH_RETRIES):
        _ensure_worktree(cfg)
        data = _read_claims(cfg)
        if cfg["agent"] not in data["claims"]:
            return json.dumps(
                {"status": "noop", "message": "You have no active claim to release."}
            )
        released = data["claims"].pop(cfg["agent"])
        _write_claims_atomic(os.path.join(cfg["worktree"], CLAIMS_FILE), data)
        msg = f"agentsync: {cfg['agent']} releases '{released.get('task')}'"
        if note:
            msg += f" ({note})"
        if _commit_and_push(cfg, msg):
            return json.dumps({"status": "released", "released": released}, indent=2)
        time.sleep(0.4 * (attempt + 1))
    return json.dumps({"status": "retry_exhausted"})


def _set_status(cfg, status, note):
    """CAS-update this agent's claim status. On 'done', auto-capture the claimed
    branch's diffstat into `changed_files` so a partner reconciling has real data,
    not just a hand-written note. Returns (ok, result) where result is the claim
    dict on success or an error/status string on failure."""
    done_changes = None
    computed = False
    for attempt in range(PUSH_RETRIES):
        _ensure_worktree(cfg)
        data = _read_claims(cfg)
        mine = data["claims"].get(cfg["agent"])
        if not mine:
            return False, "No claim to update. Call claim() first."
        mine["status"] = status
        mine["updated_at"] = _now()
        mine["instance"] = INSTANCE
        if note:
            mine["note"] = note
        if status == "done" and mine.get("branch"):
            if not computed:  # branch is stable across retries; compute once
                done_changes = _branch_changes(cfg, mine["branch"])
                computed = True
            mine["changed_files"] = done_changes
        _write_claims_atomic(os.path.join(cfg["worktree"], CLAIMS_FILE), data)
        if _commit_and_push(cfg, f"agentsync: {cfg['agent']} -> {status}"):
            return True, mine
        time.sleep(0.4 * (attempt + 1))
    return False, "retry_exhausted"


@mcp.tool()
def update_status(status: str, note: str = "") -> str:
    """Update your own claim's status (e.g. 'in-progress' -> 'done') and
    optionally attach a note for your partner. Pushes immediately. On 'done' the
    claim is auto-annotated with `changed_files` (your branch's diffstat vs the
    default branch). To drop a claim without finishing it, use release(); to
    finish AND open a PR, use finish()."""
    if status not in {"planning", "in-progress", "done"}:
        return json.dumps(
            {"error": "status must be planning | in-progress | done"}
        )
    cfg = _cfg()
    ok, res = _set_status(cfg, status, note)
    if not ok:
        if res == "retry_exhausted":
            return json.dumps({"status": "retry_exhausted"})
        return json.dumps({"error": res})
    return json.dumps({"status": "updated", "claim": res}, indent=2)


@mcp.tool()
def finish(note: str = "", title: str = "", draft: bool = False) -> str:
    """Close the loop: mark your claim done AND open a GitHub pull request from
    your claimed branch into the default branch, so your work lands in review.

    note   : PR body (falls back to your claim's existing note).
    title  : PR title (falls back to your claim's task).
    draft  : open the PR as a draft.

    Your branch must already be pushed. If a PR for the branch already exists,
    its URL is returned instead of erroring. Requires the `gh` CLI. The claim is
    marked done (with auto-captured `changed_files`) after the PR is opened."""
    cfg = _cfg()
    _ensure_worktree(cfg)
    mine = _read_claims(cfg)["claims"].get(cfg["agent"])
    if not mine or not mine.get("branch"):
        return json.dumps(
            {"error": "No branch on your claim. Call claim(...) first."}
        )
    branch = mine["branch"]
    repo, remote = cfg["repo"], cfg["remote"]
    _git(["fetch", remote, "--prune"], repo, check=False)
    if not _ref_exists(repo, f"{remote}/{branch}"):
        return json.dumps(
            {"error": f"Branch '{branch}' isn't on '{remote}' yet — push it, "
             "then call finish()."}
        )

    base = _default_remote_head(cfg).rsplit("/", 1)[-1]  # 'origin/main' -> 'main'
    pr_title = title or mine.get("task") or branch
    pr_body = note or mine.get("note") or ""
    args = ["pr", "create", "--head", branch, "--base", base,
            "--title", pr_title, "--body", pr_body]
    if draft:
        args.append("--draft")
    created = _gh(args, cwd=repo, check=False)
    if created.returncode == 0:
        pr_url = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else ""
    else:
        # most common non-zero: a PR for this branch already exists
        view = _gh(["pr", "view", branch, "--json", "url", "--jq", ".url"],
                   cwd=repo, check=False)
        if view.returncode == 0 and view.stdout.strip():
            pr_url = view.stdout.strip()
        else:
            return json.dumps(
                {"status": "pr_failed",
                 "detail": created.stderr.strip()[:300],
                 "hint": "Open the PR manually; your claim was NOT marked done."}
            )

    ok, res = _set_status(cfg, "done", note)
    return json.dumps(
        {
            "status": "finished" if ok else "pr_opened_status_failed",
            "pr_url": pr_url,
            "branch": branch,
            "claim": res if ok else None,
        },
        indent=2,
    )


@mcp.tool()
def history(limit: int = 20) -> str:
    """The coordination timeline: who claimed, finished, or released what, and
    when — read from the git history of claims.json. Newest first. `limit` caps
    how many events are returned. Useful for 'what has my partner been doing?'
    without needing them online."""
    cfg = _cfg()
    _ensure_worktree(cfg)
    limit = max(1, min(int(limit), 200))
    p = _git(
        ["log", f"-{limit}", "--format=%H%x1f%aI%x1f%an%x1f%s", "--", CLAIMS_FILE],
        cfg["worktree"], check=False,
    )
    events = []
    for line in p.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            h, when, who, subject = parts
            events.append(
                {"commit": h[:10], "at": when, "by": who, "event": subject}
            )
    return json.dumps(
        {"branch": cfg["branch"], "count": len(events), "events": events},
        indent=2,
    )


def main():
    """Console entry point (pip install agentsync-mcp -> `agentsync-mcp`)."""
    mcp.run()


if __name__ == "__main__":
    main()
