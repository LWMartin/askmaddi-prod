#!/usr/bin/env python3
"""
rename_cards — apply a reviewed batch of card display-name corrections.

Why (2026-11-17): 236 of 262 card titles were slug-shaped ("Canon 100 400mm
f5.6", "Nikon z6 iii") — not how anyone searches, and often ambiguous about
WHICH product the card is. Names were derived per card from its own evidence
(listing title, Adorama link, spine MPN, review titles), checked mechanically,
and reviewed by Lee row by row. This tool writes the approved rows.

One rename = the same three writes the 2026-11-06 hand rename of the 70-200 made:
  1. spine   skus_registry.set_override(slug, 'display_name', new) — the
             sanctioned door; a later card rebuild re-reads it, so the name
             survives regeneration.
  2. card    identity.display_name, and sentence 1 of the consensus paragraph
             ("Synthesized from N reviewer sources for <name>.") — a template,
             so the swap is exact.
  3. export  re-derived by phantom-ops card_export.build_export_artifacts (the
             module that wrote it), loaded BY FILE PATH — no sys.path insert, so
             nothing in that directory can shadow a module here.

FAIL CLOSED, per row. A row is refused (reported, nothing written for it) when:
  - the live card's name is not the reviewed `old` (someone changed it since);
  - card_export does not reproduce the card's CURRENT export exactly (then a
    re-derive would also change something other than the name);
  - the old name survives anywhere in the card after the edit (an unknown
    location — better to look than to half-rename);
  - the card carries a live Adorama price whose pin passes the identity gate
    (pin_identity) under the old name but fails under the new one (a rename
    must never withdraw a correct price);
  - the spine has no entry for the slug.
A row already applied (card and spine both carry `new`) is reported 'already'.

Guides and pages follow on their own: regen_guides (nightly) re-reads card names
and build_site re-renders; the nightly bot_push banks /opt like the 11-06 rename.

Usage (on the box, as askmaddi, from /opt/askmaddi-prod):
  python3 tools/rename_cards.py tools/rename_plans/<plan>.json            # dry run
  python3 tools/rename_cards.py tools/rename_plans/<plan>.json --apply

py3.9-safe, stdlib only (cron runs /usr/bin/python3 = 3.9.25).
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "gateway"))
sys.path.insert(0, str(HERE))
import skus_registry  # noqa: E402
from pin_identity import check_pin  # noqa: E402

DEFAULT_AGG = "/home/phantomops/phantom-ops/claude/workspace/aggregator-build"
FEED_SOURCE = "adorama_feed"


def load_card_export(agg_dir):
    """build_export_artifacts from phantom-ops card_export.py, by file path."""
    path = Path(agg_dir) / "card_export.py"
    spec = importlib.util.spec_from_file_location("_askmaddi_card_export", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load card_export from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_export_artifacts


def _anchor(n, name):
    word = "source" if n == 1 else "sources"
    return f"Synthesized from {n} reviewer {word} for {name}."


def _rename_consensus(text, old, new):
    """Swap the name in sentence 1 only. Returns (text, ok)."""
    if not text or old not in text:
        return text, True
    for n in range(1, 1000):
        a = _anchor(n, old)
        if text.startswith(a):
            return _anchor(n, new) + text[len(a):], True
    return text, False


def _strings(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)
    elif isinstance(obj, str):
        yield obj


def plan_row(row, card, spine_entry, build_export):
    """Return (status, why, new_card). status: ok | already | refused."""
    slug, old, new = row["slug"], row["old"], row["new"]
    if spine_entry is None:
        return "refused", "no spine entry", None
    ident = card.get("identity") or {}
    live = ident.get("display_name")
    spine_new = (spine_entry.get("overrides") or {}).get("display_name")
    if live == new and spine_new == new:
        return "already", "", None
    if live != old:
        return "refused", f"live name is {live!r}, reviewed {old!r}", None

    export = card.get("export")
    if export is not None:
        rebuilt = build_export(card, slug=slug)
        if {k: export.get(k) for k in rebuilt} != rebuilt:
            return "refused", "card_export does not reproduce the current export", None

    p = card.get("pricing") or {}
    if p.get("current_new_source") == FEED_SOURCE and p.get("current_new_usd"):
        url = p.get("current_new_url") or p.get("affiliate_url")
        if not check_pin(old, url) and check_pin(new, url):
            return "refused", "new name would fail the price identity gate: " + \
                " | ".join(check_pin(new, url)), None

    out = copy.deepcopy(card)
    out["identity"]["display_name"] = new
    syn = out.get("synthesis") or {}
    if "consensus_paragraph" in syn:
        text, ok = _rename_consensus(syn["consensus_paragraph"], old, new)
        if not ok:
            return "refused", "old name in consensus outside the sentence-1 template", None
        syn["consensus_paragraph"] = text
    if export is not None:
        out["export"] = dict(export)
        out["export"].update(build_export(out, slug=slug))
    # Exactly the fields this tool writes. Review titles and quoted excerpts
    # ("Canon R6 Review ...", "...on the Sigma 20mm f1.4 DG DN Art...") are the
    # reviewers' own words and legitimately keep whatever spelling they used.
    owned = [out["identity"]["display_name"], syn.get("consensus_paragraph"), out.get("export")]
    if any(old in s.replace(new, "") for s in _strings(owned)):
        return "refused", "old name survives in identity/synthesis/export", None
    return "ok", "", out


def write_card_atomic(path, card):
    """Same serialization and atomic replace as price_writeback."""
    text = json.dumps(card, indent=2, ensure_ascii=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def run(plan_path, cards_dir, spine_path, agg_dir, apply=False, out=sys.stdout):
    build_export = load_card_export(agg_dir)
    rows = json.loads(Path(plan_path).read_text(encoding="utf-8"))["renames"]
    skus = skus_registry.load_registry(spine_path).get("skus", {})
    counts = {"ok": 0, "already": 0, "refused": 0, "written": 0}
    for row in rows:
        path = Path(cards_dir) / f"{row['slug']}.json"
        if not path.exists():
            status, why, new_card = "refused", "no card file", None
        else:
            card = json.loads(path.read_text(encoding="utf-8"))
            status, why, new_card = plan_row(row, card, skus.get(row["slug"]), build_export)
        counts[status] += 1
        line = f"{status:<8} {row['slug']:<44} {row['old']!r} -> {row['new']!r}"
        if why:
            line += f"   [{why}]"
        if status == "ok" and apply:
            r = skus_registry.set_override(row["slug"], "display_name", row["new"], path=spine_path)
            if r not in ("written", "no-op"):
                line += f"   [spine: {r}; card NOT written]"
            else:
                write_card_atomic(path, new_card)
                counts["written"] += 1
                line += "   [written]"
        print(line, file=out)
    mode = "APPLIED" if apply else "DRY RUN (nothing written; add --apply)"
    print(f"\n{mode}: {counts['ok']} ok, {counts['already']} already, "
          f"{counts['refused']} refused, {counts['written']} written", file=out)
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description="Apply reviewed card display-name corrections.")
    ap.add_argument("plan")
    ap.add_argument("--cards-dir", default=str(REPO / "data" / "cards"))
    ap.add_argument("--spine", default=str(REPO / "data" / "skus.json"))
    ap.add_argument("--agg", default=os.environ.get("ASKMADDI_AGG", DEFAULT_AGG),
                    help="phantom-ops aggregator-build dir (for card_export.py)")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    c = run(a.plan, a.cards_dir, a.spine, a.agg, apply=a.apply)
    return 1 if c["refused"] else 0


if __name__ == "__main__":
    sys.exit(main())
