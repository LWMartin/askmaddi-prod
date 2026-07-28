"""
Anchor-claim ownership + share apportionment (2026-07-28).

THE RULING. The stat line owns the anchor claim outright. Before this, the
same fact about the same axis was asserted twice on every card — once by
build_site.answer_stat_line near the top, once by the synthesis paragraph's
S1, generated in a different repo:

    stat line:  "...among the 55 reviews we compiled, af performance drew the
                 most discussion: 341 claims, which we read as 50% pos..."
    S1:         "Across 55 sources, af performance draws the most discussion:
                 341 claims from 47 of them — 50% pos..."

Two surfaces is bad; two INDEPENDENT COMPUTATIONS of one quantity is worse,
and they disagreed in production:

    sigma-35 optical performance (176/34/39) → stat line 71/14/16 = 101%
    sony-a7c mount compatibility (54/52/3)   → stat line 50/48/3  = 101%

The stat line is the sentence built to be lifted whole by an extractor. Shares
that do not total 100 discredit precisely the surface meant to be checkable.

Three things are pinned here:

  1. pct_triple apportions — always exactly 100, matching the aggregator's
     _dist_pcts, which is the source of truth this was ported from.
  2. The stat line carries the on-axis coverage figure S1 used to hold, so
     retiring S1 lost no information.
  3. meta_description is COMPOSED, never a mid-sentence slice.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_site import (  # noqa: E402
    META_DESC_LIMIT,
    answer_stat_line,
    meta_description,
    pct,
    pct_triple,
    sentiment_triple,
)


# ── Oracle ───────────────────────────────────────────────────────────────
# Deliberately written differently from the implementation: sort by remainder
# descending and hand out leftovers, rather than the implementation's keyed
# sort. Two shapes agreeing over an exhaustive grid is evidence; one shape
# compared against a copy of itself is not.
def _oracle(pos, neu, neg):
    d = pos + neu + neg
    if d <= 0:
        return (0, 0, 0)
    exact = [100 * pos / d, 100 * neu / d, 100 * neg / d]
    out = [int(x) for x in exact]
    short = 100 - sum(out)
    rema = sorted(
        ((exact[i] - out[i], i) for i in range(3)),
        key=lambda t: (-t[0], t[1]),
    )
    for k in range(short):
        out[rema[k][1]] += 1
    return tuple(out)


# ── 1. Apportionment ─────────────────────────────────────────────────────

def test_pct_triple_always_sums_to_100():
    """Exhaustive over a grid that includes every fractional-remainder shape
    that matters. The naive per-share round fails this; that is the point."""
    for pos in range(0, 26):
        for neu in range(0, 26):
            for neg in range(0, 26):
                t = pct_triple(pos, neu, neg)
                if pos + neu + neg == 0:
                    assert t == (0, 0, 0)
                else:
                    assert sum(t) == 100, (pos, neu, neg, t)


def test_pct_triple_matches_independent_oracle():
    for pos in range(0, 26):
        for neu in range(0, 26):
            for neg in range(0, 26):
                assert pct_triple(pos, neu, neg) == _oracle(pos, neu, neg)


def test_pct_triple_is_deterministic():
    """Byte-stable output per input — the determinism invariant the whole
    card pipeline rests on."""
    for _ in range(5):
        assert pct_triple(1, 1, 1) == pct_triple(1, 1, 1)
        assert sum(pct_triple(1, 1, 1)) == 100


@pytest.mark.parametrize("counts,expected", [
    ((176, 34, 39), (71, 13, 16)),   # sigma-35 optical performance
    ((54, 52, 3), (49, 48, 3)),      # sony-a7c mount compatibility
])
def test_live_regressions_that_printed_101(counts, expected):
    """The two production cases, pinned by value. Both summed to 101 under the
    naive round; the assertion below proves this test guards a real defect and
    not a hypothetical one."""
    pos, neu, neg = counts
    total = pos + neu + neg
    naive = (pct(pos, total), pct(neu, total), pct(neg, total))
    assert sum(naive) == 101          # what shipped
    assert pct_triple(*counts) == expected
    assert sum(pct_triple(*counts)) == 100


def test_pct_triple_denominator_ignores_a_disagreeing_total():
    """Shares divide by the polar counts, not by sentiment.total. A card whose
    total field drifts from its own pos/neu/neg cannot bend the percentages."""
    sent = {"pos": 176, "neu": 34, "neg": 39, "total": 9999}
    assert sentiment_triple(sent) == "71% positive, 13% neutral, 16% negative"


def test_zero_claims_asserts_nothing():
    assert sentiment_triple({"pos": 0, "neu": 0, "neg": 0, "total": 0}) == ""


# ── 2. The stat line owns the anchor ─────────────────────────────────────

def _axis(axis_id, display, pos, neu, neg, covered=None):
    a = {"axis_id": axis_id, "display_name": display,
         "sentiment": {"pos": pos, "neu": neu, "neg": neg,
                       "total": pos + neu + neg}}
    if covered is not None:
        a["convergence"] = {"source_count": covered}
    return a


def _card(axes, n_sources=55):
    return {
        "card_id": "sony-a7-v",
        "identity": {"display_name": "Sony A7 V"},
        "freshness": {"last_built": "2026-07-20T12:00:00+00:00"},
        "lead_axes": axes,
        "detail_axes": [],
        "sources": [{"source_id": f"s{i}"} for i in range(n_sources)],
    }


def test_stat_line_carries_the_on_axis_coverage():
    """Inherited from retired S1. Without it the compiled-review count doubles
    as the on-axis count — wrong on all 11 live cards, by 1 to 8 sources."""
    card = _card([_axis("af", "AF Performance", 170, 133, 38, covered=47)])
    line = answer_stat_line(card, 55)
    assert "341 claims from 47 of them" in line
    assert "among the 55 reviews we compiled" in line


def test_stat_line_omits_coverage_when_it_equals_the_corpus():
    """'341 claims from 55 of them' beside 'the 55 reviews we compiled' is
    noise, not precision."""
    card = _card([_axis("af", "AF Performance", 170, 133, 38, covered=55)])
    assert "of them" not in answer_stat_line(card, 55)


def test_stat_line_omits_coverage_when_unknown():
    """A missing count is never written as a guess."""
    card = _card([_axis("af", "AF Performance", 170, 133, 38)])
    assert "of them" not in answer_stat_line(card, 55)


def test_stat_line_shares_sum_to_100():
    card = _card([_axis("opt", "Optical Performance", 176, 34, 39, covered=44)],
                 n_sources=50)
    line = answer_stat_line(card, 50)
    assert "71% positive, 13% neutral, 16% negative" in line


# ── 3. meta_description is composed, never sliced ────────────────────────

def test_meta_description_is_a_whole_claim_within_the_limit():
    card = _card([_axis("af", "AF Performance", 170, 133, 38, covered=47)])
    md = meta_description(card, 55, "Sony A7 V", "Reviewers are most positive.")
    assert len(md) <= META_DESC_LIMIT
    assert md.endswith(".")
    assert "\u2026" not in md              # never an ellipsis truncation
    assert md.startswith("Sony A7 V:")     # travels alone into a SERP
    assert "47 of 55 reviews" in md


def test_meta_description_never_cuts_mid_sentence():
    """The old behaviour was synth[:155] + '…', which ended mid-word on all 11
    live cards. Tier 2 packs WHOLE sentences or falls through."""
    long_para = ("Reviewers are most positive about color rendition (68% "
                 "positive, 25% neutral, 7% negative across 72 claims). "
                 "The most criticism lands on AF noise: 36% positive, 31% "
                 "neutral, 33% negative across 84 claims.")
    card = {"identity": {"display_name": "X"}, "lead_axes": [],
            "detail_axes": [], "sources": []}
    md = meta_description(card, 0, "X", long_para)
    assert len(md) <= META_DESC_LIMIT
    assert md.endswith(".")
    assert "\u2026" not in md
    # whatever survived must be a prefix of the original, cut at a boundary
    assert long_para.startswith(md.rstrip("."))


def test_meta_description_falls_back_without_asserting_anything_false():
    card = {"identity": {"display_name": "X"}, "lead_axes": [],
            "detail_axes": [], "sources": []}
    md = meta_description(card, 4, "X", "")
    assert md == "X \u2014 reviews synthesized from 4 sources."


def test_meta_description_is_deterministic():
    card = _card([_axis("af", "AF Performance", 170, 133, 38, covered=47)])
    a = meta_description(card, 55, "Sony A7 V", "")
    b = meta_description(card, 55, "Sony A7 V", "")
    assert a == b
