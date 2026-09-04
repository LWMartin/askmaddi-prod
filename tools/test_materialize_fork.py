"""Tests for materialize_fork.py — comparator-fork -> vs-pair materialization.

Offline: the Qwen3 typer is injected as a mock client (prompt -> raw JSON), so
these run with no Ollama. They lock vocab/mention detection, fork>positioning
tallying with the distinct-source + MENTION_FLOOR discipline, threshold
promotion, and seed append/dedup.
"""
import json

import materialize_fork as M


def _card(card_id, brand, model, clauses):
    """clauses: list of (source_id, text) → packed into one axis' sentiment."""
    return {
        "card_id": card_id,
        "identity": {"display_name": f"{brand} {model}", "brand": brand,
                     "category": "body", "subcategory": "mirrorless"},
        "lead_axes": [{
            "axis_id": "overall", "display_name": "Overall",
            "face_quote": "",
            "sentiment": {"pos": 1, "neu": 0, "neg": 0, "total": 1,
                          "sources": [{"source_id": sid, "quote_excerpt": txt}
                                      for sid, txt in clauses]},
        }],
    }


# Two Sonys that mention each other, plus a Canon.
A7V = _card("sony-a7-v", "Sony", "A7 V", [
    ("rev-1", "I picked the A7 V instead of the A7 IV for the new sensor."),
    ("rev-2", "The A7 V shares the same mount as the A7 IV."),
    ("rev-3", "Choosing between the A7 V and the A7 IV comes down to budget."),
])
A7IV = _card("sony-a7iv", "Sony", "A7 IV", [
    ("rev-9", "The A7 IV is still great next to the A7 V."),
])
R6 = _card("canon-r6", "Canon", "R6", [
    ("rev-5", "The R6 is a joy to shoot."),
])


def _mock_client(prompt):
    """Fork iff the CLAUSE (not the prompt instructions) contains a fork cue."""
    sentence = prompt.lower().rsplit("sentence:", 1)[-1]
    cue = any(w in sentence for w in ("instead of", "choosing between",
                                      "rather than"))
    return json.dumps({"label": "fork" if cue else "positioning",
                       "confidence": 0.9})


# ─── vocab ───────────────────────────────────────────────────────────────────

def test_build_vocab_strips_brand_and_matches_model():
    vocab = M.build_vocab([A7V, R6])
    assert "sony-a7-v" in vocab and "canon-r6" in vocab
    assert vocab["sony-a7-v"].search("i love the a7 v")
    assert not vocab["sony-a7-v"].search("sony makes cameras")  # brand alone


def test_build_vocab_drops_parenthetical_mpn():
    c = _card("sony-a7iv", "Sony", "A7 IV (ILCE-7M4)", [])
    vocab = M.build_vocab([c])
    assert vocab["sony-a7iv"].search("the a7 iv is nice")


# ─── mentions ────────────────────────────────────────────────────────────────

def test_find_mentions_directed_and_self_excluded():
    vocab = M.build_vocab([A7V, A7IV, R6])
    mentions = M.find_mentions([A7V, A7IV, R6], vocab)
    # A7V's clauses name the A7 IV three times; never itself.
    assert ("sony-a7-v", "sony-a7iv") in mentions
    assert len(mentions[("sony-a7-v", "sony-a7iv")]) == 3
    assert ("sony-a7-v", "sony-a7-v") not in mentions
    # R6 mentions nobody.
    assert not any(k[0] == "canon-r6" for k in mentions)


def test_pair_key_is_canonical():
    assert M.pair_key("b", "a") == ("a", "b") == M.pair_key("a", "b")


def test_find_mentions_drops_cross_category_kit_pairings():
    # A lens named in a body's review is a kit pairing, not a fork.
    body = _card("canon-r5", "Canon", "R5", [("s1", "I mounted the 100-400 on it")])
    lens = _card("canon-100-400", "Canon", "100-400", [])
    lens["identity"]["category"] = "lens"
    vocab = M.build_vocab([body, lens])
    mentions = M.find_mentions([body, lens], vocab)
    assert ("canon-r5", "canon-100-400") not in mentions  # body↔lens dropped


# ─── tally + typing ──────────────────────────────────────────────────────────

def test_tally_counts_distinct_fork_sources_with_precedence():
    M._NAMES.update({"sony-a7-v": "Sony A7 V", "sony-a7iv": "Sony A7 IV"})
    vocab = M.build_vocab([A7V, A7IV])
    mentions = M.find_mentions([A7V, A7IV], vocab)
    typer = M.ForkTyper(client=_mock_client)
    tally = M.tally_forks(mentions, typer)
    rec = tally[("sony-a7-v", "sony-a7iv")]
    # rev-1 ("instead of") and rev-3 ("choosing between") fork; rev-2 positioning;
    # rev-9 (from A7IV side) is positioning. Distinct fork sources = {rev-1, rev-3}.
    assert rec["fork_sources"] == {"rev-1", "rev-3"}


def test_mention_floor_skips_thin_pairs_without_typing():
    # A pair mentioned by only 1 source is below MENTION_FLOOR -> no typing.
    x = _card("pana-gh5", "Panasonic", "GH5", [("s1", "the s5 is a step up")])
    y = _card("pana-s5", "Panasonic", "S5", [])
    M._NAMES.update({"pana-gh5": "Panasonic GH5", "pana-s5": "Panasonic S5"})
    vocab = M.build_vocab([x, y])
    mentions = M.find_mentions([x, y], vocab)
    assert ("pana-gh5", "pana-s5") in mentions  # detected...
    calls = []
    typer = M.ForkTyper(client=lambda p: calls.append(1) or
                        json.dumps({"label": "fork", "confidence": 1.0}))
    tally = M.tally_forks(mentions, typer)
    assert calls == []  # ...but below MENTION_FLOOR, so never typed
    assert tally[("pana-gh5", "pana-s5")]["fork_sources"] == set()


def test_limit_caps_llm_calls():
    M._NAMES.update({"sony-a7-v": "Sony A7 V", "sony-a7iv": "Sony A7 IV"})
    vocab = M.build_vocab([A7V, A7IV])
    mentions = M.find_mentions([A7V, A7IV], vocab)
    calls = []
    typer = M.ForkTyper(client=lambda p: calls.append(1) or _mock_client(p))
    M.tally_forks(mentions, typer, limit=1)
    assert len(calls) == 1


# ─── resilience: isolated failure vs sustained outage ────────────────────────

def test_isolated_typer_failure_becomes_abstain_not_crash():
    # One source's clause raises (a transient Ollama drop); the rest type fine.
    mentions = {("aa", "bb"): [
        ("boom", "the bb explodes here"),                    # will raise
        ("f1", "I chose aa instead of bb"),                  # fork
        ("f2", "choosing between aa and bb"),                # fork
        ("p1", "aa shares a mount with bb"),                 # positioning
    ]}
    M._NAMES.update({"aa": "Aa", "bb": "Bb"})

    def client(prompt):
        if "explodes" in prompt.lower():
            raise OSError("connection reset")
        return _mock_client(prompt)

    tally = M.tally_forks(mentions, M.ForkTyper(client=client))
    # crash avoided; the two real forks still counted, boom counted abstain.
    assert tally[("aa", "bb")]["fork_sources"] == {"f1", "f2"}


def test_sustained_outage_aborts_loudly():
    # 9 sources all raising -> exceeds MAX_CONSEC_FAILURES(8) -> abort.
    mentions = {("aa", "bb"): [(f"s{i}", "mentions bb") for i in range(9)]}
    M._NAMES.update({"aa": "Aa", "bb": "Bb"})

    def dead(prompt):
        raise OSError("remote end closed connection")

    import pytest
    with pytest.raises(RuntimeError, match="Ollama looks down"):
        M.tally_forks(mentions, M.ForkTyper(client=dead))


# ─── promotion + emit ────────────────────────────────────────────────────────

def test_promoted_pairs_threshold():
    tally = {("a", "b"): {"fork_sources": {"1", "2", "3"}, "mention_sources": {"1", "2", "3"}},
             ("a", "c"): {"fork_sources": {"1", "2"}, "mention_sources": {"1", "2"}}}
    assert M.promoted_pairs(tally, threshold=3) == [(3, ("a", "b"))]


def test_emit_appends_new_and_skips_existing(tmp_path):
    seed = tmp_path / "vs_pairs.json"
    seed.write_text(json.dumps({"pairs": [{"a": "sony-a7-v", "b": "sony-a7iv"}]}),
                    encoding="utf-8")
    promoted = [(5, ("sony-a7-v", "sony-a7iv")),   # already present -> skip
                (4, ("canon-r6", "sony-a7c"))]      # new -> append
    appended = M.emit_to_seed(promoted, seed_path=seed)
    assert appended == [(4, ("canon-r6", "sony-a7c"))]
    doc = json.loads(seed.read_text())
    new = [p for p in doc["pairs"] if p["a"] == "canon-r6"][0]
    assert new["source"] == "comparator_fork" and new["status"] == "proposed"
    assert "4 sources" in new["reason"]
    # idempotent: a second emit of the same set appends nothing.
    assert M.emit_to_seed(promoted, seed_path=seed) == []


# ─── typer parsing ───────────────────────────────────────────────────────────

def test_forktyper_parses_and_abstains():
    t = M.ForkTyper(client=lambda p: json.dumps({"label": "fork", "confidence": 0.8}))
    assert t.type_clause("x", "A", "B") == ("fork", 0.8)
    bad = M.ForkTyper(client=lambda p: "not json at all")
    assert bad.type_clause("x", "A", "B") == ("abstain", 0.0)
