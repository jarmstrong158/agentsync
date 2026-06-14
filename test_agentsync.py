#!/usr/bin/env python3
"""
Integration test for the agentsync MCP server.

Spins up a real bare origin + two clones (jonny, partner) in a temp dir and
drives the full protocol through the actual tool functions, flipping the
AGENTSYNC_* env between "agents". No mocks for git — these are real pushes.

Run:  python3 test_agentsync.py
(Requires git on PATH and `pip install mcp`.)
"""

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))


def load_server():
    spec = importlib.util.spec_from_file_location(
        "agentsync_server", os.path.join(HERE, "agentsync_server.py")
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


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


def test_provision(m):
    """Drive provision() end-to-end with the gh CLI stubbed out: a local bare
    repo stands in for GitHub, so every git operation is real."""
    root = tempfile.mkdtemp(prefix="agentsync_prov_")
    try:
        remotes = os.path.join(root, "remotes")
        os.makedirs(remotes)
        repo_path = os.path.join(root, "fresh-project")  # does NOT exist yet

        def bare_for(slug):
            return os.path.join(remotes, slug.replace("/", "__") + ".git")

        # stub the four gh touch-points
        m._gh_login = lambda: "tester"
        m._gh_repo_exists = lambda slug: os.path.isdir(bare_for(slug))

        def fake_gh(args, cwd=None, check=True):
            if args[:2] == ["repo", "create"]:
                slug = args[2]
                bare = bare_for(slug)
                git(["init", "-q", "--bare", "-b", "main", bare], root)
                git(["remote", "add", "origin", bare], cwd)
                git(["push", "-q", "-u", "origin", "main"], cwd)
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if args[:3] == ["api", "-X", "PUT"]:  # add collaborator
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected gh call: {args}")

        m._gh = fake_gh

        os.environ["AGENTSYNC_REPO"] = repo_path
        os.environ["AGENTSYNC_AGENT_ID"] = "tester"
        os.environ.pop("AGENTSYNC_PARTNER_GITHUB", None)

        r = json.loads(m.provision(repo="tester/fresh-project",
                                   partner_github="buddy"))
        assert r["status"] == "provisioned", r
        assert r["repo"] == "tester/fresh-project", r
        assert r["partner_invited"] is True, r
        # the coordination branch + claims.json landed on the "remote"
        bare = bare_for("tester/fresh-project")
        ls = git(["ls-tree", "-r", "--name-only", "agentsync"], bare).stdout
        assert "claims.json" in ls, ls
        # idempotent: a second call still succeeds and changes nothing structural
        r2 = json.loads(m.provision(repo="tester/fresh-project"))
        assert r2["status"] == "provisioned", r2
        # the survey protocol now works on the provisioned repo
        assert json.loads(m.survey())["partners"] == {}
        print("PROVISION PASS")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    m = load_server()
    root = tempfile.mkdtemp(prefix="agentsync_test_")
    try:
        origin, clones = setup_lab(root)

        def be(who):
            os.environ["AGENTSYNC_REPO"] = clones[who]
            os.environ["AGENTSYNC_AGENT_ID"] = who

        # 1. jonny surveys (auto-creates the agentsync branch), sees nobody
        be("jonny")
        assert json.loads(m.survey())["partners"] == {}

        # 2. jonny claims auth
        r = json.loads(m.claim("auth", ["auth.py"], requires=["db.sql"],
                               branch="jonny/auth"))
        assert r["status"] == "claimed", r

        # 3. partner sees jonny's claim
        be("partner")
        assert "jonny" in json.loads(m.survey())["partners"]

        # 4. partner claiming the same file is blocked
        r = json.loads(m.claim("auth2", ["auth.py"], branch="partner/auth"))
        assert r["status"] == "blocked", r
        assert r["conflicts"]["jonny"]["reasons"][0]["files"] == ["auth.py"]

        # 5. dependency-on-WIP is also blocked
        r = json.loads(m.claim("ui", ["ui.py"], requires=["auth.py"],
                               branch="partner/ui"))
        assert r["status"] == "blocked", r
        assert r["conflicts"]["jonny"]["reasons"][0]["type"] == "depends_on_their_wip"

        # 6. a clean, non-overlapping claim succeeds
        r = json.loads(m.claim("ui", ["ui.py"], branch="partner/ui"))
        assert r["status"] == "claimed", r

        # 7. textual conflict detection: both branches edit the same README line
        for who, line in (("jonny", "jonny/auth"), ("partner", "partner/ui")):
            p = clones[who]
            git(["checkout", "-qb", line], p)
            with open(os.path.join(p, "README.md"), "w") as f:
                f.write(f"# project\n{who.upper()}\n")
            git(["commit", "-qam", "edit"], p)
            git(["push", "-q", "origin", line], p)
            git(["checkout", "-q", "main"], p)
        be("jonny")
        res = json.loads(m.check_conflicts())["results"][0]
        assert res["merge_conflict"]["conflict"] is True, res
        assert res["merge_conflict"]["files"] == ["README.md"], res

        # 8. status update lands and is visible to the partner
        assert json.loads(m.update_status("done", note="auth finished"))["status"] == "updated"
        be("partner")
        assert json.loads(m.survey())  # jonny no longer an active blocker

        print("ALL PASS")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    test_provision(m)


if __name__ == "__main__":
    main()
