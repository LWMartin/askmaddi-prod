#!/usr/bin/env python3
"""
bot_push.py — the only door for machine commits to askmaddi-prod.

Spec: phantom-ops claude/workspace/specs/maddi-writeback-architecture.md

Every automated job (used-price cron, herald proposals, Stage 6 ingestion)
routes its commits through this wrapper. Raw `git push` from automation is a
protocol violation. The wrapper:

  1. Loads a frozen Crucible spawn snapshot (allowlist + per-job merge policy).
     The snapshot is exported by phantom-ops `crucible.spawn.export_writeback_snapshot`
     — the bot cannot widen its own permissions by editing a reachable config.
  2. Fences dirty paths against the allowlist (direct-to-master policy only;
     branch-and-propose jobs may touch anything — a branch is a proposal).
  3. Exits clean on an empty change set. An unchanged price is not an event;
     no heartbeat commits, ever.
  4. Runs the validation gate (default: the tools/ pytest suite). No green, no push.
  5. Commits as askmaddi-bot with a structured, audit-friendly message.
  6. Pulls --rebase (auto-pull cron shares the box), then pushes:
       direct-to-master   -> origin master
       branch-and-propose -> origin HEAD:bot/<job>/<date>  (never merged by the bot)

Any abort writes a JSON signal file (default ~/.askmaddi-bot/signals/) so
failure is loud — silent failure is the enemy (incident 2026-05-06).

The bot never amends, never force-pushes, never reverts. Rollback is a
human act through the normal airlock.
"""

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BOT_NAME = "askmaddi-bot"
BOT_EMAIL = "bot@askmaddi.com"
# ─── Gate interpreter selection ──────────────────────────────────────────────
# `tools/` code runs on TWO production interpreters, so passing on one proves
# nothing about the other:
#
#   /usr/bin/python3        3.9.25  — cron: card_factory (15-min drip), both
#                                     minting stages, image_catalog_sweep,
#                                     refresh_used_prices
#   /usr/local/bin/python3  3.11.13 — version-match for the venv, which runs
#                                     tools/build_site.py on every publish
#                                     (admin_surface passes sys.executable)
#
# The venv itself is NOT a candidate: it has no pytest, so naming it would fail
# the gate closed on every commit. /usr/local/bin/python3 is the same 3.11.13
# and differs only in site-packages, which tools/ does not need.
#
# Paths are absolute deliberately. Cron's PATH (/sbin:/bin:/usr/sbin:/usr/bin,
# no /usr/local/bin) and an interactive PATH resolve the same bare name to
# different interpreters — the ambiguity this whole change exists to remove.
#
# A candidate that is absent or cannot import pytest is REPORTED, never
# silently dropped: a gate that quietly halves itself is precisely the
# failure class here (R5 — exhaustion is loud).
#
# GATEWAY COVERAGE (2026-07-29). The clause above — "differs only in
# site-packages, which tools/ does not need" — is exactly why gateway/ was
# outside this gate. gateway/ DOES need site-packages: flask, flask_cors and
# flask_limiter, which live in the venv the gateway service runs under. Neither
# gate interpreter IS that venv.
#
# So widening the gate outright would very likely fail both interpreters on
# ModuleNotFoundError, and bot_push is the machine-commit door behind
# /admin/publish — the publish path would go down, reporting an import error
# that names nothing about gates or publishing.
#
# Instead each interpreter is asked what it can actually run. One that imports
# the gateway deps gates tools/ AND gateway/; one that cannot gates tools/ and
# says so, loudly, on stderr. Narrower coverage is REPORTED, never silent —
# the same doctrine as an absent interpreter, for the same reason (R5).
#
# `--require-gateway` flips it to fail-closed once the deps are confirmed
# present on every production interpreter. That is the ratchet: safe by
# default, tightened deliberately, never tightened by assumption.
GATE_BASE_SUITES = "tools/"
GATE_GATEWAY_SUITES = "tools/ gateway/"
# What an interpreter must import to COLLECT AND RUN gateway/ — derived from
# gateway/'s actual unguarded module-level imports, not from requirements.txt
# and not from what the service happens to use. test_gateway_deps_match_imports
# re-derives this from the tree and fails if the two drift.
#
# Corrected 2026-07-30, wrong in BOTH directions since it was written:
#   - `flask_limiter` was listed but is a GUARDED optional import in
#     app_production (try/except ImportError -> HAS_LIMITER), absent from
#     requirements.txt, and referenced by no test. Listing it marked both
#     production interpreters narrow when neither actually needed it.
#   - `requests` was NOT listed despite being a hard module-level import in
#     three gateway/ files, so gateway_capable() could answer True for an
#     interpreter where collection would still fail.
GATEWAY_DEPS = ("flask", "flask_cors", "requests")


def _pytest_args(suites: str) -> str:
    return f"-m pytest {suites} -q"


GATE_PYTEST_ARGS = _pytest_args(GATE_BASE_SUITES)
GATE_PYTEST_ARGS_GATEWAY = _pytest_args(GATE_GATEWAY_SUITES)
DEFAULT_GATE_PYTHONS = ("/usr/bin/python3", "/usr/local/bin/python3")


class GateCoverageError(RuntimeError):
    """Raised under --require-gateway when an interpreter cannot gate gateway/."""


def gateway_capable(python: str) -> bool:
    """Can this interpreter import what gateway/ needs to be tested?

    Probed, never assumed from requirements.txt: the file declares flask and
    flask-cors, and the gateway SERVICE imports them fine, but the service runs
    under the venv and these gate interpreters do not. What a declared
    dependency proves is that something on the box has it, not that this
    interpreter can see it.
    """
    probe = subprocess.run(
        [python, "-c", f"import {', '.join(GATEWAY_DEPS)}"],
        capture_output=True, text=True)
    return probe.returncode == 0


def usable_gate_pythons(candidates=DEFAULT_GATE_PYTHONS):
    """Partition candidates into (usable, [(path, reason_unusable), ...])."""
    usable, skipped = [], []
    for c in candidates:
        if not Path(c).exists():
            skipped.append((c, "absent"))
            continue
        probe = subprocess.run([c, "-c", "import pytest"],
                               capture_output=True, text=True)
        if probe.returncode != 0:
            skipped.append((c, "cannot import pytest"))
            continue
        usable.append(c)
    return usable, skipped


def gate_coverage(candidates=DEFAULT_GATE_PYTHONS):
    """(usable, skipped, narrow) — which interpreters gate what.

    `narrow` lists usable interpreters that CANNOT run gateway/. They still
    gate tools/; the point of naming them separately is that "gateway is
    gated" must never quietly become false.
    """
    usable, skipped = usable_gate_pythons(candidates)
    narrow = [c for c in usable if not gateway_capable(c)]
    return usable, skipped, narrow


def build_gate(candidates=DEFAULT_GATE_PYTHONS, *, require_gateway=False):
    """Compose the gate: EVERY usable production interpreter must pass.

    Returns (command, skipped). Each usable interpreter runs the widest suite
    it can: tools/ + gateway/ where the gateway deps import, tools/ alone
    otherwise.

    Falls back to the running interpreter when no candidate is usable — the
    sandbox/CI case, where these absolute VPS paths do not exist. The fallback
    is a degraded gate, so it is reported too.

    With require_gateway, an interpreter that cannot run gateway/ is fatal
    rather than narrow. Use it once the deps are confirmed on the box; until
    then the default keeps the publish path alive and complains loudly.
    """
    usable, skipped, narrow = gate_coverage(candidates)

    if require_gateway and (narrow or not usable):
        raise GateCoverageError(
            "--require-gateway: cannot gate gateway/ under "
            f"{narrow or 'any interpreter'} — install "
            f"{' '.join(GATEWAY_DEPS)} for it, or drop the flag")

    if not usable:
        args = (GATE_PYTEST_ARGS_GATEWAY if gateway_capable(sys.executable)
                else GATE_PYTEST_ARGS)
        return f"{shlex.quote(sys.executable)} {args}", skipped

    return " && ".join(
        f"{shlex.quote(c)} "
        f"{GATE_PYTEST_ARGS if c in narrow else GATE_PYTEST_ARGS_GATEWAY}"
        for c in usable), skipped
POLICY_DIRECT = "direct-to-master"
POLICY_BRANCH = "branch-and-propose"
VALID_POLICIES = {POLICY_DIRECT, POLICY_BRANCH}


# ─── Allowlist matching ───────────────────────────────────────────────────────
# Deliberate semantics (tighter than fnmatch, which lets `*` cross `/`):
#   *   matches within a single path segment only
#   **  matches across segments (any depth)
# So `data/cards/*.json` does NOT cover `data/cards/sub/x.json`, while
# `browser/**` covers the whole subtree. A fence that silently over-matches
# is a widened allowlist nobody confirmed.
def _pattern_to_regex(pattern):
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def path_allowed(path, allowlist):
    return any(_pattern_to_regex(p).match(path) for p in allowlist)


# ─── Snapshot ─────────────────────────────────────────────────────────────────
def load_snapshot(path):
    snap = json.loads(Path(path).read_text(encoding="utf-8"))
    for field in ("role", "allowlist", "policies", "crucible_hash"):
        if field not in snap:
            raise ValueError(f"snapshot missing required field: {field}")
    if snap["role"] != "writeback":
        raise ValueError(f"snapshot role is {snap['role']!r}, expected 'writeback'")
    return snap


def job_policy(snap, job):
    policy = (snap.get("policies") or {}).get(job)
    if policy not in VALID_POLICIES:
        raise ValueError(
            f"job {job!r} has no valid policy in snapshot "
            f"(got {policy!r}); a job without a confirmed Crucible policy may not push"
        )
    return policy


# ─── Git plumbing ─────────────────────────────────────────────────────────────
def git(repo, *args, check=True):
    res = subprocess.run(["git", "-C", str(repo), *args],
                         capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{res.stderr.strip()}")
    return res.stdout


def dirty_paths(repo):
    """Modified + untracked paths, the candidate change set. `-uall` lists
    untracked files individually (porcelain default collapses new directories
    to `dir/`, which would blur the fence and let `git add` sweep a directory)."""
    out = git(repo, "status", "--porcelain", "-uall")
    paths = []
    for line in out.splitlines():
        if not line.strip():
            continue
        p = line[3:].strip()
        if " -> " in p:  # rename
            p = p.split(" -> ", 1)[1]
        paths.append(p.strip('"'))
    return paths


# ─── Signals ──────────────────────────────────────────────────────────────────
def write_signal(signal_dir, job, stage, reason, detail=None):
    d = Path(signal_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {"ts": ts, "tool": "bot_push", "job": job,
               "stage": stage, "reason": reason}
    if detail:
        payload["detail"] = str(detail)[:2000]
    path = d / f"bot_push-{job}-{int(time.time())}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"  signal -> {path}", file=sys.stderr)
    return path


# ─── Commit message ───────────────────────────────────────────────────────────
def commit_message(job, summary, policy, snap, gate_result):
    return (
        f"[bot:{job}] {summary}\n"
        f"\n"
        f"Job: {job}\n"
        f"Policy: {policy}\n"
        f"Snapshot-Hash: {snap['crucible_hash']}\n"
        f"Gates: {gate_result}\n"
    )


# ─── Main flow ────────────────────────────────────────────────────────────────
def run(repo, job, snapshot_path, summary, gate_cmd, signal_dir, dry_run=False):
    repo = Path(repo)

    # 0. Snapshot + policy — a job the Crucible doesn't know may not push.
    try:
        snap = load_snapshot(snapshot_path)
        policy = job_policy(snap, job)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        write_signal(signal_dir, job, "snapshot", "snapshot/policy load failed", e)
        return 2

    # 1. Change set. Empty -> clean no-op exit, no commit.
    changes = dirty_paths(repo)
    if not changes:
        print(f"[bot:{job}] no changes — nothing to push (clean exit)")
        return 0

    # 2. Allowlist fence (direct policy only; a branch is a proposal).
    if policy == POLICY_DIRECT:
        violations = [p for p in changes if not path_allowed(p, snap["allowlist"])]
        if violations:
            write_signal(signal_dir, job, "allowlist",
                         "paths outside locked allowlist",
                         ", ".join(violations[:20]))
            print(f"[bot:{job}] BLOCKED — {len(violations)} path(s) outside allowlist:",
                  file=sys.stderr)
            for v in violations[:20]:
                print(f"    {v}", file=sys.stderr)
            return 2

    # 3. Validation gate. No green, no push. Tree left dirty for inspection.
    print(f"[bot:{job}] gate: {gate_cmd}")
    gate = subprocess.run(gate_cmd, shell=True, cwd=repo,
                          capture_output=True, text=True)
    if gate.returncode != 0:
        write_signal(signal_dir, job, "gate", "validation gate failed",
                     (gate.stdout + gate.stderr)[-2000:])
        print(f"[bot:{job}] BLOCKED — gate failed (exit {gate.returncode})",
              file=sys.stderr)
        return 2
    gate_result = f"pass ({gate_cmd})"

    if dry_run:
        print(f"[bot:{job}] DRY RUN — would commit {len(changes)} path(s) "
              f"under policy {policy}")
        return 0

    # 4. Stage + commit as the bot identity.
    git(repo, "add", "--", *changes)
    msg = commit_message(job, summary, policy, snap, gate_result)
    git(repo, "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}",
        "commit", "-m", msg)

    # 5. Rebase onto fresh origin (auto-pull cron shares the box), then push.
    #    Rebase is for the bot's own unpushed commit only — published history
    #    is never rewritten.
    try:
        git(repo, "pull", "--rebase", "origin", "master")
    except RuntimeError as e:
        git(repo, "rebase", "--abort", check=False)
        write_signal(signal_dir, job, "rebase",
                     "rebase onto origin/master failed; local commit retained", e)
        return 2

    if policy == POLICY_DIRECT:
        target = "master"
        git(repo, "push", "origin", "master")
    else:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = f"bot/{job}/{date}"
        try:
            git(repo, "push", "origin", f"HEAD:refs/heads/{target}")
        except RuntimeError:
            target = f"bot/{job}/{date}-{datetime.now(timezone.utc).strftime('%H%M%S')}"
            git(repo, "push", "origin", f"HEAD:refs/heads/{target}")
        # Proposal notice — never merged by the bot.
        write_signal(signal_dir, job, "proposal",
                     f"branch pushed for review: {target}")

    print(f"[bot:{job}] pushed -> {target} ({len(changes)} path(s), policy {policy})")
    return 0


# ─── Preflight classification (fence-only) ────────────────────────────────────
def classify_dirt(repo, snapshot_path, signal_dir=None, job="preflight"):
    """Classify the working tree's dirt against the snapshot allowlist,
    WITHOUT committing or pushing. The nightly's preflight question is not
    'is the tree dirty' but 'is the dirt within the bot's writ' — asked
    against the SAME frozen snapshot the stage-3 bot_push fences with, so
    preflight and fence can never disagree (one source of truth).

    Exit-code contract (consumed by nightly_used_prices.sh):
        0  tree clean — nothing to bank
        3  dirty, but every path is allowlisted — pipeline-owned writes,
           safe to proceed and bank through the door
        2  foreign dirt present — abort posture, paths printed + signaled
           (committing unknown changes under the bot identity would launder
           them; recorded failure mode 2026-05-06)
    """
    try:
        snap = load_snapshot(snapshot_path)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        if signal_dir:
            write_signal(signal_dir, job, "snapshot", "snapshot load failed", e)
        print(f"[fence-only] snapshot load failed: {e}", file=sys.stderr)
        return 2

    changes = dirty_paths(repo)
    if not changes:
        print("[fence-only] tree clean")
        return 0

    foreign = [p for p in changes if not path_allowed(p, snap["allowlist"])]
    owned = [p for p in changes if path_allowed(p, snap["allowlist"])]
    if foreign:
        if signal_dir:
            write_signal(signal_dir, job, "preflight",
                         "foreign dirt outside locked allowlist",
                         ", ".join(foreign[:20]))
        print(f"[fence-only] FOREIGN dirt ({len(foreign)} path(s)):", file=sys.stderr)
        for p in foreign[:20]:
            print(f"    {p}", file=sys.stderr)
        return 2

    print(f"[fence-only] pipeline-owned dirt only ({len(owned)} path(s)):")
    for p in owned[:20]:
        print(f"    {p}")
    return 3


def main():
    ap = argparse.ArgumentParser(description="Machine-commit door for askmaddi-prod.")
    ap.add_argument("--job", required=True, help="Job name; must hold a policy in the snapshot.")
    ap.add_argument("--snapshot", required=True, help="Frozen Crucible writeback snapshot JSON.")
    ap.add_argument("--summary", default="automated data refresh",
                    help="One-line commit summary.")
    ap.add_argument("--repo", default=".", help="Repo path (default: cwd).")
    ap.add_argument("--gate", default=None,
                    help="Validation command; non-zero blocks the push. "
                         "Default: pytest tools/ (plus gateway/ where the "
                         "interpreter can import it) under EVERY production "
                         "interpreter (see DEFAULT_GATE_PYTHONS).")
    ap.add_argument("--require-gateway", action="store_true",
                    help="Fail closed if any production interpreter cannot "
                         "gate gateway/. Off by default: tightening this on a "
                         "box whose interpreters lack flask/flask_cors/"
                         "flask_limiter would block every machine commit, and "
                         "bot_push is the door behind /admin/publish.")
    ap.add_argument("--signal-dir", default="~/.askmaddi-bot/signals",
                    help="Where abort/proposal signals are written.")
    ap.add_argument("--dry-run", action="store_true", help="Fence + gate only; no commit, no push.")
    ap.add_argument("--fence-only", action="store_true",
                    help="Classify dirt against the allowlist and exit "
                         "(0 clean / 3 owned / 2 foreign). No gate, no commit.")
    args = ap.parse_args()
    if args.fence_only:
        return classify_dirt(args.repo, args.snapshot,
                             signal_dir=args.signal_dir, job=args.job)
    gate_cmd = args.gate
    if gate_cmd is None:
        try:
            gate_cmd, skipped = build_gate(
                require_gateway=args.require_gateway)
        except GateCoverageError as exc:
            print(f"[bot:{args.job}] BLOCKED — {exc}", file=sys.stderr)
            return 2
        for path, why in skipped:
            print(f"[bot:{args.job}] gate interpreter NOT used: "
                  f"{path} ({why})", file=sys.stderr)
        # Narrower coverage is reported for the same reason an absent
        # interpreter is: a gate that quietly gates LESS than it claims is the
        # same defect as one that quietly halves itself.
        _, _, narrow = gate_coverage()
        for path in narrow:
            print(f"[bot:{args.job}] gate NARROWED to {GATE_BASE_SUITES} for "
                  f"{path} (cannot import {', '.join(GATEWAY_DEPS)}) — "
                  f"gateway/ is NOT gated under this interpreter",
                  file=sys.stderr)
    return run(args.repo, args.job, args.snapshot, args.summary,
               gate_cmd, args.signal_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
