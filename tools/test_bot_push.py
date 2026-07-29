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
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from bot_push import (  # noqa: E402
    path_allowed, load_snapshot, job_policy, run, POLICY_DIRECT, POLICY_BRANCH,
    build_gate, usable_gate_pythons, DEFAULT_GATE_PYTHONS, GATE_PYTEST_ARGS,
    GATE_PYTEST_ARGS_GATEWAY, GATEWAY_DEPS, GateCoverageError, gate_coverage,
    gateway_capable,
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


# ─── 8. Interpreter pinning (2026-07-29) ─────────────────────────────────────
# The wrapper decides which interpreter validates every machine commit. A bare
# `python3` made that decision depend on the caller's PATH, so these pin it
# structurally rather than trusting the comment above it.
_BOT_PUSH_SH = Path(__file__).parent / "bot_push.sh"


def test_wrapper_pins_the_interpreter_rather_than_resolving_it():
    """bot_push.sh must never exec a bare `python`/`python3`.

    Cron runs with PATH=/sbin:/bin:/usr/sbin:/usr/bin (no /usr/local/bin), so a
    bare `python3` is /usr/bin/python3 = 3.9.25 under cron but
    /usr/local/bin/python3 = 3.11.13 in an interactive root shell. Identical
    command, two interpreters — which made the gate's effective interpreter a
    function of who invoked it, and impossible to reproduce by hand.
    """
    text = _BOT_PUSH_SH.read_text(encoding="utf-8")
    exec_lines = [ln.strip() for ln in text.splitlines()
                  if ln.strip().startswith("exec ")]
    assert exec_lines, "bot_push.sh no longer execs anything — wrapper changed"
    for ln in exec_lines:
        assert not re.search(r"^exec\s+python3?\b", ln), (
            f"bare interpreter resolved via PATH: {ln!r}. Pin an absolute path "
            f"(see BOT_PUSH_PYTHON) — cron and interactive PATHs differ."
        )


def test_wrapper_default_interpreter_is_absolute():
    """The pinned default must be a path, not a name to be looked up."""
    text = _BOT_PUSH_SH.read_text(encoding="utf-8")
    m = re.search(r'BOT_PUSH_PYTHON="\$\{BOT_PUSH_PYTHON:-([^}"]+)\}"', text)
    assert m, "BOT_PUSH_PYTHON default not found in bot_push.sh"
    assert m.group(1).startswith("/"), (
        f"default interpreter {m.group(1)!r} is not an absolute path"
    )


# ─── 9. Multi-interpreter gate (2026-07-29) ──────────────────────────────────
# tools/ runs on 3.9.25 under cron and on the venv 3.11.13 via build_site, so
# a single-interpreter gate proves half of what it appears to.
def test_every_gate_candidate_is_an_absolute_path():
    """A bare name would reintroduce the PATH ambiguity this change removed."""
    for c in DEFAULT_GATE_PYTHONS:
        assert c.startswith("/"), f"gate candidate {c!r} is not absolute"


def test_the_venv_is_never_a_gate_candidate():
    """The venv has no pytest — naming it fails the gate closed on EVERY commit.

    This is the specific mistake the 2026-07-29 pre-flight caught before it
    shipped. /usr/local/bin/python3 is the same 3.11.13 and is used instead.
    """
    assert not any("venv" in c for c in DEFAULT_GATE_PYTHONS), (
        "a venv interpreter is a gate candidate; verify it has pytest first"
    )


def test_all_usable_interpreters_must_pass(tmp_path):
    """Multiple usable candidates chain with && — every one must pass."""
    fake_a, fake_b = tmp_path / "pa", tmp_path / "pb"
    for f in (fake_a, fake_b):
        f.write_text("#!/bin/sh\nexit 0\n")
        f.chmod(0o755)
    cmd, skipped = build_gate((str(fake_a), str(fake_b)))
    assert skipped == []
    assert " && " in cmd
    # Suite-agnostic: as of 2026-07-29 each interpreter runs the widest suite
    # it can, so the exact args depend on whether it imports the gateway deps.
    # What this test is about is that BOTH are chained and both must pass.
    assert cmd.count("-m pytest") == 2


def test_an_absent_candidate_is_reported_not_silently_dropped(tmp_path):
    """A gate that quietly halves itself is the failure class under repair."""
    real = tmp_path / "preal"
    real.write_text("#!/bin/sh\nexit 0\n")
    real.chmod(0o755)
    missing = str(tmp_path / "nope")
    cmd, skipped = build_gate((str(real), missing))
    assert (missing, "absent") in skipped
    assert missing not in cmd


def test_a_candidate_without_pytest_is_reported(tmp_path):
    """`import pytest` failing must skip loudly, not pass silently."""
    nopytest = tmp_path / "pnp"
    nopytest.write_text("#!/bin/sh\nexit 1\n")
    nopytest.chmod(0o755)
    _, skipped = build_gate((str(nopytest),))
    assert skipped == [(str(nopytest), "cannot import pytest")]


def test_no_usable_candidate_falls_back_to_the_running_interpreter(tmp_path):
    """Sandbox/CI: the absolute VPS paths do not exist. Degrade, don't crash."""
    cmd, skipped = build_gate((str(tmp_path / "a"), str(tmp_path / "b")))
    assert sys.executable in cmd
    assert len(skipped) == 2


def test_usable_gate_pythons_agrees_with_build_gate(tmp_path):
    good = tmp_path / "g"
    good.write_text("#!/bin/sh\nexit 0\n")
    good.chmod(0o755)
    usable, skipped = usable_gate_pythons((str(good), str(tmp_path / "x")))
    assert usable == [str(good)]
    assert skipped == [(str(tmp_path / "x"), "absent")]


# ── gateway coverage in the gate (2026-07-29) ────────────────────────

def _fake_python(path, *, pytest_ok=True, gateway_ok=True):
    """A stub interpreter that answers the two `-c` import probes.

    Distinguishes them by inspecting the code string, so one stub can be
    pytest-capable and gateway-incapable — which is the exact box condition
    this feature exists for and cannot be reproduced with exit-0 stubs.
    """
    path.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        f'  *flask*) exit {0 if gateway_ok else 1} ;;\n'
        f'  *pytest*) exit {0 if pytest_ok else 1} ;;\n'
        "esac\n"
        "exit 0\n")
    path.chmod(0o755)
    return str(path)


def test_an_interpreter_without_the_gateway_deps_still_gates_tools(tmp_path):
    """The box condition. Losing tools/ coverage because gateway/ cannot run
    would trade a real gate for a missing one."""
    narrow = _fake_python(tmp_path / "narrow", gateway_ok=False)
    cmd, skipped = build_gate((narrow,))
    assert skipped == []
    assert GATE_PYTEST_ARGS in cmd
    assert "gateway/" not in cmd


def test_a_capable_interpreter_gates_gateway_too(tmp_path):
    capable = _fake_python(tmp_path / "capable")
    cmd, _ = build_gate((capable,))
    assert "gateway/" in cmd


def test_mixed_interpreters_each_run_the_widest_suite_they_can(tmp_path):
    """Neither interpreter is held back to the other's capability, and neither
    is asked to run something it cannot import."""
    narrow = _fake_python(tmp_path / "n", gateway_ok=False)
    capable = _fake_python(tmp_path / "c")
    cmd, _ = build_gate((narrow, capable))
    narrow_part, capable_part = cmd.split(" && ")
    assert "gateway/" not in narrow_part
    assert "gateway/" in capable_part


def test_narrow_coverage_is_reported_not_silent(tmp_path):
    """A gate that quietly halves itself is the failure class this whole file
    was hardened against; a gate that quietly gates LESS than it claims is the
    same defect one level down."""
    narrow = _fake_python(tmp_path / "n", gateway_ok=False)
    capable = _fake_python(tmp_path / "c")
    usable, skipped, reported = gate_coverage((narrow, capable))
    assert usable == [narrow, capable]
    assert reported == [narrow], "narrow interpreter was not named"


def test_require_gateway_fails_closed_rather_than_narrowing(tmp_path):
    """The ratchet. Off by default because tightening it on an unverified box
    would take /admin/publish down; explicit once the deps are confirmed."""
    narrow = _fake_python(tmp_path / "n", gateway_ok=False)
    with pytest.raises(GateCoverageError) as exc:
        build_gate((narrow,), require_gateway=True)
    assert "flask" in str(exc.value)


def test_require_gateway_passes_when_every_interpreter_is_capable(tmp_path):
    capable = _fake_python(tmp_path / "c")
    cmd, _ = build_gate((capable,), require_gateway=True)
    assert "gateway/" in cmd


def test_a_pytest_less_interpreter_is_still_skipped_not_narrowed(tmp_path):
    """Narrowing and skipping are different verdicts; no pytest means the
    interpreter gates nothing at all, which must not read as 'gates tools/'."""
    nopytest = _fake_python(tmp_path / "np", pytest_ok=False)
    usable, skipped, narrow = gate_coverage((nopytest,))
    assert usable == [] and narrow == []
    assert skipped == [(nopytest, "cannot import pytest")]


def test_the_gateway_dep_list_matches_what_gateway_actually_imports():
    """Pinned against the source, so a new gateway dependency cannot silently
    make the probe optimistic — it would report capable, then the suite would
    fail on an import the probe never checked."""
    import re
    root = Path(__file__).resolve().parent.parent / "gateway"
    imported = set()
    for f in root.glob("*.py"):
        for m in re.finditer(r"^\s*(?:import|from)\s+(flask[a-z_]*)",
                             f.read_text(), re.M):
            imported.add(m.group(1))
    assert imported, "read no flask imports — this assertion would be vacuous"
    assert imported <= set(GATEWAY_DEPS), (
        f"gateway/ imports {sorted(imported - set(GATEWAY_DEPS))}, which the "
        f"capability probe does not check")
