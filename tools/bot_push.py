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
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BOT_NAME = "askmaddi-bot"
BOT_EMAIL = "bot@askmaddi.com"
DEFAULT_GATE = "python -m pytest tools/ -q"
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


def main():
    ap = argparse.ArgumentParser(description="Machine-commit door for askmaddi-prod.")
    ap.add_argument("--job", required=True, help="Job name; must hold a policy in the snapshot.")
    ap.add_argument("--snapshot", required=True, help="Frozen Crucible writeback snapshot JSON.")
    ap.add_argument("--summary", default="automated data refresh",
                    help="One-line commit summary.")
    ap.add_argument("--repo", default=".", help="Repo path (default: cwd).")
    ap.add_argument("--gate", default=DEFAULT_GATE, help="Validation command; non-zero blocks the push.")
    ap.add_argument("--signal-dir", default="~/.askmaddi-bot/signals",
                    help="Where abort/proposal signals are written.")
    ap.add_argument("--dry-run", action="store_true", help="Fence + gate only; no commit, no push.")
    args = ap.parse_args()
    return run(args.repo, args.job, args.snapshot, args.summary,
               args.gate, args.signal_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
