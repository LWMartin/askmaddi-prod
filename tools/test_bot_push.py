"""Tests for bot_push.py — the machine-commit door (maddi-writeback-architecture).

Design rules under test:
  1. Allowlist `*` never crosses `/`; `**` crosses. Over-match = silently
     widened allowlist, so semantics are pinned here.
  2. A job without a confirmed Crucible policy may not push (exit 2).
  3. Empty change set -> exit 0, zero commits. No heartbeat commits, ever.
  4. Gate failure blocks before any commit; tree left dirty for inspection.
  5. direct-to-master: allowlist violation aborts with a signal, nothing
     reaches the remote.
  6. branch-and-propose: pushes bot/<job>/<date>, never master, and may
     touch paths outside the allowlist (a branch is a proposal).
  7. Commit message carries [bot:<job>] + Job/Policy/Snapshot-Hash/Gates
     trailers — the audit trail rides in the history itself.

Run from repo root:  python -m pytest tools/test_bot_push.py -q
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from bot_push import (  # noqa: E402
    path_allowed, load_snapshot, job_policy, run, POLICY_DIRECT, POLICY_BRANCH,
)

ALLOW = ["data/cards/*.json", "browser/**", "cards-manifest.json"]
GATE_PASS = f"{sys.executable} -c pass"
GATE_FAIL = f"{sys.executable} -c \"import sys; sys.exit(1)\""


# ─── 1. Matcher semantics ────────────────────────────────────────────────────
def test_single_star_stays_in_segment():
    assert path_allowed("data/cards/sony.json", ALLOW)
    assert not path_allowed("data/cards/sub/sony.json", ALLOW)  # * must not cross /
    assert not path_allowed("data/cards/sony.json.bak", ALLOW)


def test_double_star_crosses_segments():
    assert path_allowed("browser/robots.txt", ALLOW)
    assert path_allowed("browser/cards/sony-a7iv/index.html", ALLOW)


def test_exact_and_outside():
    assert path_allowed("cards-manifest.json", ALLOW)
    assert not path_allowed("tools/bot_push.py", ALLOW)   # the bot may not edit its own door
    assert not path_allowed("gateway/app.py", ALLOW)
    assert not path_allowed("datax/cards/a.json", ALLOW)  # prefix confusion


# ─── Fixtures: snapshot + real git repo pair (work clone + bare remote) ──────
def _snapshot(tmp_path, policies=None):
    snap = {
        "role": "writeback",
        "spawned_at": "2026-06-10T00:00:00Z",
        "crucible_hash": "abc123",
        "identity": "askmaddi-bot (deploy key, ed25519)",
        "repo": "LWMartin/askmaddi-prod",
        "allowlist": ALLOW,
        "policies": policies or {"cron_used_prices": POLICY_DIRECT,
                                 "herald_photo": POLICY_BRANCH},
    }
    p = tmp_path / "writeback.json"
    p.write_text(json.dumps(snap))
    return p


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo_pair(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "master", str(remote)],
                   capture_output=True, check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(remote), str(work)],
                   capture_output=True, check=True)
    _git(work, "config", "user.name", "seed")
    _git(work, "config", "user.email", "seed@test")
    (work / "data" / "cards").mkdir(parents=True)
    (work / "data" / "cards" / "cam.json").write_text('{"price": 1}')
    (work / "cards-manifest.json").write_text("{}")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "seed")
    _git(work, "push", "origin", "master")
    return work, remote


def _remote_log(remote, ref="master"):
    res = subprocess.run(["git", "-C", str(remote), "log", "--format=%s", ref],
                         capture_output=True, text=True)
    return res.stdout.splitlines() if res.returncode == 0 else []


# ─── 2. Policy fence ─────────────────────────────────────────────────────────
def test_unknown_job_blocked(repo_pair, tmp_path):
    work, remote = repo_pair
    (work / "data" / "cards" / "cam.json").write_text('{"price": 2}')
    rc = run(work, "rogue_job", _snapshot(tmp_path), "x",
             GATE_PASS, tmp_path / "sig")
    assert rc == 2
    assert len(_remote_log(remote)) == 1  # nothing pushed
    assert list((tmp_path / "sig").glob("bot_push-rogue_job-*.json"))


def test_snapshot_wrong_role_blocked(repo_pair, tmp_path):
    work, _ = repo_pair
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"role": "herald", "allowlist": [],
                               "policies": {}, "crucible_hash": "x"}))
    rc = run(work, "cron_used_prices", bad, "x", GATE_PASS, tmp_path / "sig")
    assert rc == 2


# ─── 3. Empty change set ─────────────────────────────────────────────────────
def test_clean_tree_exits_zero_no_commit(repo_pair, tmp_path):
    work, remote = repo_pair
    rc = run(work, "cron_used_prices", _snapshot(tmp_path), "x",
             GATE_PASS, tmp_path / "sig")
    assert rc == 0
    assert len(_remote_log(remote)) == 1  # still just the seed


# ─── 4. Gate failure ─────────────────────────────────────────────────────────
def test_gate_failure_blocks_before_commit(repo_pair, tmp_path):
    work, remote = repo_pair
    (work / "data" / "cards" / "cam.json").write_text('{"price": 3}')
    rc = run(work, "cron_used_prices", _snapshot(tmp_path), "x",
             GATE_FAIL, tmp_path / "sig")
    assert rc == 2
    assert len(_remote_log(remote)) == 1
    assert "cam.json" in _git(work, "status", "--porcelain")  # dirty for inspection
    sig = json.loads(next((tmp_path / "sig").glob("*.json")).read_text())
    assert sig["stage"] == "gate"


# ─── 5. Direct policy: allowlist + happy path ────────────────────────────────
def test_direct_allowlist_violation_blocks(repo_pair, tmp_path):
    work, remote = repo_pair
    (work / "gateway").mkdir()
    (work / "gateway" / "evil.py").write_text("x = 1")
    (work / "data" / "cards" / "cam.json").write_text('{"price": 4}')
    rc = run(work, "cron_used_prices", _snapshot(tmp_path), "x",
             GATE_PASS, tmp_path / "sig")
    assert rc == 2
    assert len(_remote_log(remote)) == 1
    sig = json.loads(next((tmp_path / "sig").glob("*.json")).read_text())
    assert sig["stage"] == "allowlist" and "gateway/evil.py" in sig["detail"]


def test_direct_happy_path_pushes_master_with_trailers(repo_pair, tmp_path):
    work, remote = repo_pair
    (work / "data" / "cards" / "cam.json").write_text('{"price": 5}')
    rc = run(work, "cron_used_prices", _snapshot(tmp_path),
             "nightly used-price refresh", GATE_PASS, tmp_path / "sig")
    assert rc == 0
    subjects = _remote_log(remote)
    assert subjects[0] == "[bot:cron_used_prices] nightly used-price refresh"
    body = subprocess.run(["git", "-C", str(remote), "log", "-1", "--format=%b", "master"],
                          capture_output=True, text=True).stdout
    assert "Policy: direct-to-master" in body
    assert "Snapshot-Hash: abc123" in body
    assert "Gates: pass" in body
    author = subprocess.run(["git", "-C", str(remote), "log", "-1", "--format=%an", "master"],
                            capture_output=True, text=True).stdout.strip()
    assert author == "askmaddi-bot"


def test_direct_rebases_onto_fresh_origin(repo_pair, tmp_path):
    """Auto-pull cron shares the box: a commit lands upstream between the
    job's fetch and its push. The bot must rebase, not fail, not force."""
    work, remote = repo_pair
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(remote), str(other)],
                   capture_output=True, check=True)
    _git(other, "config", "user.name", "lee")
    _git(other, "config", "user.email", "lee@test")
    (other / "cards-manifest.json").write_text('{"v": 2}')
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "human change")
    _git(other, "push", "origin", "master")

    (work / "data" / "cards" / "cam.json").write_text('{"price": 6}')
    rc = run(work, "cron_used_prices", _snapshot(tmp_path), "refresh",
             GATE_PASS, tmp_path / "sig")
    assert rc == 0
    subjects = _remote_log(remote)
    assert subjects[0].startswith("[bot:cron_used_prices]")
    assert "human change" in subjects  # both present, history linear


# ─── 6. Branch policy ────────────────────────────────────────────────────────
def test_branch_policy_pushes_proposal_branch_not_master(repo_pair, tmp_path):
    work, remote = repo_pair
    (work / "proposal.md").write_text("new card idea")  # outside allowlist: allowed on a branch
    rc = run(work, "herald_photo", _snapshot(tmp_path), "card proposal",
             GATE_PASS, tmp_path / "sig")
    assert rc == 0
    assert len(_remote_log(remote, "master")) == 1  # master untouched
    branches = subprocess.run(["git", "-C", str(remote), "branch", "--list", "bot/herald_photo/*"],
                              capture_output=True, text=True).stdout
    assert "bot/herald_photo/" in branches
    sig = json.loads(next((tmp_path / "sig").glob("*.json")).read_text())
    assert sig["stage"] == "proposal"


# ─── 7. Dry run + snapshot validation units ──────────────────────────────────
def test_dry_run_no_commit_no_push(repo_pair, tmp_path):
    work, remote = repo_pair
    (work / "data" / "cards" / "cam.json").write_text('{"price": 7}')
    rc = run(work, "cron_used_prices", _snapshot(tmp_path), "x",
             GATE_PASS, tmp_path / "sig", dry_run=True)
    assert rc == 0
    assert len(_remote_log(remote)) == 1
    assert "cam.json" in _git(work, "status", "--porcelain")


def test_job_policy_rejects_invalid(tmp_path):
    snap = json.loads(_snapshot(tmp_path, policies={"weird": "yolo-push"}).read_text())
    with pytest.raises(ValueError):
        job_policy(snap, "weird")
    with pytest.raises(ValueError):
        job_policy(snap, "absent")


def test_load_snapshot_missing_field(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"role": "writeback"}))
    with pytest.raises(ValueError):
        load_snapshot(p)


# ─── classify_dirt (fence-only preflight) ─────────────────────────────────────
# The nightly's preflight question, asked against the SAME snapshot the
# stage-3 fence uses: 0 clean / 3 pipeline-owned dirt / 2 foreign dirt.

from bot_push import classify_dirt  # noqa: E402


def test_fence_only_clean_tree_zero(repo_pair, tmp_path):
    work, _ = repo_pair
    assert classify_dirt(work, _snapshot(tmp_path)) == 0


def test_fence_only_owned_dirt_three(repo_pair, tmp_path):
    work, _ = repo_pair
    (work / "data" / "cards" / "cam.json").write_text('{"price": 2}')
    (work / "cards-manifest.json").write_text('{"n": 1}')
    assert classify_dirt(work, _snapshot(tmp_path)) == 3


def test_fence_only_foreign_dirt_two_and_signals(repo_pair, tmp_path):
    work, _ = repo_pair
    (work / "data" / "cards" / "cam.json").write_text('{"price": 2}')  # owned
    (work / "rogue.py").write_text("x = 1")                             # foreign
    sig = tmp_path / "signals"
    rc = classify_dirt(work, _snapshot(tmp_path), signal_dir=sig,
                       job="cron_used_prices")
    assert rc == 2
    files = list(sig.glob("bot_push-cron_used_prices-*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["stage"] == "preflight"
    assert "rogue.py" in payload["detail"]


def test_fence_only_untracked_owned_is_owned(repo_pair, tmp_path):
    # A publish strands a NEW card file (untracked) — still within the writ.
    work, _ = repo_pair
    (work / "data" / "cards" / "new-card.json").write_text('{"fresh": true}')
    assert classify_dirt(work, _snapshot(tmp_path)) == 3


def test_fence_only_bad_snapshot_two(repo_pair, tmp_path):
    work, _ = repo_pair
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert classify_dirt(work, bad) == 2
