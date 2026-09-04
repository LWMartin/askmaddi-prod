#!/usr/bin/env python3
"""
materialize_fork.py — turn the comparator FORK signal into curated vs-pairs.

Fast-follow to the curated vs-page seed (data/vs_pairs.json). Recon proved that
no on-card *structural* signal (category / subcategory / price / shared axes)
separates a real cross-shop from a coincidence, so pair selection was seeded by
hand. This tool adds the demand-true source the side-by-side seed note always
pointed at: the comparator FORK — reviewers weighing product A *against* product
B as a purchase alternative.

Where the corpus lives: each published card already carries its review clauses
(every axis' face_quote + sentiment.sources[].quote_excerpt). So we mine the
LIVE cards directly — no separate classifier_corpus.json per card needed (the
phantom-ops aggregator workspace only ever built four).

The FORK-vs-POSITIONING distinction is the whole point (note
2026-06-25-comparator-mining-model-cascade §3): a flat mention count lets a
positioning reference ("as good as the A1") outrank a real purchase fork. Only
an instruction-tuned model separates them, so every carded-vs-carded mention is
typed by Qwen3-4B through a forced-choice grammar (same posture as
resolve_sku.py's disambiguator). A pair is promoted only when >= --fork-threshold
DISTINCT review sources carry a FORK-typed clause about it.

Promoted pairs are appended to data/vs_pairs.json as source='comparator_fork',
status='proposed' — the human ratify gate (review-is-the-craft-seat) still holds;
build_vs_pages.py renders whatever is ratified.

    python3 tools/materialize_fork.py --report              # type + show table
    python3 tools/materialize_fork.py --emit                # + append to seed
    python3 tools/materialize_fork.py --report --limit 50   # cap LLM calls
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CARDS_DIR = ROOT / "data" / "cards"
SEED_PATH = ROOT / "data" / "vs_pairs.json"

DEFAULT_MODEL = "hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT = 120

# A pair is promoted only when this many DISTINCT review sources carry a
# fork-typed clause about it — the same distinct-source discipline the flat
# comparator miner uses, applied to fork-typed (not raw) mentions.
FORK_THRESHOLD = 3
# Only spend LLM calls on pairs a human would plausibly promote: at least this
# many distinct sources must MENTION the pair before we type its clauses. Kills
# the long tail of one-off coincidental name-drops without an LLM call each.
MENTION_FLOOR = 3

# Consecutive per-clause typer failures (each already retried inside _generate)
# that mean "Ollama is down, not a blip" — abort rather than bank a zero-fork run.
MAX_CONSEC_FAILURES = 8

# Forced-choice typing schema — Ollama compiles it to a decoding grammar so the
# model emits ONLY this object (fork vs positioning) and stops.
_TYPE_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": ["fork", "positioning"]},
        "confidence": {"type": "number"},
    },
    "required": ["label", "confidence"],
}

_STOP = {
    "the", "and", "for", "with", "camera", "lens", "digital", "mirrorless",
    "dslr", "mm", "kit", "body", "black", "silver", "mark", "edition", "version",
}


# ─── vocabulary: a matcher per carded product ────────────────────────────────

def _model_phrase(card):
    """The distinctive model string for a card: display_name minus brand and
    minus any parenthetical (the MPN aside), lowercased and space-collapsed."""
    ident = card.get("identity", {}) or {}
    dn = ident.get("display_name", "") or ""
    brand = ident.get("brand", "") or ""
    phrase = re.sub(r"\(.*?\)", " ", dn)
    if brand:
        phrase = re.sub(re.escape(brand), " ", phrase, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", phrase).strip().lower()


def build_vocab(cards):
    """card_id -> compiled regex matching that product's model phrase (and any
    sku_alt_names). Phrases shorter than 4 chars, or single stopword tokens, are
    dropped as too ambiguous to attribute a mention to."""
    vocab = {}
    for card in cards:
        ident = card.get("identity", {}) or {}
        phrases = {_model_phrase(card)}
        for alt in ident.get("sku_alt_names", []) or []:
            phrases.add(re.sub(r"\s+", " ", str(alt)).strip().lower())
        good = []
        for p in phrases:
            # Keep distinctive model strings: any phrase with a digit (r6, a1,
            # gh5, z7 — short but unambiguous), or a >=4-char alpha phrase. Drop
            # a lone generic stopword ("body", "black").
            has_digit = bool(re.search(r"\d", p))
            if not has_digit and len(p) < 4:
                continue
            toks = re.findall(r"[a-z0-9]+", p)
            if len(toks) == 1 and toks[0] in _STOP:
                continue
            good.append(re.escape(p))
        if good:
            good.sort(key=len, reverse=True)
            vocab[card["card_id"]] = re.compile(r"\b(?:" + "|".join(good) + r")\b")
    return vocab


def _clauses(card):
    """(source_id, text) for every review clause a card carries — axis
    face_quotes and per-source sentiment quote excerpts. Deduped."""
    seen, out = set(), []

    def text_of(x):
        if isinstance(x, str):
            return x
        if isinstance(x, dict):
            return x.get("text") or x.get("quote") or x.get("quote_excerpt") or ""
        return ""

    for axis in (card.get("lead_axes") or []) + (card.get("detail_axes") or []):
        fq = text_of(axis.get("face_quote"))
        if fq:
            key = ("face", fq)
            if key not in seen:
                seen.add(key)
                out.append((axis.get("axis_id", "face"), fq))
        for src in ((axis.get("sentiment") or {}).get("sources") or []):
            q = text_of(src.get("quote_excerpt"))
            sid = src.get("source_id") or src.get("url") or ""
            if q and (sid, q) not in seen:
                seen.add((sid, q))
                out.append((sid, q))
    return out


# ─── mention detection ───────────────────────────────────────────────────────

def find_mentions(cards, vocab):
    """Directed mentions: for each card (subject), the clauses whose text names
    another carded product IN THE SAME CATEGORY. Returns
    {(subject_id, other_id): [(source_id, clause)]}.

    Same-category guard: a purchase fork is an either/or between comparable
    goods. A lens named in a body's review (or vice-versa) is a kit pairing, not
    a cross-shop — 'I put the 100-400 on my R5' is not 'R5 vs 100-400'. Bodies
    fork bodies, lenses fork lenses; cross-category mentions are dropped before
    they ever reach the typer."""
    cat = {c["card_id"]: (c.get("identity", {}) or {}).get("category") or "" for c in cards}
    mentions = defaultdict(list)
    for card in cards:
        cid = card["card_id"]
        for source_id, clause in _clauses(card):
            low = clause.lower()
            for other_id, rx in vocab.items():
                if other_id == cid:
                    continue
                if cat.get(cid) and cat[cid] != cat.get(other_id):
                    continue  # cross-category kit pairing, not a fork
                if rx.search(low):
                    mentions[(cid, other_id)].append((source_id, clause))
    return mentions


def pair_key(a, b):
    """Canonical unordered pair — same orientation build_vs_pages.vs_slug uses."""
    return tuple(sorted((a, b)))


# ─── the Qwen3 forced-choice typer ───────────────────────────────────────────

class ForkTyper:
    """Types a review clause as 'fork' (a purchase fork — the reviewer is weighing
    the two products as buy alternatives) or 'positioning' (a reference/spec/
    context mention). Live via Ollama /api/generate + grammar; inject `client`
    (prompt -> raw response str) in tests."""

    def __init__(self, model=DEFAULT_MODEL, ollama_url=DEFAULT_OLLAMA_URL,
                 client=None, timeout=DEFAULT_TIMEOUT):
        self.model = model
        self.ollama_url = ollama_url
        self.client = client
        self.timeout = timeout

    def _generate(self, prompt):
        if self.client is not None:
            return self.client(prompt)
        payload = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": _TYPE_SCHEMA,
            "options": {"temperature": 0.0, "num_predict": 96},
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.ollama_url}/api/generate",
            data=payload, headers={"Content-Type": "application/json"})
        # Retry with backoff. Ollama on this shared box drops connections under
        # contention / model eviction (RemoteDisconnected — an http.client
        # exception, NOT a URLError; the first version missed it and one blip
        # aborted the whole overnight batch). Catch the connection-reset family
        # too. A persistent failure re-raises for the caller to handle.
        last_err = None
        for attempt in (1, 2, 3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8")).get("response", "")
            except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
                last_err = e
                if attempt < 3:
                    time.sleep(2 * attempt)
        raise last_err

    def type_clause(self, clause, subject_name, other_name):
        prompt = _build_prompt(clause, subject_name, other_name)
        raw = self._generate(prompt)
        try:
            obj = json.loads(raw)
            label = obj.get("label")
            if label in ("fork", "positioning"):
                return label, float(obj.get("confidence") or 0.0)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        return "abstain", 0.0


def _build_prompt(clause, subject_name, other_name):
    return (
        "You classify a sentence from a product review.\n"
        f"The review is about: {subject_name}\n"
        f"The sentence mentions another product: {other_name}\n\n"
        "Decide the ROLE of that other product in the sentence:\n"
        '- "fork": the reviewer treats it as a purchase ALTERNATIVE — weighing, '
        "choosing between, recommending one over the other, or cross-shopping "
        "the two as options a buyer decides between.\n"
        '- "positioning": the other product is only a reference point — a spec '
        "comparison, lineage/heritage note, name-drop, or context, NOT a buy "
        "decision between the two.\n\n"
        f"Sentence: {clause.strip()[:500]}\n\n"
        'Reply with {"label": "fork"|"positioning", "confidence": 0..1}.'
    )


# ─── tally + promotion ───────────────────────────────────────────────────────

def tally_forks(mentions, typer, limit=None):
    """Type the clauses of every pair that clears MENTION_FLOOR and count DISTINCT
    fork-typed sources per unordered pair. Returns
    {pair_key: {"fork_sources": set, "mention_sources": set, "typed": n}}.

    Precedence: a source counts as fork for a pair if ANY of its clauses types
    fork (same fork > positioning rule as the reference miner)."""
    # merge directed mentions into unordered pairs first, so a<->b sources pool
    by_pair = defaultdict(list)
    for (subj, other), hits in mentions.items():
        for source_id, clause in hits:
            by_pair[pair_key(subj, other)].append((subj, other, source_id, clause))

    result = {}
    typed_calls = 0
    consec_fail = 0  # spans pairs: a sustained outage is a run-level condition
    # deterministic order; densest pairs first so --limit spends on the best.
    ordered = sorted(by_pair.items(),
                     key=lambda kv: (-len({h[2] for h in kv[1]}), kv[0]))
    for pk, hits in ordered:
        mention_sources = {h[2] for h in hits}
        rec = {"fork_sources": set(), "mention_sources": mention_sources, "typed": 0}
        result[pk] = rec
        if len(mention_sources) < MENTION_FLOOR:
            continue  # not worth an LLM call
        fork_sources = set()
        for subj, other, source_id, clause in sorted(hits, key=lambda h: (h[2], h[3])):
            if source_id in fork_sources:
                continue  # already fork-confirmed for this pair
            if limit is not None and typed_calls >= limit:
                break
            try:
                label, _conf = typer.type_clause(clause, _name(subj), _name(other))
                consec_fail = 0
            except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
                # One clause failing (after _generate's own retries) must not lose
                # the hundreds of calls already done: count it abstain and move on.
                # But a SUSTAINED outage should abort loudly rather than silently
                # return an all-abstain (zero-fork) result — mirror resolve_pass's
                # "abort the batch on a live-service outage" contract.
                consec_fail += 1
                if consec_fail >= MAX_CONSEC_FAILURES:
                    raise RuntimeError(
                        f"typer failed {MAX_CONSEC_FAILURES}x in a row "
                        f"(last: {e!r}) — Ollama looks down; aborting so the run "
                        f"isn't silently recorded as zero-fork") from e
                label = "abstain"
            typed_calls += 1
            rec["typed"] += 1
            if label == "fork":
                fork_sources.add(source_id)
        rec["fork_sources"] = fork_sources
    return result


_NAMES = {}


def _name(card_id):
    return _NAMES.get(card_id, card_id)


def load_cards(cards_dir=CARDS_DIR):
    cards = []
    for path in sorted(Path(cards_dir).glob("*.json")):
        try:
            cards.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return cards


def promoted_pairs(tally, threshold=FORK_THRESHOLD):
    """Pairs with >= threshold distinct fork-typed sources, densest first."""
    out = []
    for pk, rec in tally.items():
        n = len(rec["fork_sources"])
        if n >= threshold:
            out.append((n, pk))
    out.sort(reverse=True)
    return out


def existing_seed_pairs(seed_path=SEED_PATH):
    try:
        doc = json.loads(Path(seed_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set(), None
    have = {pair_key(e["a"], e["b"]) for e in doc.get("pairs", [])
            if e.get("a") and e.get("b")}
    return have, doc


def emit_to_seed(promoted, seed_path=SEED_PATH):
    """Append promoted pairs not already present. Returns (appended, skipped)."""
    have, doc = existing_seed_pairs(seed_path)
    if doc is None:
        doc = {"pairs": []}
    appended = []
    for n, pk in promoted:
        if pk in have:
            continue
        a, b = pk
        doc["pairs"].append({
            "a": a, "b": b,
            "reason": f"comparator fork ({n} sources)",
            "source": "comparator_fork", "status": "proposed",
        })
        have.add(pk)
        appended.append((n, pk))
    Path(seed_path).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return appended


def _report(promoted, tally, have):
    print(f"\n  fork-promoted carded-vs-carded pairs (>= {FORK_THRESHOLD} fork sources):")
    for n, pk in promoted:
        flag = "  (already in seed)" if pk in have else "  NEW"
        rec = tally[pk]
        print(f"    {n:2d} fork / {len(rec['mention_sources']):2d} mention   "
              f"{pk[0]} vs {pk[1]}{flag}")
    print(f"  {len(promoted)} promoted, "
          f"{sum(1 for _n, pk in promoted if pk not in have)} new.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cards-dir", default=str(CARDS_DIR))
    ap.add_argument("--seed", default=str(SEED_PATH))
    ap.add_argument("--fork-threshold", type=int, default=FORK_THRESHOLD)
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap total LLM type calls (spent on densest pairs first).")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    ap.add_argument("--report", action="store_true", help="Print the promotion table.")
    ap.add_argument("--emit", action="store_true", help="Append new pairs to the seed.")
    ap.add_argument("--minilm", action="store_true",
                    help=argparse.SUPPRESS)  # reserved; refuse to emit if set
    args = ap.parse_args(argv)

    cards = load_cards(args.cards_dir)
    if not cards:
        print("no cards found", file=sys.stderr)
        return 1
    _NAMES.clear()
    _NAMES.update({c["card_id"]: (c.get("identity", {}) or {}).get("display_name")
                   or c["card_id"] for c in cards})

    vocab = build_vocab(cards)
    mentions = find_mentions(cards, vocab)
    print(f"materialize_fork: {len(cards)} cards, {len(vocab)} in vocab, "
          f"{len(mentions)} directed mention edges", file=sys.stderr)

    typer = ForkTyper(model=args.model, ollama_url=args.ollama_url)
    tally = tally_forks(mentions, typer, limit=args.limit)
    promoted = promoted_pairs(tally, threshold=args.fork_threshold)
    have, _doc = existing_seed_pairs(args.seed)

    if args.report or not args.emit:
        _report(promoted, tally, have)

    if args.emit:
        appended = emit_to_seed(promoted, seed_path=args.seed)
        print(f"  [emit] appended {len(appended)} new fork pair(s) -> {args.seed}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
