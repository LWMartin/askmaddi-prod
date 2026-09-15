"""Tests for build_axis_pages — the evidence-gated per-axis micro-pages."""
import json
import re

import build_axis_pages as ax


def _axis(aid, pos, neu, neg, total, nsources, face=None):
    srcs = [{"source_id": f"s{i}", "source_type": "youtube",
             "url": f"https://x/{i}", "weight": 1.0 - i * 0.1,
             "quote_excerpt": f"take number {i} about it"} for i in range(nsources)]
    return {"axis_id": aid, "display_name": aid.replace("_", " ").title(),
            "face_quote": face,
            "sentiment": {"pos": pos, "neu": neu, "neg": neg, "total": total,
                          "sources": srcs}}


def _card(cid="sony-a7iv", axes=None):
    return {"card_id": cid, "identity": {"display_name": "Sony A7 IV"},
            "sources": [], "lead_axes": axes or [], "detail_axes": []}


def test_gate_skips_thin_axes(tmp_path):
    card = _card(axes=[
        _axis("video_capability", 65, 25, 17, 107, 6),   # substantial → page
        _axis("battery_life", 1, 0, 1, 2, 1),            # thin → skipped
    ])
    urls = ax.build_pages([card], str(tmp_path), "https://x.com")
    assert any("video_capability" in u for u in urls)
    assert not any("battery_life" in u for u in urls)
    assert (tmp_path / "aspect" / "sony-a7iv" / "video_capability" / "index.html").exists()
    assert not (tmp_path / "aspect" / "sony-a7iv" / "battery_life").exists()


def test_counts_rendered_no_rating(tmp_path):
    card = _card(axes=[_axis("autofocus_body", 40, 10, 5, 55, 5)])
    ax.build_pages([card], str(tmp_path), "https://x.com")
    raw = (tmp_path / "aspect" / "sony-a7iv" / "autofocus_body" / "index.html").read_text()
    assert "40" in raw and "critical" in raw          # counts present
    prose = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    prose = re.sub(r"<[^>]+>", " ", prose).lower()
    for banned in ("best", "top", "#1", "winner", "worth it"):
        assert banned not in prose


def test_face_quote_leads_and_dedups(tmp_path):
    face = {"source_id": "face", "quote_excerpt": "the standout curated take",
            "url": "https://x/face"}
    card = _card(axes=[_axis("handling", 30, 5, 5, 40, 4, face=face)])
    ax.build_pages([card], str(tmp_path), "https://x.com")
    body = (tmp_path / "aspect" / "sony-a7iv" / "handling" / "index.html").read_text()
    assert "standout curated take" in body            # face_quote leads
    # <= MAX_QUOTES figures, distinct sources
    assert body.count("<figure") <= ax.MAX_QUOTES


def test_lead_detail_axis_dedup(tmp_path):
    a = _axis("image_quality", 20, 5, 3, 28, 4)
    card = {"card_id": "x", "identity": {"display_name": "X"}, "sources": [],
            "lead_axes": [a], "detail_axes": [dict(a)]}  # same axis in both
    urls = ax.build_pages([card], str(tmp_path), "https://x.com")
    assert sum("image_quality" in u for u in urls) == 1  # one page, not two
