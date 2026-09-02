#!/usr/bin/env python3
"""spine_guard.py — refuse a skus.json push that would revert /opt's live spine.

THE FAILURE THIS PREVENTS (observed 2026-09-02, spine 231->195 wipe):
  The /opt deploy checkout is BOTH a pull target and the surface the gateway
  grows live — resolve_pass.upsert() writes new SKUs straight into
  /opt/askmaddi-prod/data/skus.json. opt-pull.sh, on any incoming commit that
  touches data/skus.json, DISCARDS /opt's local edits on that exact path and
  fast-forwards. So a clone that pushes a skus.json built on a baseline OLDER
  than /opt's live growth silently reverts every SKU the gateway added since —
  the cards survive in /var/lib but their spine entries vanish and they can no
  longer publish ("no spine entry").

  Banking (bot_push cron_used_prices, allowlist includes data/skus.json) closes
  the window daily, but any clone push inside that window is a live wipe. This
  guard makes the reverting push STRUCTURALLY IMPOSSIBLE: a clone may only push
  a skus.json whose key set is a SUPERSET of /opt's live spine (plus explicitly
  declared, intentional drops for delists).

Doctrine: /opt is the single live writer of the spine. A clone that wants to
edit skus.json must carry /opt's current growth (pull it in / re-bank first),
never a stale subset. Intentional removals (delist_card) declare --allow-drop.

Exit codes: 0 safe · 3 would revert live keys · 2 usage/IO error.
"""
import argparse
import json
import sys


def load_keys(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    skus = data.get("skus", data) if isinstance(data, dict) else data
    if not isinstance(skus, dict):
        raise ValueError(f"{path}: 'skus' is not an object")
    return set(skus)


def evaluate(clone_keys, live_keys, allow_drop):
    """Return the set of live keys the push would wrongly drop.

    A key counts as a wrongful drop when it exists live, is absent from the
    clone being pushed, and was NOT explicitly declared droppable. Empty set
    means the push is safe (clone ⊇ live, modulo declared drops).
    """
    return (live_keys - clone_keys) - set(allow_drop)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clone-skus", required=True,
                    help="skus.json about to be pushed (the clone's copy)")
    ap.add_argument("--live-skus", required=True,
                    help="/opt live skus.json (the deploy target being grown)")
    ap.add_argument("--allow-drop", action="append", default=[],
                    help="a slug this push intentionally removes (delist); repeatable")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    try:
        clone_keys = load_keys(args.clone_skus)
        live_keys = load_keys(args.live_skus)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"spine_guard: cannot read spine: {e}", file=sys.stderr)
        return 2

    dropped = evaluate(clone_keys, live_keys, args.allow_drop)
    added = clone_keys - live_keys

    if args.json:
        print(json.dumps({
            "safe": not dropped,
            "would_revert": sorted(dropped),
            "adds": sorted(added),
            "allow_drop": sorted(args.allow_drop),
            "clone_count": len(clone_keys),
            "live_count": len(live_keys),
        }, indent=2))
    if dropped:
        if not args.json:
            print(f"spine_guard: ABORT — push would revert {len(dropped)} live spine "
                  f"entr{'y' if len(dropped) == 1 else 'ies'} the gateway grew:",
                  file=sys.stderr)
            for k in sorted(dropped):
                print(f"  - {k}", file=sys.stderr)
            print("Re-bank /opt's growth (or pull it into this clone) before pushing; "
                  "for an intentional delist pass --allow-drop <slug>.", file=sys.stderr)
        return 3
    if not args.json:
        print(f"spine_guard: OK — clone spine ⊇ live ({len(clone_keys)} ⊇ {len(live_keys)}, "
              f"+{len(added)} new, {len(args.allow_drop)} declared drop(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
