"""Holdout gate for build_site.order_cards_for_grid — homepage grid ordering (Area A).

Authored BLIND by the spec-writer, proven satisfiable by a scratch impl that was
then reverted. The worker adds order_cards_for_grid to tools/build_site.py and
calls it where the cards-manifest 'cards' list is built; this file is the
independent outer gate it never sees.

Contract:
  order_cards_for_grid(cards) -> list   (new list; input not mutated)
    - HERO first: the single newest-minted card (max freshness.created_at);
      ties broken by ascending card_id.
    - Then the REMAINING cards grouped by identity.category in the fixed order
      body -> lens -> support -> drone -> other (anything not
      body/lens/support/drone), and within each group newest-minted first
      (tie -> ascending card_id).
    - The hero appears exactly ONCE (pulled out; not duplicated in its group).
    - Total, deterministic: len(out) == len(cards); missing created_at sinks to
      the end of its group; missing/unknown category -> the 'other' group.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import build_site  # noqa: E402


def _card(cid, minted, category):
    """Minimal card carrying only the fields the ordering reads."""
    fresh = {} if minted is None else {"created_at": minted}
    idy = {} if category is None else {"category": category}
    return {"card_id": cid, "freshness": fresh, "identity": idy}


def _fixture():
    # Deliberately shuffled input order; ordering must not depend on it.
    return [
        _card("l2", "2026-06-01T00:00:00+00:00", "lens"),
        _card("b1", "2026-07-20T00:00:00+00:00", "body"),     # newest overall -> HERO
        _card("s1", "2026-07-18T00:00:00+00:00", "support"),
        _card("b3", "2026-07-10T00:00:00+00:00", "body"),     # ties b2 on minted
        _card("x1", "2026-07-19T00:00:00+00:00", "flash"),    # unknown category
        _card("b2", "2026-07-10T00:00:00+00:00", "body"),     # tie -> card_id b2 < b3
        _card("l1", "2026-07-15T00:00:00+00:00", "lens"),
        _card("d1", "2026-07-12T00:00:00+00:00", "drone"),    # drone group: after support
        _card("b4", None, "body"),                            # no created_at -> sinks in body
    ]


def _ids(cards):
    return [c["card_id"] for c in cards]


def test_full_order_is_exact():
    out = build_site.order_cards_for_grid(_fixture())
    assert _ids(out) == ["b1", "b2", "b3", "b4", "l1", "l2", "s1", "d1", "x1"]


def test_hero_is_newest_minted():
    out = build_site.order_cards_for_grid(_fixture())
    assert out[0]["card_id"] == "b1"


def test_groups_follow_body_lens_support_other():
    out = build_site.order_cards_for_grid(_fixture())
    rank = {"body": 0, "lens": 1, "support": 2, "drone": 3}
    # after the hero, category rank is non-decreasing (unknown -> last)
    seen = [rank.get((c["identity"].get("category") or "").lower(), 4) for c in out[1:]]
    assert seen == sorted(seen)


def test_newest_first_within_group_and_tie_by_card_id():
    out = build_site.order_cards_for_grid(_fixture())
    # body group after hero b1: b2 and b3 tie on minted -> card_id asc, then b4 (no date) last
    body_ids = [c["card_id"] for c in out if (c["identity"].get("category") or "") == "body"]
    assert body_ids == ["b1", "b2", "b3", "b4"]


def test_hero_not_duplicated_and_total_preserved():
    src = _fixture()
    out = build_site.order_cards_for_grid(src)
    assert len(out) == len(src)
    assert sorted(_ids(out)) == sorted(_ids(src))   # same set, no drops/dupes


def test_input_not_mutated():
    src = _fixture()
    before = _ids(src)
    build_site.order_cards_for_grid(src)
    assert _ids(src) == before


def test_empty_is_empty():
    assert build_site.order_cards_for_grid([]) == []


# ─── homepage recent-cards SSR injection ─────────────────────────────────────

_INDEX_TEMPLATE = (
    "<html><body>\n"
    "<nav><ul>\n"
    "            <!-- RECENT-CARDS:START -->\n"
    "            <!-- RECENT-CARDS:END -->\n"
    "</ul></nav>\n"
    "<footer>keep me</footer>\n</body></html>\n"
)


def _named(cid, minted, category, name):
    c = _card(cid, minted, category)
    c["identity"]["display_name"] = name
    return c


def test_inject_fills_region_with_real_links_in_grid_order(tmp_path):
    (tmp_path / "index.html").write_text(_INDEX_TEMPLATE, encoding="utf-8")
    cards = [
        _named("b1", "2026-07-20T00:00:00+00:00", "body", "Sony A7 V"),
        _named("l1", "2026-07-15T00:00:00+00:00", "lens", "Canon 35mm"),
    ]
    build_site.inject_recent_cards(str(tmp_path), cards)
    html = (tmp_path / "index.html").read_text()
    assert '<li><a href="/cards/b1/">Sony A7 V</a></li>' in html
    assert '<li><a href="/cards/l1/">Canon 35mm</a></li>' in html
    # hero (newest) first, and the surrounding page is untouched.
    assert html.index("/cards/b1/") < html.index("/cards/l1/")
    assert "<footer>keep me</footer>" in html


def test_inject_is_idempotent_and_escapes(tmp_path):
    (tmp_path / "index.html").write_text(_INDEX_TEMPLATE, encoding="utf-8")
    cards = [_named("x1", "2026-07-20T00:00:00+00:00", "body", "A & B <cam>")]
    build_site.inject_recent_cards(str(tmp_path), cards)
    once = (tmp_path / "index.html").read_text()
    build_site.inject_recent_cards(str(tmp_path), cards)
    twice = (tmp_path / "index.html").read_text()
    assert once == twice                      # re-running does not drift
    assert "A &amp; B &lt;cam&gt;" in twice   # name is HTML-escaped
    assert once.count("RECENT-CARDS:START") == 1


def test_inject_noops_without_markers(tmp_path, capsys):
    (tmp_path / "index.html").write_text("<html>no markers</html>", encoding="utf-8")
    result = build_site.inject_recent_cards(str(tmp_path), [])
    assert result is None
    assert "markers absent" in capsys.readouterr().err
    assert (tmp_path / "index.html").read_text() == "<html>no markers</html>"
