"""
Tests for tools/regenerate_synthesis.py.

The tool rewrites published prose, so the tests weight REFUSALS and what it
declines to touch at least as heavily as the happy path.

Two properties carry the most weight:

  * freshness is never modified. last_built asserts the synthesis was
    recomputed. It was not — the axis aggregates, the sentiment classification
    and the evidence are unchanged, and only the wording of the report is
    fixed. Moving the clock to publish a corrected sentence would be the same
    species of overclaim the corrected sentence exists to remove.

  * an empty regeneration is refused. A card whose axes cannot produce prose
    keeps the prose it has. Blanking a live card is worse than a stale
    sentence, and "fabricate nothing" cuts both ways.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import regenerate_synthesis as rs  # noqa: E402


def _card(paragraph="old text, 32% positive."):
    return {"card_id": "x",
            "synthesis": {"consensus_paragraph": paragraph,
                          "attestation": {"examined": 12}},
            "freshness": {"created_at": "2026-06-04T00:00:00+00:00",
                          "last_built": "2026-06-22T00:00:00+00:00",
                          "last_checked": "2026-06-22T00:00:00+00:00"},
            "lead_axes": [], "detail_axes": []}


class TestRegenerate:

    def test_new_prose_is_returned(self):
        out, reason = rs.regenerate(_card(), lambda c: "fresh grounded text.")
        assert out == "fresh grounded text."
        assert reason == "regenerated"

    def test_identical_output_is_not_a_rewrite(self):
        """A re-run must be a no-op, so the diff stays honest."""
        same = "old text, 32% positive."
        out, reason = rs.regenerate(_card(same), lambda c: same)
        assert out is None and reason == "already current"

    @pytest.mark.parametrize("empty", ["", "   ", "\n"])
    def test_empty_generation_is_refused(self, empty):
        out, reason = rs.regenerate(_card(), lambda c: empty)
        assert out is None
        assert "keeping existing prose" in reason

    def test_none_generation_is_refused(self):
        out, reason = rs.regenerate(_card(), lambda c: None)
        assert out is None and "keeping existing prose" in reason

    def test_generator_exception_is_caught_not_propagated(self):
        def boom(card):
            raise ValueError("bad axes")
        out, reason = rs.regenerate(_card(), boom)
        assert out is None
        assert "ValueError" in reason and "bad axes" in reason


class TestGroundednessCheck:

    @pytest.mark.parametrize("text,ok", [
        ("32% positive, 6% neutral, 62% negative", True),
        ("32% positive", False),
        ("41 positive vs 55 negative claims", False),     # counts, still drops neutral
        ("24% of 46 claims are negative", False),
        ("no numbers here at all", True),
    ])
    def test_detects_lone_shares(self, text, ok):
        assert rs._grounded(text) is ok


class TestWriting:

    def _dir(self, tmp_path, card):
        d = tmp_path / "cards"
        d.mkdir()
        p = d / "x.json"
        p.write_text(json.dumps(card, indent=2, ensure_ascii=False),
                     encoding="utf-8")
        return d, p

    def _run(self, monkeypatch, d, *extra):
        monkeypatch.setattr(rs, "load_builder",
                            lambda path: (lambda c: "regenerated café text."))
        return rs.main(["--cards-dir", str(d), *extra])

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        d, p = self._dir(tmp_path, _card())
        before = p.read_bytes()
        self._run(monkeypatch, d)
        assert p.read_bytes() == before

    def test_commit_replaces_only_the_paragraph(self, tmp_path, monkeypatch):
        d, p = self._dir(tmp_path, _card())
        self._run(monkeypatch, d, "--commit")
        after = json.loads(p.read_text(encoding="utf-8"))
        assert after["synthesis"]["consensus_paragraph"] == \
            "regenerated café text."
        assert after["synthesis"]["attestation"] == {"examined": 12}

    def test_freshness_is_never_touched(self, tmp_path, monkeypatch):
        """The load-bearing one. Nothing was re-analyzed; only re-worded."""
        card = _card()
        d, p = self._dir(tmp_path, card)
        self._run(monkeypatch, d, "--commit")
        after = json.loads(p.read_text(encoding="utf-8"))
        assert after["freshness"] == card["freshness"]

    def test_byte_exact_serialization(self, tmp_path, monkeypatch):
        d, p = self._dir(tmp_path, _card())
        self._run(monkeypatch, d, "--commit")
        raw = p.read_text(encoding="utf-8")
        assert raw == json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        assert not raw.endswith("\n")
        assert "café" in raw

    def test_missing_synthesizer_fails_loudly(self, tmp_path, monkeypatch):
        """A silent no-op reporting success is the failure mode that hides."""
        d, p = self._dir(tmp_path, _card())
        before = p.read_bytes()
        monkeypatch.setattr(rs, "load_builder", lambda path: None)
        assert rs.main(["--cards-dir", str(d), "--commit"]) == \
            rs.EXIT_NO_SYNTHESIZER
        assert p.read_bytes() == before
