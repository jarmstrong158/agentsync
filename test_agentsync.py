#!/usr/bin/env python3
"""
Integration test suite for the agentsync MCP server.

Each test spins up real git repositories (a bare "origin" + clones) in a temp
dir and drives the actual tool functions — no mocks for git, these are real
pushes. The GitHub CLI is stubbed only where a test would otherwise create a
real remote repo (the provision tests); every git operation underneath stays
real.

Run:  python3 test_agentsync.py
(Requires git on PATH and `pip install mcp`.)
"""

import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))


def load_server():
    spec = importlib.util.spec_from_file_location(
        "agentsync_server", os.path.join(HERE, "agentsync_server.py")
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


M = load_server()


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def setup_lab(root):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.io",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.io"}
    os.environ.update(env)
    origin = os.path.join(root, "origin.git")
    git(["init", "-q", "--bare", "-b", "main", origin], root)
    seed = os.path.join(root, "seed")
    git(["clone", "-q", origin, seed], root)
    with open(os.path.join(seed, "README.md"), "w") as f:
        f.write("# project\nbase\n")
    git(["add", "."], seed); git(["commit", "-qm", "init"], seed)
    git(["push", "-q", "origin", "main"], seed)
    clones = {}
    for who in ("jonny", "partner"):
        path = os.path.join(root, who)
        git(["clone", "-q", origin, path], root)
        git(["remote", "set-head", "origin", "main"], path)
        clones[who] = path
    return origin, clones


@contextlib.contextmanager
def lab():
    """A fresh origin + jonny/partner clones, cleaned up afterward."""
    root = tempfile.mkdtemp(prefix="agentsync_test_")
    try:
        origin, clones = setup_lab(root)
        yield root, origin, clones
    finally:
        shutil.rmtree(root, ignore_errors=True)


def be(clones, who):
    os.environ["AGENTSYNC_REPO"] = clones[who]
    os.environ["AGENTSYNC_AGENT_ID"] = who
    os.environ.pop("AGENTSYNC_PARTNER_GITHUB", None)


def peer_push_claim(clone, branch, agent_id, entry):
    """Land a claim entry on the coordination branch out-of-band, as if a peer's
    agent pushed it. Used to force the compare-and-swap path."""
    git(["fetch", "-q", "origin", branch], clone)
    git(["checkout", "-q", "-B", branch, f"origin/{branch}"], clone)
    path = os.path.join(clone, "claims.json")
    data = {"claims": {}}
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
    data.setdefault("claims", {})[agent_id] = entry
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    git(["add", "claims.json"], clone)
    git(["commit", "-qm", f"peer {agent_id}"], clone)
    git(["push", "-q", "origin", branch], clone)


# --------------------------------------------------------------------------- #
# protocol tests
# --------------------------------------------------------------------------- #
def test_survey_empty_then_visible():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        # auto-creates the agentsync branch; nobody else yet
        assert json.loads(M.survey())["partners"] == {}, "expected no partners"
        r = json.loads(M.claim("auth", ["auth.py"], requires=["db.sql"],
                               branch="jonny/auth"))
        assert r["status"] == "claimed", r
        # partner now sees jonny
        be(clones, "partner")
        partners = json.loads(M.survey())["partners"]
        assert "jonny" in partners, partners
        assert partners["jonny"]["task"] == "auth", partners


def test_block_on_shared_file():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("auth", ["auth.py"], branch="jonny/auth")
        be(clones, "partner")
        r = json.loads(M.claim("auth2", ["auth.py"], branch="partner/auth"))
        assert r["status"] == "blocked", r
        assert r["conflicts"]["jonny"]["reasons"][0]["type"] == "shared_files", r
        assert r["conflicts"]["jonny"]["reasons"][0]["files"] == ["auth.py"], r


def test_block_on_dependency_on_wip():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("auth", ["auth.py"], branch="jonny/auth")
        be(clones, "partner")
        r = json.loads(M.claim("ui", ["ui.py"], requires=["auth.py"],
                               branch="partner/ui"))
        assert r["status"] == "blocked", r
        assert r["conflicts"]["jonny"]["reasons"][0]["type"] == "depends_on_their_wip", r


def test_clean_claim_succeeds():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("auth", ["auth.py"], branch="jonny/auth")
        be(clones, "partner")
        r = json.loads(M.claim("ui", ["ui.py"], branch="partner/ui"))
        assert r["status"] == "claimed", r


def test_force_overrides_block():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("auth", ["auth.py"], branch="jonny/auth")
        be(clones, "partner")
        # would be blocked, but force=True claims anyway
        r = json.loads(M.claim("auth2", ["auth.py"], branch="partner/auth",
                               force=True))
        assert r["status"] == "claimed", r


def test_done_claim_does_not_block():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("auth", ["auth.py"], branch="jonny/auth")
        M.update_status("done", note="auth finished")
        be(clones, "partner")
        # jonny's claim is done -> no longer an obstacle on the same file
        r = json.loads(M.claim("auth2", ["auth.py"], branch="partner/auth"))
        assert r["status"] == "claimed", r


def test_update_status_validation():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("auth", ["auth.py"], branch="jonny/auth")
        r = json.loads(M.update_status("bogus"))
        assert "error" in r, r
        r = json.loads(M.update_status("done", note="ok"))
        assert r["status"] == "updated", r


def test_check_conflicts_requires_own_claim():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.survey()  # create the branch but make no claim
        r = json.loads(M.check_conflicts())
        assert "error" in r, r


def test_textual_conflict_detected():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("auth", ["auth.py"], branch="jonny/auth")
        be(clones, "partner")
        M.claim("ui", ["ui.py"], branch="partner/ui")
        # both branches edit the same README line -> a real merge conflict
        for who, line in (("jonny", "jonny/auth"), ("partner", "partner/ui")):
            p = clones[who]
            git(["checkout", "-qb", line], p)
            with open(os.path.join(p, "README.md"), "w") as f:
                f.write(f"# project\n{who.upper()}\n")
            git(["commit", "-qam", "edit"], p)
            git(["push", "-q", "origin", line], p)
            git(["checkout", "-q", "main"], p)
        be(clones, "jonny")
        res = json.loads(M.check_conflicts())["results"][0]
        assert res["merge_conflict"]["conflict"] is True, res
        assert res["merge_conflict"]["files"] == ["README.md"], res


def test_no_textual_conflict_when_disjoint():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("auth", ["auth.py"], branch="jonny/auth")
        be(clones, "partner")
        M.claim("ui", ["ui.py"], branch="partner/ui")
        # each edits a different new file -> clean merge
        for who, line, fname in (("jonny", "jonny/auth", "auth.py"),
                                 ("partner", "partner/ui", "ui.py")):
            p = clones[who]
            git(["checkout", "-qb", line], p)
            with open(os.path.join(p, fname), "w") as f:
                f.write(f"# {who}\n")
            git(["add", fname], p)
            git(["commit", "-qm", "edit"], p)
            git(["push", "-q", "origin", line], p)
            git(["checkout", "-q", "main"], p)
        be(clones, "jonny")
        res = json.loads(M.check_conflicts())["results"][0]
        assert res["merge_conflict"]["conflict"] is False, res
        assert res["claim_overlap"] == [], res


def test_check_conflicts_partner_branch_not_pushed():
    """A partner who has claimed but not yet pushed their work branch must be
    reported as 'branch_not_pushed', NOT a phantom conflict against a missing
    remote ref (merge-tree vs a ref that doesn't exist)."""
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("auth", ["auth.py"], branch="jonny/auth")   # work branch never pushed
        be(clones, "partner")
        M.claim("ui", ["ui.py"], branch="partner/ui")       # never pushed either
        be(clones, "jonny")
        res = json.loads(M.check_conflicts())["results"][0]
        assert res["merge_conflict"]["conflict"] == "branch_not_pushed", res
        assert "partner/ui" in res["merge_conflict"]["note"], res
        # claim_overlap still reports intent-level info even without a mergeable ref
        assert res["claim_overlap"] == [], res


# --------------------------------------------------------------------------- #
# compare-and-swap (the core mutual-exclusion guarantee)
# --------------------------------------------------------------------------- #
def test_cas_peer_entry_survives_retry():
    """A peer claim that lands between our read and our push must NOT be lost by
    our retry — we only edit our own key."""
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.survey()  # ensure the coordination branch exists
        scratch = os.path.join(root, "scratch")
        git(["clone", "-q", origin, scratch], root)

        real = M._commit_and_push
        fired = {"done": False}

        def wrapper(cfg, message):
            if not fired["done"]:
                fired["done"] = True
                peer_push_claim(scratch, cfg["branch"], "external", {
                    "task": "side", "touches": ["z.py"], "requires": [],
                    "branch": "ext/side", "status": "in-progress",
                    "updated_at": "t", "note": "hi from peer",
                })
            return real(cfg, message)

        M._commit_and_push = wrapper
        try:
            r = json.loads(M.claim("mine", ["a.py"], branch="jonny/a"))
        finally:
            M._commit_and_push = real
        assert r["status"] == "claimed", r
        partners = json.loads(M.survey())["partners"]
        assert "external" in partners, partners
        assert partners["external"]["note"] == "hi from peer", partners


def test_cas_colliding_peer_blocks_on_retry():
    """If the peer claim that lands mid-flight collides with us, the retry must
    OBSERVE it and block — proving overlap is evaluated against fresh state."""
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.survey()
        scratch = os.path.join(root, "scratch")
        git(["clone", "-q", origin, scratch], root)

        real = M._commit_and_push
        fired = {"done": False}

        def wrapper(cfg, message):
            if not fired["done"]:
                fired["done"] = True
                peer_push_claim(scratch, cfg["branch"], "external", {
                    "task": "same", "touches": ["a.py"], "requires": [],
                    "branch": "ext/a", "status": "in-progress",
                    "updated_at": "t", "note": None,
                })
            return real(cfg, message)

        M._commit_and_push = wrapper
        try:
            r = json.loads(M.claim("mine", ["a.py"], branch="jonny/a"))
        finally:
            M._commit_and_push = real
        assert r["status"] == "blocked", r
        assert "external" in r["conflicts"], r
        assert r["conflicts"]["external"]["reasons"][0]["files"] == ["a.py"], r


# --------------------------------------------------------------------------- #
# error handling
# --------------------------------------------------------------------------- #
def test_gh_missing_friendly_error():
    orig = M.subprocess.run

    def boom(*a, **k):
        raise FileNotFoundError("gh")

    M.subprocess.run = boom
    try:
        M._gh(["--version"])
        assert False, "expected RuntimeError when gh is absent"
    except RuntimeError as e:
        assert "GitHub CLI" in str(e), e
    finally:
        M.subprocess.run = orig


# --------------------------------------------------------------------------- #
# provisioning (gh CLI stubbed; a local bare repo stands in for GitHub)
# --------------------------------------------------------------------------- #
def install_gh_stub(root, login="tester"):
    remotes = os.path.join(root, "remotes")
    os.makedirs(remotes, exist_ok=True)
    record = {"created": [], "invites": []}

    def bare_for(slug):
        return os.path.join(remotes, slug.replace("/", "__") + ".git")

    M._gh_login = lambda: login
    M._gh_repo_exists = lambda slug: os.path.isdir(bare_for(slug))

    def fake_gh(args, cwd=None, check=True):
        if args[:2] == ["repo", "create"]:
            slug = args[2]
            bare = bare_for(slug)
            git(["init", "-q", "--bare", "-b", "main", bare], root)
            git(["remote", "add", "origin", bare], cwd)
            git(["push", "-q", "-u", "origin", "main"], cwd)
            record["created"].append(slug)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:3] == ["api", "-X", "PUT"]:
            record["invites"].append(args[3].rsplit("/", 1)[-1])  # username
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected gh call: {args}")

    M._gh = fake_gh
    return record, bare_for


def test_provision_creates_seeds_and_invites():
    root = tempfile.mkdtemp(prefix="agentsync_prov_")
    try:
        record, bare_for = install_gh_stub(root)
        repo_path = os.path.join(root, "fresh-project")  # does not exist yet
        os.environ["AGENTSYNC_REPO"] = repo_path
        os.environ["AGENTSYNC_AGENT_ID"] = "tester"
        os.environ.pop("AGENTSYNC_PARTNER_GITHUB", None)

        r = json.loads(M.provision(repo="tester/fresh-project",
                                   partner_github="buddy"))
        assert r["status"] == "provisioned", r
        assert r["repo"] == "tester/fresh-project", r
        assert r["partner_invited"] is True, r
        assert "buddy" in record["invites"], record
        # coordination branch + claims.json landed on the "remote"
        bare = bare_for("tester/fresh-project")
        ls = git(["ls-tree", "-r", "--name-only", "agentsync"], bare).stdout
        assert "claims.json" in ls, ls
        # idempotent re-run still succeeds and creates nothing new
        before = list(record["created"])
        r2 = json.loads(M.provision(repo="tester/fresh-project"))
        assert r2["status"] == "provisioned", r2
        assert record["created"] == before, record
        # the survey protocol now works on the provisioned repo
        assert json.loads(M.survey())["partners"] == {}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_provision_partner_from_env():
    root = tempfile.mkdtemp(prefix="agentsync_prov_")
    try:
        record, _ = install_gh_stub(root)
        repo_path = os.path.join(root, "envproj")
        os.environ["AGENTSYNC_REPO"] = repo_path
        os.environ["AGENTSYNC_AGENT_ID"] = "tester"
        os.environ["AGENTSYNC_PARTNER_GITHUB"] = "env-buddy"
        try:
            r = json.loads(M.provision(repo="tester/envproj"))
        finally:
            os.environ.pop("AGENTSYNC_PARTNER_GITHUB", None)
        assert r["partner_invited"] is True, r
        assert "env-buddy" in record["invites"], record
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_provision_skips_when_remote_already_configured():
    root = tempfile.mkdtemp(prefix="agentsync_prov_")
    try:
        record, bare_for = install_gh_stub(root)
        slug = "tester/preconfigured"
        bare = bare_for(slug)
        git(["init", "-q", "--bare", "-b", "main", bare], root)
        # a local repo that already has a commit and origin set
        repo_path = os.path.join(root, "preconfigured")
        os.makedirs(repo_path)
        git(["init", "-q", "-b", "main"], repo_path)
        with open(os.path.join(repo_path, "README.md"), "w") as f:
            f.write("# pre\n")
        git(["add", "-A"], repo_path)
        git(["-c", "user.email=t@t.io", "-c", "user.name=t",
             "commit", "-qm", "init"], repo_path)
        git(["remote", "add", "origin", bare], repo_path)
        os.environ["AGENTSYNC_REPO"] = repo_path
        os.environ["AGENTSYNC_AGENT_ID"] = "tester"
        os.environ.pop("AGENTSYNC_PARTNER_GITHUB", None)

        r = json.loads(M.provision(repo=slug))
        assert r["status"] == "provisioned", r
        assert record["created"] == [], "must not create when remote exists"
        assert any("remote already configured" in s for s in r["steps"]), r["steps"]
        ls = git(["ls-tree", "-r", "--name-only", "agentsync"], bare).stdout
        assert "claims.json" in ls, ls
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_provision_reports_invite_failure():
    root = tempfile.mkdtemp(prefix="agentsync_prov_")
    try:
        install_gh_stub(root)
        # override only the collaborator PUT to fail
        real_login = M._gh_login
        remotes = os.path.join(root, "remotes")

        def bare_for(slug):
            return os.path.join(remotes, slug.replace("/", "__") + ".git")

        def fake_gh(args, cwd=None, check=True):
            if args[:2] == ["repo", "create"]:
                slug = args[2]
                git(["init", "-q", "--bare", "-b", "main", bare_for(slug)], root)
                git(["remote", "add", "origin", bare_for(slug)], cwd)
                git(["push", "-q", "-u", "origin", "main"], cwd)
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if args[:3] == ["api", "-X", "PUT"]:
                return SimpleNamespace(returncode=1, stdout="",
                                       stderr="HTTP 404: user not found")
            raise AssertionError(f"unexpected gh call: {args}")

        M._gh = fake_gh
        repo_path = os.path.join(root, "failinvite")
        os.environ["AGENTSYNC_REPO"] = repo_path
        os.environ["AGENTSYNC_AGENT_ID"] = "tester"
        os.environ.pop("AGENTSYNC_PARTNER_GITHUB", None)

        r = json.loads(M.provision(repo="tester/failinvite",
                                   partner_github="ghost"))
        # provisioning still succeeds; the invite failure is reported, not fatal
        assert r["status"] == "provisioned", r
        assert r["partner_invited"] is False, r
        assert any("could not invite" in s for s in r["steps"]), r["steps"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_add_collaborator():
    root = tempfile.mkdtemp(prefix="agentsync_collab_")
    try:
        repo = os.path.join(root, "proj")
        os.makedirs(repo)
        git(["init", "-q", "-b", "main"], repo)  # _cfg() needs a .git dir
        invites = []

        def fake_gh(args, cwd=None, check=True):
            if args[:2] == ["repo", "view"]:
                return SimpleNamespace(returncode=0, stdout="tester/proj\n",
                                       stderr="")
            if args[:3] == ["api", "-X", "PUT"]:
                invites.append(args[3].rsplit("/", 1)[-1])  # username
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected gh call: {args}")

        M._gh = fake_gh
        os.environ["AGENTSYNC_REPO"] = repo
        os.environ["AGENTSYNC_AGENT_ID"] = "tester"
        os.environ.pop("AGENTSYNC_PARTNER_GITHUB", None)

        r = json.loads(M.add_collaborator("jarmstrong158"))
        assert r["status"] == "invited", r
        assert r["repo"] == "tester/proj", r
        assert r["permission"] == "push", r
        assert "jarmstrong158" in invites, invites
        assert r["clone_url"] == "https://github.com/tester/proj.git", r

        # invalid permission is rejected before any gh call
        r2 = json.loads(M.add_collaborator("x", permission="superuser"))
        assert "error" in r2, r2
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_add_collaborator_no_remote():
    root = tempfile.mkdtemp(prefix="agentsync_collab_")
    try:
        repo = os.path.join(root, "proj")
        os.makedirs(repo)
        git(["init", "-q", "-b", "main"], repo)

        def fake_gh(args, cwd=None, check=True):
            if args[:2] == ["repo", "view"]:  # no GitHub repo behind origin
                return SimpleNamespace(returncode=1, stdout="", stderr="no repo")
            raise AssertionError(f"unexpected gh call: {args}")

        M._gh = fake_gh
        os.environ["AGENTSYNC_REPO"] = repo
        os.environ["AGENTSYNC_AGENT_ID"] = "tester"

        r = json.loads(M.add_collaborator("jarmstrong158"))
        assert "error" in r, r
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- #
# path / glob overlap
# --------------------------------------------------------------------------- #
def test_block_on_directory_containment():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("api", ["src/api"], branch="jonny/api")
        be(clones, "partner")
        r = json.loads(M.claim("route", ["src/api/routes.py"],
                               branch="partner/route"))
        assert r["status"] == "blocked", r
        files = r["conflicts"]["jonny"]["reasons"][0]["files"]
        assert "src/api/routes.py" in files, r


def test_block_on_glob():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("all-src", ["src/**"], branch="jonny/src")
        be(clones, "partner")
        r = json.loads(M.claim("model", ["src/models/user.py"],
                               branch="partner/model"))
        assert r["status"] == "blocked", r


def test_path_normalization_overlap():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("auth", ["./auth.py"], branch="jonny/auth")
        be(clones, "partner")
        r = json.loads(M.claim("auth2", ["auth.py"], branch="partner/auth"))
        assert r["status"] == "blocked", r


def test_disjoint_directories_are_clean():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("api", ["src/api"], branch="jonny/api")
        be(clones, "partner")
        r = json.loads(M.claim("web", ["src/web"], branch="partner/web"))
        assert r["status"] == "claimed", r


# --------------------------------------------------------------------------- #
# release + staleness + duplicate-id guard
# --------------------------------------------------------------------------- #
def test_release_frees_the_file():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("auth", ["auth.py"], branch="jonny/auth")
        r = json.loads(M.release())
        assert r["status"] == "released", r
        # releasing again is a no-op, not an error
        assert json.loads(M.release())["status"] == "noop"
        # partner can now take the freed file
        be(clones, "partner")
        r = json.loads(M.claim("auth2", ["auth.py"], branch="partner/auth"))
        assert r["status"] == "claimed", r


def test_survey_flags_stale_claim():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.survey()  # create the coordination branch
        scratch = os.path.join(root, "scratch")
        git(["clone", "-q", origin, scratch], root)
        peer_push_claim(scratch, "agentsync", "sleepy", {
            "task": "long-gone", "touches": ["old.py"], "requires": [],
            "branch": "sleepy/x", "status": "in-progress",
            "updated_at": "2000-01-01T00:00:00+00:00", "note": None,
        })
        be(clones, "jonny")
        s = json.loads(M.survey())
        assert "sleepy" in s["stale_claims"], s
        assert s["partners"]["sleepy"]["stale"] is True, s
        assert s["partners"]["sleepy"]["age_hours"] > 24, s


def test_duplicate_agent_id_warns():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.survey()
        scratch = os.path.join(root, "scratch")
        git(["clone", "-q", origin, scratch], root)
        # an entry under "jonny" written by a *different* instance
        peer_push_claim(scratch, "agentsync", "jonny", {
            "task": "someone-elses", "touches": ["z.py"], "requires": [],
            "branch": "other/z", "status": "in-progress",
            "updated_at": M._now(), "instance": "deadbeef", "note": None,
        })
        be(clones, "jonny")
        r = json.loads(M.claim("mine", ["a.py"], branch="jonny/a"))
        assert r["status"] == "claimed", r
        assert "warning" in r and "unique" in r["warning"].lower(), r


# --------------------------------------------------------------------------- #
# multi-collaborator invites
# --------------------------------------------------------------------------- #
def test_add_multiple_collaborators():
    root = tempfile.mkdtemp(prefix="agentsync_collab_")
    try:
        repo = os.path.join(root, "proj")
        os.makedirs(repo)
        git(["init", "-q", "-b", "main"], repo)
        invites = []

        def fake_gh(args, cwd=None, check=True):
            if args[:2] == ["repo", "view"]:
                return SimpleNamespace(returncode=0, stdout="tester/proj\n",
                                       stderr="")
            if args[:3] == ["api", "-X", "PUT"]:
                invites.append(args[3].rsplit("/", 1)[-1])
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected gh call: {args}")

        M._gh = fake_gh
        os.environ["AGENTSYNC_REPO"] = repo
        os.environ["AGENTSYNC_AGENT_ID"] = "tester"
        os.environ.pop("AGENTSYNC_PARTNER_GITHUB", None)

        r = json.loads(M.add_collaborator("alice, bob carol"))
        assert r["status"] == "invited", r
        assert len(r["results"]) == 3, r
        assert set(invites) == {"alice", "bob", "carol"}, invites
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_provision_invites_multiple_partners():
    root = tempfile.mkdtemp(prefix="agentsync_prov_")
    try:
        record, bare_for = install_gh_stub(root)
        repo_path = os.path.join(root, "team-project")
        os.environ["AGENTSYNC_REPO"] = repo_path
        os.environ["AGENTSYNC_AGENT_ID"] = "tester"
        os.environ.pop("AGENTSYNC_PARTNER_GITHUB", None)

        r = json.loads(M.provision(repo="tester/team-project",
                                   partner_github="alice bob"))
        assert r["status"] == "provisioned", r
        assert r["partner_invited"] is True, r
        assert len(r["partners_invited"]) == 2, r
        assert set(record["invites"]) == {"alice", "bob"}, record
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _push_branch_with_change(clone, branch, fname, content):
    """Create `branch` off the current HEAD, add a file, and push it."""
    git(["checkout", "-qb", branch], clone)
    with open(os.path.join(clone, fname), "w") as f:
        f.write(content)
    git(["add", fname], clone)
    git(["commit", "-qm", f"add {fname}"], clone)
    git(["push", "-q", "origin", branch], clone)
    git(["checkout", "-q", "main"], clone)


# --------------------------------------------------------------------------- #
# history / done-diffstat / finish
# --------------------------------------------------------------------------- #
def test_history_timeline():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("auth", ["auth.py"], branch="jonny/auth")
        M.update_status("done")
        h = json.loads(M.history())
        subjects = [e["event"] for e in h["events"]]
        assert any("claims 'auth'" in s for s in subjects), h
        assert "-> done" in h["events"][0]["event"], h  # newest first
        assert h["events"][0]["by"], h  # author recorded


def test_done_captures_changed_files():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("auth", ["auth.py"], branch="jonny/auth")
        _push_branch_with_change(clones["jonny"], "jonny/auth", "auth.py", "x=1\n")
        be(clones, "jonny")
        r = json.loads(M.update_status("done"))
        cf = r["claim"]["changed_files"]
        assert cf and any(c["path"] == "auth.py" for c in cf), r
        assert cf[0]["status"] == "A", r  # added file


def test_finish_opens_pr_and_marks_done():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("auth", ["auth.py"], branch="jonny/auth")
        _push_branch_with_change(clones["jonny"], "jonny/auth", "auth.py", "x=1\n")

        orig = M._gh

        def fake_gh(args, cwd=None, check=True):
            if args[:2] == ["pr", "create"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout="https://github.com/o/r/pull/7\n", stderr="")
            raise AssertionError(f"unexpected gh call: {args}")

        M._gh = fake_gh
        try:
            be(clones, "jonny")
            r = json.loads(M.finish(note="please review"))
        finally:
            M._gh = orig
        assert r["status"] == "finished", r
        assert r["pr_url"].endswith("/pull/7"), r
        assert r["claim"]["status"] == "done", r
        assert r["claim"]["changed_files"], r


def test_finish_returns_existing_pr_url():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("auth", ["auth.py"], branch="jonny/auth")
        _push_branch_with_change(clones["jonny"], "jonny/auth", "auth.py", "x=1\n")

        orig = M._gh

        def fake_gh(args, cwd=None, check=True):
            if args[:2] == ["pr", "create"]:
                return SimpleNamespace(returncode=1, stdout="",
                                       stderr="a pull request already exists")
            if args[:2] == ["pr", "view"]:
                return SimpleNamespace(returncode=0,
                                       stdout="https://github.com/o/r/pull/3\n",
                                       stderr="")
            raise AssertionError(f"unexpected gh call: {args}")

        M._gh = fake_gh
        try:
            be(clones, "jonny")
            r = json.loads(M.finish())
        finally:
            M._gh = orig
        assert r["status"] == "finished", r
        assert r["pr_url"].endswith("/pull/3"), r


def test_finish_requires_pushed_branch():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("auth", ["auth.py"], branch="jonny/auth")  # never pushed
        be(clones, "jonny")
        r = json.loads(M.finish())
        assert "error" in r and "push" in r["error"].lower(), r


def test_history_survives_smart_quote_in_subject():
    """Regression: a cp1252-undefined char (smart double-quote U+201D, UTF-8
    E2 80 9D — the 0x9D byte is undefined in cp1252) in a claim task lands in a
    git commit subject. history() reads `git log --format=…%s…`; if the git
    subprocess decodes with the Windows locale default (cp1252) the reader
    crashes, stdout becomes None, and splitlines() raises AttributeError.
    With encoding='utf-8', errors='replace' the read must succeed and the text
    must round-trip."""
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        task = "fix “smart” quotes"  # curly double-quotes
        M.claim(task, ["auth.py"], branch="jonny/auth")
        M.update_status("done")
        # Must not raise (pre-fix: AttributeError on None.splitlines()).
        h = json.loads(M.history())
        subjects = [e["event"] for e in h["events"]]
        assert any("“smart”" in s for s in subjects), subjects
        # And the claim itself round-trips through claims.json unchanged.
        cj = os.path.join(clones["jonny"], ".git", "agentsync-wt", "claims.json")
        with open(cj, encoding="utf-8") as f:
            mine = json.load(f)["claims"]["jonny"]
        assert mine["task"] == task, mine


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
TESTS = [
    test_survey_empty_then_visible,
    test_block_on_shared_file,
    test_block_on_dependency_on_wip,
    test_clean_claim_succeeds,
    test_force_overrides_block,
    test_done_claim_does_not_block,
    test_update_status_validation,
    test_check_conflicts_requires_own_claim,
    test_textual_conflict_detected,
    test_no_textual_conflict_when_disjoint,
    test_check_conflicts_partner_branch_not_pushed,
    test_cas_peer_entry_survives_retry,
    test_cas_colliding_peer_blocks_on_retry,
    test_gh_missing_friendly_error,
    test_provision_creates_seeds_and_invites,
    test_provision_partner_from_env,
    test_provision_skips_when_remote_already_configured,
    test_provision_reports_invite_failure,
    test_add_collaborator,
    test_add_collaborator_no_remote,
    test_block_on_directory_containment,
    test_block_on_glob,
    test_path_normalization_overlap,
    test_disjoint_directories_are_clean,
    test_release_frees_the_file,
    test_survey_flags_stale_claim,
    test_duplicate_agent_id_warns,
    test_add_multiple_collaborators,
    test_provision_invites_multiple_partners,
    test_history_timeline,
    test_done_captures_changed_files,
    test_finish_opens_pr_and_marks_done,
    test_finish_returns_existing_pr_url,
    test_finish_requires_pushed_branch,
    test_history_survives_smart_quote_in_subject,
]


def main():
    failures = 0
    for t in TESTS:
        name = t.__name__
        try:
            t()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001 - test runner
            failures += 1
            traceback.print_exc()
            print(f"FAIL  {name}: {e}")
    print()
    if failures:
        print(f"{failures}/{len(TESTS)} FAILED")
        sys.exit(1)
    print(f"ALL {len(TESTS)} TESTS PASS")


if __name__ == "__main__":
    main()
