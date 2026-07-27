"""
Phase 1 — Perplexity-first citation surface (maddi-distribution v2.0 §5).

The load-bearing test in this file is the GROUNDED PERCENTAGE rule. Before
2026-07-27 the A7 IV card led with "524 claims, 32% positive" on video
capability — an axis whose actual split is 168 pos / 31 neu / 325 neg, i.e.
62% NEGATIVE. Every digit was accurate and the sentence was still misleading,
because a lone positive share reads as approval and no denominator is visible
to correct it. That same sentence is reused verbatim as the meta description
and the schema.org description, so one ungrounded figure reached three
surfaces at once.

Hence: a share never appears without its siblings. The other tests here cover
structure — answer above specs, question-form headings, year interpolated at
render — but this is the one that protects a claim about the world.

Specs are NOT demoted by Phase 1. The spine, the UNSPSC axis, the adjudicated
GTIN identity and the manufacturer adapters are the expensive part of a card
and are a citation asset in their own right; the answer block is INSERTED above
them, nothing is pushed down.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_site import (  # noqa: E402
    answer_stat_line,
    asof_phrase,
    most_discussed_axis,
    render_page,
    sentiment_triple,
    specs_heading,
    used_price_heading,
)


def _axis(axis_id, display, pos, neu, neg):
    return {"axis_id": axis_id, "display_name": display,
            "sentiment": {"pos": pos, "neu": neu, "neg": neg,
                          "total": pos + neu + neg}}


def _card(**over):
    card = {
        "card_id": "sony-a7iv",
        "card_version": "1.0",
        "vertical": "photography",
        "identity": {"display_name": "Sony A7 IV", "category": "body",
                     "brand": "Sony"},
        "freshness": {"last_built": "2026-06-22T16:51:36+00:00",
                      "source_count": 55},
        "pricing": {},
        "confidence": {"overall": "medium"},
        # the real A7 IV distribution: majority negative on the loudest axis
        "lead_axes": [_axis("video_capability", "Video Capability",
                            168, 31, 325),
                      _axis("autofocus", "Autofocus", 160, 40, 75)],
        "detail_axes": [],
        "synthesis": {"consensus_paragraph": "Reviewers converge on the hybrid "
                                             "positioning."},
        "sources": [],
        # Specs must be present for the ordering tests to mean anything — an
        # absent specs block renders nothing, which would let "answer above
        # specs" pass vacuously.
        "facts": {"specs": {"weight": {"value": None, "low": 658, "high": 658,
                                       "anchor": 658, "anchor_source": "icecat",
                                       "unit": "g"},
                            "mount": {"value": "Sony E-mount", "low": None,
                                      "high": None, "anchor": None,
                                      "anchor_source": "icecat", "unit": ""}},
                  "provenance": {}, "conflicts": []},
    }
    card.update(over)
    return card


# ────────────────────────────────────────────────────────────────────────
# Grounded percentages — the rule this file exists for
# ────────────────────────────────────────────────────────────────────────

class TestGroundedPercentages:

    def test_all_three_shares_present(self):
        out = sentiment_triple({"pos": 168, "neu": 31, "neg": 325,
                                "total": 524})
        assert "32% positive" in out
        assert "6% neutral" in out
        assert "62% negative" in out

    def test_majority_negative_axis_cannot_read_as_favourable(self):
        """The exact regression. 32% positive alone was the old copy."""
        out = sentiment_triple({"pos": 168, "neu": 31, "neg": 325,
                                "total": 524})
        assert "negative" in out, "a 62%-negative axis must say so"

    def test_no_claims_yields_nothing_rather_than_zeroes(self):
        """0%/0%/0% would assert a measurement nobody made."""
        assert sentiment_triple({"pos": 0, "neu": 0, "neg": 0,
                                 "total": 0}) == ""
        assert sentiment_triple({}) == ""

    def test_stat_line_carries_denominators(self):
        line = answer_stat_line(_card(), 55)
        assert "524 claims" in line
        assert "55 reviews" in line
        assert "positive" in line and "neutral" in line and "negative" in line

    @pytest.mark.parametrize("share", ["positive", "neutral", "negative"])
    def test_no_lone_share_anywhere_on_the_page(self, share):
        """Scan the whole rendered page: any percentage adjacent to one
        sentiment word must have the other two nearby. This is the rule stated
        structurally rather than at a single call site."""
        html = render_page(_card())
        for m in re.finditer(r"(\d+)%\s+" + share, html):
            window = html[max(0, m.start() - 160):m.start() + 160]
            others = {"positive", "neutral", "negative"} - {share}
            assert all(o in window for o in others), (
                f"a lone '{share}' share appears without its siblings")


# ────────────────────────────────────────────────────────────────────────
# Structure
# ────────────────────────────────────────────────────────────────────────

class TestAnswerFirst:

    def test_answer_block_precedes_specs(self):
        """44% of citations come from the first 30% of a page, and the
        synthesis is the only thing here nobody else has."""
        html = render_page(_card())
        answer = html.index("What do reviewers say about")
        specs = html.index("specifications?")
        assert answer < specs

    def test_specs_still_present_and_not_demoted_below_the_axes(self):
        """Specs are an asset, not filler — the answer block is inserted above
        them, it does not push them down the page."""
        html = render_page(_card())
        specs = html.index("specifications?")
        axes = html.index("praise and criticize")
        assert specs < axes

    def test_most_discussed_axis_ranks_by_volume_not_sentiment(self):
        """Picking the most favourable axis instead would be an editorial
        thumb on the scale."""
        axis = most_discussed_axis(_card())
        assert axis["axis_id"] == "video_capability"

    def test_retired_heading_does_not_return(self):
        html = render_page(_card())
        assert "What reviewers agree on" not in html


class TestQuestionHeadings:

    def test_headings_are_questions(self):
        html = render_page(_card())
        for q in ("What do reviewers say about",
                  "specifications?",
                  "What do reviewers praise and criticize?",
                  "Which reviews is this based on?"):
            assert q in html

    def test_headings_degrade_without_a_display_name(self):
        """No doubled spaces, no dangling articles."""
        bare = {"identity": {}}
        assert specs_heading(bare) == "What are the full specifications?"
        assert "  " not in used_price_heading(bare)


class TestDateClaimsDescribeTheAnalysisNotTheSources:
    """The reviews are not from the year we read them.

    sources[].publication_date is present on every source and empty on every
    source — 55/55 on the live A7 IV card — so we hold NO publication dates.
    ingested_at records when we fetched. The years inferable from source-id
    slugs run 2021, 2022, 2025, 2026: a five-year-wide corpus. So a heading
    like "What do reviewers say ... in 2026?" asserts a source recency the
    evidence contradicts, in the same way a lone "32% positive" asserted a
    favourability the sentiment split contradicted.

    Every date claim therefore attaches to OUR analysis."""

    def test_answer_heading_carries_no_year(self):
        html = render_page(_card())
        heading = re.search(
            r"What do reviewers say about[^<]*", html).group(0)
        assert not re.search(r"\b(19|20)\d\d\b", heading), (
            "a year here reads as the reviews' year, not ours")

    def test_stat_line_leads_with_the_analysis_date(self):
        line = answer_stat_line(_card(), 55)
        assert line.startswith("As of June 2026"), line

    def test_asof_uses_month_grain_not_day(self):
        """A synthesis drawn from a multi-week corpus does not earn day-level
        precision; the exact build date is already on the hero line."""
        assert asof_phrase(_card()) == "As of June 2026"

    def test_asof_uses_the_cards_date_not_todays(self):
        """The card was built in June; rendering it in July must not relabel
        the analysis as current."""
        assert str(datetime.now(timezone.utc).year) in asof_phrase(_card())
        assert "June" in asof_phrase(_card())

    def test_no_date_claim_when_no_synthesis_date(self):
        """No date beats a manufactured one."""
        card = _card(freshness={"source_count": 55})
        assert asof_phrase(card) == ""
        assert "As of" not in answer_stat_line(card, 55)

    def test_title_year_describes_the_analysis(self):
        html = render_page(_card())
        title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
        assert "analyzed 2026" in title
        assert "review in 2026" not in title

    def test_h1_stays_the_bare_product_name(self):
        """The <h1> is the branded-search anchor and the schema.org name; a
        churning year there buys nothing the <title> isn't already getting."""
        html = render_page(_card())
        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S).group(1)
        assert "Sony A7 IV" in h1
        assert str(datetime.now(timezone.utc).year) not in h1

    def test_year_still_present_in_the_title(self):
        """The citation gain from a year in the title is real (~+30% per the
        spec) — it is kept, just attached to a claim we can defend."""
        html = render_page(_card())
        title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
        assert str(datetime.now(timezone.utc).year) in title


class TestClaimScope:
    """We are not exhaustive and must not read as if we were.

    Three distinct overclaims have now been caught on this one sentence, all
    of the same shape — a true token implying more than the data supports:

      1. a lone "32% positive" on a 62%-negative axis (fixed 7bcba5c),
      2. "in 2026" on a corpus spanning 2021-2026 (fixed 09d2902),
      3. "524 claims across 55 sources", exact, but reading as THE review
         landscape rather than the slice we assembled.

    Coverage and classification get separate qualifiers because they are
    separate uncertainties: we chose which reviews to compile, and a model
    chose how to label each claim. The numbers themselves stay concrete —
    softening them would cost citation value without buying honesty, since the
    counts are the one part that is not uncertain.
    """

    def test_coverage_is_scoped_to_what_we_compiled(self):
        line = answer_stat_line(_card(), 55)
        assert "reviews we compiled" in line

    def test_classification_is_attributed_to_us(self):
        line = answer_stat_line(_card(), 55)
        assert "we read as" in line

    def test_numbers_stay_concrete(self):
        """Scope qualifiers, not hedged figures."""
        line = answer_stat_line(_card(), 55)
        for token in ("524 claims", "55 reviews", "32% positive",
                      "6% neutral", "62% negative"):
            assert token in line, token
        for weasel in ("roughly", "approximately", "about 5", "~"):
            assert weasel not in line

    @pytest.mark.parametrize("claim", [
        "all reviews", "every review", "all available", "complete list",
        "exhaustive", "the internet's",
    ])
    def test_no_exhaustiveness_claim_anywhere_on_the_page(self, claim):
        html = render_page(_card()).lower()
        assert claim not in html

    def test_reads_as_a_sentence_without_a_date(self):
        """No leading lowercase when the as-of clause is absent."""
        card = _card(freshness={"source_count": 55})
        line = answer_stat_line(card, 55)
        assert line.startswith("Among the")
