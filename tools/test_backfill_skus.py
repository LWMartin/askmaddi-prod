"""
Unit tests for backfill_skus — scorer + confidence gate.
========================================================
Pins the decision logic that picks WHICH eBay identity seeds each card. A wrong
pick poisons the seed cadre's join key, so the gate behavior (auto_accept vs
needs_review vs gemma) is what matters most here. Offline: _search_candidates
and resolve are monkeypatched, no creds/network.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
# backfill_skus imports ebay_api at module level, which imports `requests`.
# The bot_push gate runs this file under /usr/local/bin/python3 too, where
# (as the askmaddi user) requests is absent and the interpreter is narrowed
# to tools/ — but this tools/ test transitively needs a gateway dep. Skip
# cleanly there; 3.9 and the prod venv both have requests and run for real.
bf = pytest.importorskip('backfill_skus')  # noqa: E402
ebay_api = pytest.importorskip('ebay_api')  # noqa: E402


SONY_CARD = {
    'identity': {
        'display_name': 'Sony A7 IV',
        'brand': 'Sony',
        'model': 'A7 IV (ILCE-7M4)',
        'sku_alt_names': ['ILCE-7M4', 'Alpha 7 IV', 'a7 IV'],
    },
    'category': 'body',
}


# ─── scorer ─────────────────────────────────────────────────────────────────

def test_score_exact_product_scores_high():
    s = bf._score(SONY_CARD['identity'],
                  'Sony Alpha A7 IV ILCE-7M4 Mirrorless Camera Body')
    assert s >= bf.ACCEPT_THRESHOLD


def test_score_wrong_product_scores_low():
    # The classic confusable: a DIFFERENT Sony body must not clear the gate.
    s = bf._score(SONY_CARD['identity'],
                  'Sony Alpha A7 III ILCE-7M3 Mirrorless Camera')
    assert s < bf.ACCEPT_THRESHOLD


def test_score_accessory_scores_low():
    s = bf._score(SONY_CARD['identity'], 'Camera Bag for Mirrorless Cameras')
    assert s < bf.ACCEPT_THRESHOLD


# ─── gate: auto_accept ──────────────────────────────────────────────────────

def _patch(monkeypatch, candidates, resolved=None):
    monkeypatch.setattr(ebay_api, '_search_candidates', lambda q, limit=10: candidates)
    if resolved is not None:
        monkeypatch.setattr(ebay_api, 'resolve', lambda item_id, customid=None: resolved)


def _resolved():
    return {
        'identity': {'epid': '15042899333', 'legacy_item_id': '123',
                     'ebay_category_id': '31388', 'brand': 'Sony', 'mpn': 'ILCE-7M4',
                     'market_title': 'Sony A7 IV', 'image': 'x',
                     'price_seen': {'value': '2498', 'currency': 'USD', 'as_of': 'now'}},
        'affiliate_url': 'https://www.ebay.com/itm/1?campid=5339138080',
        '_raw': {},
    }


def test_gate_auto_accepts_strong_match(monkeypatch):
    cands = [
        {'item_id': 'v1|1|0', 'title': 'Sony Alpha A7 IV ILCE-7M4 Body', 'epid': '', 'brand': ''},
        {'item_id': 'v1|2|0', 'title': 'Random unrelated lens cap', 'epid': '', 'brand': ''},
    ]
    _patch(monkeypatch, cands, _resolved())
    entry, review = bf.backfill_card('sony-a7iv', SONY_CARD)
    assert review['decision'] == 'auto_accept'
    assert review['chosen']['item_id'] == 'v1|1|0'
    assert entry is not None
    assert entry['marketplace_ids']['ebay_epid'] == '15042899333'
    assert entry['contamination_key'] == 'sony-a7-iv'


def test_gate_no_survivors_when_all_fail_coarse_gate(monkeypatch):
    # A7 III (wrong generation) is disqualified by the generation gate; the
    # strap fails on overlap. Nothing survives -> no_survivors (distinct from
    # needs_review, which means survivors exist but are too close to call).
    cands = [
        {'item_id': 'v1|9|0', 'title': 'Sony A7 III ILCE-7M3 Body', 'epid': '', 'brand': ''},
        {'item_id': 'v1|8|0', 'title': 'Camera strap universal', 'epid': '', 'brand': ''},
    ]
    _patch(monkeypatch, cands)
    entry, review = bf.backfill_card('sony-a7iv', SONY_CARD)
    assert review['decision'] == 'no_survivors'
    assert entry is None  # nothing seeded on a failed gate — the whole point


def test_gate_no_candidates(monkeypatch):
    _patch(monkeypatch, [])
    entry, review = bf.backfill_card('sony-a7iv', SONY_CARD)
    assert review['decision'] == 'no_candidates'
    assert entry is None


def test_rejected_alternatives_recorded(monkeypatch):
    cands = [
        {'item_id': 'v1|1|0', 'title': 'Sony Alpha A7 IV ILCE-7M4 Body', 'epid': '', 'brand': ''},
        {'item_id': 'v1|2|0', 'title': 'Sony A7 III ILCE-7M3', 'epid': '', 'brand': ''},
    ]
    _patch(monkeypatch, cands, _resolved())
    _, review = bf.backfill_card('sony-a7iv', SONY_CARD)
    assert len(review['rejected']) >= 1
    assert review['rejected'][0]['title'].startswith('Sony A7 III')


# ─── gemma escalation ───────────────────────────────────────────────────────

def test_gemma_escalation_accepts_when_shim_picks(monkeypatch):
    cands = [
        {'item_id': 'v1|7|0', 'title': 'Sony ILCE 7M4 (ambiguous listing)', 'epid': '', 'brand': ''},
        {'item_id': 'v1|6|0', 'title': 'Sony body unknown', 'epid': '', 'brand': ''},
    ]
    _patch(monkeypatch, cands, _resolved())
    # Force weak scores so we fall to the gemma tier, then have gemma pick v1|7|0.
    monkeypatch.setattr(bf, '_score', lambda idn, title: 0.1)
    monkeypatch.setattr(bf, '_gemma_adjudicate', lambda idn, c: 'v1|7|0')
    entry, review = bf.backfill_card('sony-a7iv', SONY_CARD, use_gemma=True)
    assert review['decision'] == 'gemma_accept'
    assert review['gemma_used'] is True
    assert entry is not None


def test_gemma_no_match_falls_to_review(monkeypatch):
    cands = [{'item_id': 'v1|7|0', 'title': 'something', 'epid': '', 'brand': ''}]
    _patch(monkeypatch, cands)
    monkeypatch.setattr(bf, '_score', lambda idn, title: 0.1)
    monkeypatch.setattr(bf, '_gemma_adjudicate', lambda idn, c: None)
    entry, review = bf.backfill_card('sony-a7iv', SONY_CARD, use_gemma=True)
    assert review['decision'] == 'gemma_no_match'
    assert entry is None


def test_category_backfill_flagged(monkeypatch):
    lens_card = {
        'identity': {'display_name': 'Sigma 35mm f/1.4 DG DN Art II',
                     'brand': 'Sigma', 'model': '35mm f/1.4 DG DN Art II',
                     'sku_alt_names': ['340969']},
        'category': None,
    }
    cands = [{'item_id': 'v1|5|0',
              'title': 'Sigma 35mm f/1.4 DG DN Art II Lens 340969', 'epid': '', 'brand': ''}]
    _patch(monkeypatch, cands, _resolved())
    entry, review = bf.backfill_card('sigma-35-art-dg-dn-ii', lens_card)
    assert review.get('category_backfilled') == 'lens'
    assert entry['facet'] == 'lens'


# ─── 2026-06-23 lesson: coarse-gate + refuse-to-guess (real failure cases) ──

def _real(slug):
    import json as _j
    return _j.load(open(f'data/cards/{slug}.json'))['identity']


def test_generation_gate_disqualifies_wrong_generation():
    # Sigma Art II card: an Art I (DG DN Art, no II) candidate must score 0.
    idn = _real('sigma-35-art-dg-dn-ii')
    assert bf._score(idn, 'Sigma 35mm f/1.4 DG DN Art Lens for Sony E - 303965') == 0.0
    # The right Art II candidate survives.
    assert bf._score(idn, 'Sigma Art 35mm f/1.4 DG II Lens for Sony E-Mount') > 0


def test_generation_gate_disqualifies_a7_iii_for_a7_iv_card():
    idn = _real('sony-a7iv')
    assert bf._score(idn, 'Sony Alpha A7 III ILCE-7M3 Mirrorless Camera') == 0.0


def test_close_survivors_escalate_not_autopick(monkeypatch):
    # Pro tripod range: Pro / Pro Tall / base cluster within the margin ->
    # the scorer must NOT auto-pick; it escalates (needs_review w/o gemma).
    cands = [
        {'item_id': 'v1|f', 'title': 'Peak Design Pro Tall Carbon Fiber Tripod Ball Head PT-T-BK-1', 'epid': '', 'brand': ''},
        {'item_id': 'v1|g', 'title': 'Peak Design Pro Carbon Fiber Tripod Ball Head PT-T-BK-1', 'epid': '', 'brand': ''},
        {'item_id': 'v1|h', 'title': 'Peak Design Pro Tripod', 'epid': '', 'brand': ''},
    ]
    monkeypatch.setattr(bf.ebay_api, '_search_candidates', lambda q, limit=10: cands)
    card = {'identity': _real('peak-design-pro-tripod'), 'category': 'support'}
    entry, review = bf.backfill_card('peak-design-pro-tripod', card, use_gemma=False)
    assert review['decision'] == 'needs_review'   # close cluster -> refuse to guess
    assert entry is None


def test_clear_winner_auto_accepts(monkeypatch):
    # One strong match, no close runner-up -> auto_accept.
    cands = [
        {'item_id': 'v1|x', 'title': 'Sony Alpha A7 IV ILCE-7M4 Mirrorless Camera Body', 'epid': '', 'brand': ''},
        {'item_id': 'v1|y', 'title': 'Generic camera cleaning kit', 'epid': '', 'brand': ''},
    ]
    monkeypatch.setattr(bf.ebay_api, '_search_candidates', lambda q, limit=10: cands)
    monkeypatch.setattr(bf.ebay_api, 'resolve',
                        lambda item_id, customid=None: {'identity': {'epid': 'E'}, 'affiliate_url': 'u', '_raw': {}})
    card = {'identity': _real('sony-a7iv'), 'category': 'body'}
    entry, review = bf.backfill_card('sony-a7iv', card, use_gemma=False)
    assert review['decision'] == 'auto_accept'
    assert entry is not None


def test_gemma_resolves_close_survivors(monkeypatch):
    # Same close Pro cluster, but with --gemma the shim picks the base member.
    cands = [
        {'item_id': 'v1|f', 'title': 'Peak Design Pro Tall Carbon Tripod PT-T-BK-1', 'epid': '', 'brand': ''},
        {'item_id': 'v1|h', 'title': 'Peak Design Pro Tripod', 'epid': '', 'brand': ''},
    ]
    monkeypatch.setattr(bf.ebay_api, '_search_candidates', lambda q, limit=10: cands)
    monkeypatch.setattr(bf.ebay_api, 'resolve',
                        lambda item_id, customid=None: {'identity': {'epid': 'E'}, 'affiliate_url': 'u', '_raw': {}})
    monkeypatch.setattr(bf, '_gemma_adjudicate', lambda idn, c: 'v1|h')  # base member
    card = {'identity': _real('peak-design-pro-tripod'), 'category': 'support'}
    entry, review = bf.backfill_card('peak-design-pro-tripod', card, use_gemma=True)
    assert review['decision'] == 'gemma_accept'
    assert review['chosen']['item_id'] == 'v1|h'
    assert entry is not None


# ─── Gemma shim contract: text-in/text-out /orient (real wiring) ────────────

def test_gemma_prompt_numbers_candidates():
    cands = [{'item_id': 'v1|a', 'title': 'Peak Design Pro Tripod'},
             {'item_id': 'v1|b', 'title': 'Peak Design Pro Tall Tripod'}]
    p = bf._build_gemma_prompt(_real('peak-design-pro-tripod'), cands)
    assert '1. Peak Design Pro Tripod' in p
    assert '2. Peak Design Pro Tall Tripod' in p
    assert 'single number' in p.lower()


def test_gemma_adjudicate_parses_numbered_reply(monkeypatch):
    cands = [{'item_id': 'v1|a', 'title': 'Pro Tripod'},
             {'item_id': 'v1|b', 'title': 'Pro Tall Tripod'}]

    class _Resp:
        status_code = 200
        def json(self): return {'text': '1'}      # shim returns text, not item_id

    import requests
    monkeypatch.setattr(requests, 'post', lambda *a, **k: _Resp())
    chosen = bf._gemma_adjudicate({'brand': 'Peak Design', 'model': 'Pro Tripod'}, cands)
    assert chosen == 'v1|a'                          # number 1 -> first candidate


def test_gemma_adjudicate_zero_means_none(monkeypatch):
    cands = [{'item_id': 'v1|a', 'title': 'x'}]

    class _Resp:
        status_code = 200
        def json(self): return {'text': '0'}        # 0 = none qualifies

    import requests
    monkeypatch.setattr(requests, 'post', lambda *a, **k: _Resp())
    assert bf._gemma_adjudicate({'model': 'y'}, cands) is None


def test_gemma_adjudicate_unparseable_falls_to_none(monkeypatch):
    cands = [{'item_id': 'v1|a', 'title': 'x'}]

    class _Resp:
        status_code = 200
        def json(self): return {'text': 'I think none of these match well.'}

    import requests
    monkeypatch.setattr(requests, 'post', lambda *a, **k: _Resp())
    assert bf._gemma_adjudicate({'model': 'y'}, cands) is None


def test_gemma_adjudicate_shim_down_falls_to_none(monkeypatch):
    import requests
    def _boom(*a, **k): raise requests.exceptions.ConnectionError('refused')
    monkeypatch.setattr(requests, 'post', _boom)
    assert bf._gemma_adjudicate({'model': 'y'}, [{'item_id': 'v1|a', 'title': 'x'}]) is None


# ─── Gemma adjudicator speaks the REAL shim contract ({prompt}->{text}) ─────

def test_gemma_adjudicator_parses_real_text_response(monkeypatch):
    # The shim is text-in/text-out: POST {prompt,...} -> {"text": "..."}.
    # Adjudicator must build a numbered prompt and parse the integer reply,
    # mapping it back to the candidate item_id. Guards against regressing to a
    # structured {item_id} contract the shim does not speak.
    cands = [{'item_id': 'v1|f', 'title': 'Peak Design Pro Tall Carbon Tripod'},
             {'item_id': 'v1|h', 'title': 'Peak Design Pro Tripod'}]
    idn = {'brand': 'Peak Design', 'model': 'Pro Tripod', 'sku_alt_names': ['Pro Tall Tripod']}

    class _R:
        status_code = 200
        def json(self):
            return {'text': '2'}   # Gemma picks listing #2

    import requests
    monkeypatch.setattr(requests, 'post', lambda *a, **k: _R())
    assert bf._gemma_adjudicate(idn, cands) == 'v1|h'


def test_gemma_adjudicator_zero_means_none(monkeypatch):
    cands = [{'item_id': 'v1|f', 'title': 'x'}, {'item_id': 'v1|h', 'title': 'y'}]
    idn = {'brand': 'B', 'model': 'M', 'sku_alt_names': []}

    class _R:
        status_code = 200
        def json(self):
            return {'text': '0'}   # none qualifies

    import requests
    monkeypatch.setattr(requests, 'post', lambda *a, **k: _R())
    assert bf._gemma_adjudicate(idn, cands) is None


def test_gemma_adjudicator_shim_down_returns_none(monkeypatch):
    cands = [{'item_id': 'v1|f', 'title': 'x'}]
    idn = {'brand': 'B', 'model': 'M', 'sku_alt_names': []}

    import requests
    def _boom(*a, **k):
        raise requests.exceptions.ConnectionError('refused')
    monkeypatch.setattr(requests, 'post', _boom)
    assert bf._gemma_adjudicate(idn, cands) is None   # falls to review, no raise


# ─── slug gate (2026-06-24 wire flag): hard-reject ambiguous/colliding slugs ─
# The gate runs in backfill_card before build_entry. For the seed cadre it is a
# no-op (identity resolves to the frozen slug against the live data/skus.json).
# For a new/ambiguous/colliding slug it raises SlugRejected — nothing is seeded.
# We drive the reject paths by patching slug_normalizer.resolve_slug so the test
# is deterministic and does not depend on live registry contents.

import slug_normalizer as _sn  # noqa: E402


def _resolution(slug, source, needs_review, collision=None):
    return _sn.SlugResolution(slug=slug, source=source, input_text='x',
                              needs_review=needs_review, collision=collision)


def test_gate_passes_for_frozen_cadre_slug(monkeypatch):
    # Real path, no patch: sony-a7iv resolves to the frozen slug via the live
    # registry → gate is a no-op and the strong match seeds as before.
    cands = [{'item_id': 'v1|1|0', 'title': 'Sony Alpha A7 IV ILCE-7M4 Body',
              'epid': '', 'brand': ''}]
    _patch(monkeypatch, cands, _resolved())
    entry, review = bf.backfill_card('sony-a7iv', SONY_CARD)
    assert review['decision'] == 'auto_accept'
    assert review.get('slug_gate') == 'override'   # resolved as a frozen fact
    assert entry is not None


def test_gate_rejects_colliding_slug(monkeypatch):
    # The chosen item passes the scorer, but the slug collides with an existing
    # spine slug under normalization (sony-a7iv ~ sony-a7-iv class). HARD reject:
    # backfill_card raises, nothing is seeded.
    cands = [{'item_id': 'v1|1|0', 'title': 'Sony Alpha A7 IV ILCE-7M4 Body',
              'epid': '', 'brand': ''}]
    _patch(monkeypatch, cands, _resolved())
    monkeypatch.setattr(bf.slug_normalizer, 'resolve_slug',
                        lambda v, m, override=None, **k:
                        _resolution('sony-a7-iv', 'override', False, collision='sony-a7iv'))
    with __import__('pytest').raises(bf.SlugRejected) as ei:
        bf.backfill_card('sony-a7-iv', SONY_CARD)
    assert 'sony-a7iv' in str(ei.value)


def test_gate_rejects_unreviewed_generated_slug(monkeypatch):
    # NOTE: from backfill_card this branch is NOT reachable — backfill always
    # passes the card's authored filename slug as the override, and an override
    # always resolves needs_review=False. So we test _gate_slug DIRECTLY with no
    # authored slug, the way the FUTURE route writer (no filename to lean on)
    # will call it: a generated proposal with no override → HARD reject.
    monkeypatch.setattr(bf.slug_normalizer, 'resolve_slug',
                        lambda v, m, override=None, **k:
                        _resolution('tamron-28-75mm-f-2-8-g2', 'generated', True))
    with __import__('pytest').raises(bf.SlugRejected) as ei:
        # slug='' so chosen_override is falsy → resolve_slug sees override=None,
        # exactly the no-authored-slug case the route will hit.
        bf._gate_slug('', 'Tamron', '28-75mm f/2.8 G2')
    assert 'UNREVIEWED' in str(ei.value)


def test_gate_override_confirms_new_slug(monkeypatch):
    # The operator re-runs with --override: the same slug, now confirmed and
    # collision-free, resolves as an override fact → gate passes, card seeds.
    cands = [{'item_id': 'v1|1|0', 'title': 'Tamron 28-75 G2', 'epid': '', 'brand': ''}]
    _patch(monkeypatch, cands, _resolved())
    monkeypatch.setattr(bf.slug_normalizer, 'resolve_slug',
                        lambda v, m, override=None, **k:
                        _resolution(override or 'x', 'override', False, collision=None))
    monkeypatch.setattr(bf, '_score', lambda idn, title: 0.9)  # force clear winner
    entry, review = bf.backfill_card(
        'tamron-28-75-g2',
        {'identity': {'display_name': 'Tamron 28-75 G2',
                      'brand': 'Tamron', 'model': '28-75mm f/2.8 G2'},
         'category': 'lens'},
        override='tamron-28-75-g2')
    assert entry is not None
    assert review.get('slug_gate') == 'override'
