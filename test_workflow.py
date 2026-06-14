#!/usr/bin/env python3
"""
Workflow / integration tests for agentsync — the parts that prove the tool holds
up under a realistic "two people on one repo" session, plus the real MCP stdio
transport (which the unit suite in test_agentsync.py never exercises).

Three layers of coverage here:

  1. Full two-person lifecycle: survey -> claim disjoint lanes -> build on
     branches -> check_conflicts -> finish (update_status done) -> a follow-up
     that depends on the now-finished work. The whole collaboration arc, in one
     test, the way COLLAB.md describes it.

  2. The shared-hotspot reality: a collision is refused, narrowing unblocks, and
     force=True lets two agents share a file when their regions are disjoint.

  3. Real MCP stdio transport: the server is launched as an actual subprocess and
     driven over JSON-RPC, exactly as an MCP client (the app) does — and every
     git subprocess is asserted to use stdin=DEVNULL. This is the regression
     guard for the inherited-stdin hang: the unit tests call the functions
     in-process and would never catch it.

Reuses the real-git harness (bare origin + two clones) from test_agentsync.

Run:  python3 test_workflow.py
"""

import asyncio
import json
import os
import subprocess
import sys
import traceback

# Reuse the proven harness: M is the loaded server module, lab()/be()/git() set
# up a bare origin with "jonny" and "partner" clones and switch the active agent.
from test_agentsync import HERE, M, lab, be, git


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def work(clone, branch, fname, content, base="main"):
    """Do a real piece of work on `branch`: create it off `base`, write a file,
    commit, push, and return to main — i.e. what an agent's code branch does."""
    git(["checkout", "-qB", branch, base], clone)
    path = os.path.join(clone, fname)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    git(["add", "-A"], clone)
    git(["commit", "-qm", f"work on {fname}"], clone)
    git(["push", "-q", "origin", branch], clone)
    git(["checkout", "-q", "main"], clone)


def _unwrap(text):
    """A tool returns a JSON string; parse it (unwrapping a {'result': '...'}
    envelope if a client added one)."""
    d = json.loads(text)
    if isinstance(d, dict) and set(d) == {"result"} and isinstance(d["result"], str):
        d = json.loads(d["result"])
    return d


def _server_env(repo, agent_id):
    env = os.environ.copy()
    env["AGENTSYNC_REPO"] = repo
    env["AGENTSYNC_AGENT_ID"] = agent_id
    env.pop("AGENTSYNC_PARTNER_GITHUB", None)
    return env


def mcp_drive(server_env, calls, timeout=60):
    """Launch the server as a real MCP stdio subprocess and make the given tool
    calls over JSON-RPC, returning each result's text. Bounded by `timeout` so a
    regression of the inherited-stdin hang fails loudly instead of wedging CI."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(HERE, "agentsync_server.py")],
        env=server_env,
    )

    async def run():
        results = []
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for name, args in calls:
                    res = await session.call_tool(name, arguments=args)
                    results.append(res.content[0].text if res.content else "")
        return results

    async def bounded():
        return await asyncio.wait_for(run(), timeout=timeout)

    return asyncio.run(bounded())


# --------------------------------------------------------------------------- #
# 1. the full two-person lifecycle
# --------------------------------------------------------------------------- #
def test_two_person_full_lifecycle():
    """Two collaborators take a project from empty board to merged work, in the
    disjoint-lane style COLLAB.md prescribes — and the board reflects every
    step along the way."""
    LANE_A = ["public/js/ui.js", "public/css/style.css"]       # client/UX lane
    LANE_B = ["public/js/world.js", "public/shared/content.js"]  # world/systems lane

    with lab() as (root, origin, clones):
        # 1. jonny opens an empty board and claims Lane A.
        be(clones, "jonny")
        assert json.loads(M.survey())["partners"] == {}, "board should start empty"
        r = json.loads(M.claim("QoL pass", LANE_A, branch="jonny/qol"))
        assert r["status"] == "claimed", r

        # 2. partner surveys, sees jonny, claims the disjoint Lane B -> no block.
        be(clones, "partner")
        partners = json.loads(M.survey())["partners"]
        assert partners["jonny"]["task"] == "QoL pass", partners
        r = json.loads(M.claim("Systems pass", LANE_B, branch="partner/systems"))
        assert r["status"] == "claimed", r

        # 3. both actually build on their branches and push.
        work(clones["jonny"], "jonny/qol", "public/js/ui.js", "// hud polish\n")
        work(clones["partner"], "partner/systems", "public/js/world.js", "// new biome\n")

        # 4. partner checks conflicts against jonny -> disjoint, clean merge.
        be(clones, "partner")
        results = json.loads(M.check_conflicts())["results"]
        assert len(results) == 1, results
        assert results[0]["claim_overlap"] == [], results
        assert results[0]["merge_conflict"]["conflict"] is False, results

        # 5. jonny finishes and marks the work done.
        be(clones, "jonny")
        assert json.loads(M.update_status("done", note="QoL merged"))["status"] == "updated"

        # 6. partner sees jonny is done, and a follow-up that *depends on* a file
        #    jonny just finished is NOT blocked (a done claim is not WIP).
        be(clones, "partner")
        partners = json.loads(M.survey())["partners"]
        assert partners["jonny"]["status"] == "done", partners
        r = json.loads(M.claim("Wire HUD to new UI", ["public/js/hud.js"],
                               requires=["public/js/ui.js"], branch="partner/hud"))
        assert r["status"] == "claimed", r


# --------------------------------------------------------------------------- #
# 2. shared-hotspot reality: collide, narrow, or force
# --------------------------------------------------------------------------- #
def test_collision_caught_then_narrowing_unblocks():
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("rewire main", ["public/js/main.js"], branch="jonny/main")

        be(clones, "partner")
        # partner reaches for the same shared hotspot -> refused, with the reason.
        r = json.loads(M.claim("also main", ["public/js/main.js"], branch="partner/main"))
        assert r["status"] == "blocked", r
        assert r["conflicts"]["jonny"]["reasons"][0]["type"] == "shared_files", r
        assert r["conflicts"]["jonny"]["reasons"][0]["files"] == ["public/js/main.js"], r

        # narrowing to a file partner actually owns clears the block.
        r = json.loads(M.claim("entities tweak", ["public/js/entities.js"],
                               branch="partner/ent"))
        assert r["status"] == "claimed", r


def test_force_lets_two_share_a_file_for_disjoint_regions():
    """COLLAB.md's guidance: a shared file (e.g. constants.js) is fine for two
    agents when their edits are disjoint regions — force=True records both."""
    with lab() as (root, origin, clones):
        be(clones, "jonny")
        M.claim("constants: add MSG ids", ["public/shared/constants.js"],
                branch="jonny/const")

        be(clones, "partner")
        r = json.loads(M.claim("constants: add tuning", ["public/shared/constants.js"],
                               branch="partner/const", force=True))
        assert r["status"] == "claimed", r

        # both claims coexist on the board.
        be(clones, "jonny")
        partners = json.loads(M.survey())["partners"]
        assert "partner" in partners, partners
        assert partners["partner"]["task"] == "constants: add tuning", partners


# --------------------------------------------------------------------------- #
# 3. real MCP stdio transport (regression guard for the inherited-stdin hang)
# --------------------------------------------------------------------------- #
def test_mcp_transport_two_agents_survey_and_claim():
    """Drive the server the way the app does — as a subprocess over MCP stdio —
    for two agents. Proves survey()/claim() round-trip over the real transport
    and, via the timeout in mcp_drive(), that no call hangs."""
    with lab() as (root, origin, clones):
        # agent 1 over real stdio: empty survey, then claim. Must return, not hang.
        out = mcp_drive(_server_env(clones["jonny"], "jonny"), [
            ("survey", {}),
            ("claim", {"task": "QoL", "touches": ["public/js/ui.js"],
                       "branch": "jonny/qol"}),
        ])
        survey0, claim0 = _unwrap(out[0]), _unwrap(out[1])
        assert survey0["partners"] == {}, survey0
        assert claim0["status"] == "claimed", claim0

        # agent 2 over real stdio: survey sees agent 1's claim landed.
        out2 = mcp_drive(_server_env(clones["partner"], "partner"), [("survey", {})])
        survey1 = _unwrap(out2[0])
        assert "jonny" in survey1["partners"], survey1
        assert survey1["partners"]["jonny"]["task"] == "QoL", survey1


def test_git_calls_never_inherit_stdin():
    """The actual fix, pinned: every git subprocess must pass stdin=DEVNULL, so a
    credential prompt can't block forever on the inherited MCP transport pipe.
    Spy on subprocess.run during a real survey and assert it on every call."""
    captured = []
    real_run = M.subprocess.run

    def spy(*args, **kwargs):
        captured.append(kwargs.get("stdin", "INHERITED"))
        return real_run(*args, **kwargs)

    with lab() as (root, origin, clones):
        be(clones, "jonny")
        # Install the spy only around the server call, so it captures the
        # server's git invocations (via _git) and not the harness's own setup.
        M.subprocess.run = spy
        try:
            M.survey()  # creates the branch -> fetch/ls-remote/worktree/commit/push
        finally:
            M.subprocess.run = real_run

    assert captured, "expected at least one git subprocess call"
    bad = [s for s in captured if s is not M.subprocess.DEVNULL]
    assert not bad, f"{len(bad)} git call(s) did not set stdin=DEVNULL: {bad}"


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
TESTS = [
    test_two_person_full_lifecycle,
    test_collision_caught_then_narrowing_unblocks,
    test_force_lets_two_share_a_file_for_disjoint_regions,
    test_mcp_transport_two_agents_survey_and_claim,
    test_git_calls_never_inherit_stdin,
]


def main():
    failures = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001 - test runner
            failures += 1
            traceback.print_exc()
            print(f"FAIL  {t.__name__}: {e}")
    print()
    if failures:
        print(f"{failures}/{len(TESTS)} FAILED")
        sys.exit(1)
    print(f"ALL {len(TESTS)} TESTS PASS")


if __name__ == "__main__":
    main()
