"""verify_rebuilt_card: the pre-publish check for a card the eye cannot audit.

The tool's job is to refuse a card that looks finished and is not, so these
cover the ways it could fail to refuse:

  - a body extracted with the lens dictionary must FAIL, and must name the
    dictionary the foreign axes came from
  - a card carrying only universal axes must FAIL, because price and weight
    are on everything and prove nothing about which dictionary was loaded
  - a missing dictionary must exit 2, never 0 — a verifier whose silence can
    mean either "passed" or "did not run" is worse than no verifier, which is
    the requeue_rebuild lesson from 2026-07-27
  - the axis sets must come from the dictionary, so a new category becomes
    checkable without editing the tool

The dictionary is faked here rather than imported. These tests must pass in
CI and in a sandbox with no phantom-ops checkout, and pinning them to the
real dictionary would make them fail the day a genuine axis is authored.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_rebuilt_card as v  # noqa: E402


# ── A fake dictionary with the same shape as the real one ────────────────

AXES = {
    'body': {'sensor_performance': [], 'evf_lcd': [], 'battery_life': [],
             'price': [], 'weight': [], 'build': []},
    'lens/prime': {'optical_performance': [], 'bokeh': [], 'vignetting': [],
                   'price': [], 'weight': [], 'build': []},
    'support': {'stability': [], 'price': [], 'weight': [], 'build': []},
}
LABELS = {
    'body': {'sensor_performance': {'display': 'Sensor Performance'},
             'evf_lcd': {'display': 'EVF & LCD'},
             'battery_life': {'display': 'Battery Life'},
             'price': {'display': 'Price & Value'},
             'weight': {'display': 'Weight & Portability'},
             'build': {'display': 'Build Quality'}},
    'lens/prime': {k: {'display': k.replace('_', ' ').title()}
                   for k in AXES['lens/prime']},
    'support': {k: {'display': k.replace('_', ' ').title()}
                for k in AXES['support']},
}


def fake_loader(_dict_root=None):
    return (lambda c: AXES[c], lambda c: LABELS[c], lambda: list(AXES))


def _card(category='body', axes=('sensor_performance', 'evf_lcd', 'price'),
          labels=None, created='2026-06-19T10:00:00+00:00',
          last_built='2026-07-28T04:00:00+00:00', paragraph='',
          staleness=False):
    labels = labels or {}
    lead = [{'axis_id': a,
             'display_name': labels.get(a, LABELS[category].get(a, {}).get('display'))}
            for a in axes]
    freshness = {'created_at': created, 'last_built': last_built}
    if staleness:
        freshness['staleness_days'] = 0
    return {
        'identity': {'category': category, 'slug': 'test-card'},
        'lead_axes': lead,
        'detail_axes': [],
        'freshness': freshness,
        'synthesis': {'consensus_paragraph': paragraph, 'aspect_breakdown': []},
    }


def run(card, published=None):
    return v.check(card, published, dict_root='/nonexistent', loader=fake_loader)


# ── Vocabulary: the defect this exists for ───────────────────────────────

def test_body_extracted_with_the_lens_dictionary_fails():
    card = _card(axes=('optical_performance', 'bokeh', 'vignetting', 'price'))
    fails, _, _ = run(card)
    assert any('foreign to body' in f for f in fails)


def test_the_failure_names_where_the_foreign_axes_came_from():
    """Diagnosis, not just detection: 'lens/prime' in the message is what
    tells the operator this was the driver bug and not schema drift."""
    card = _card(axes=('optical_performance', 'bokeh'))
    fails, _, _ = run(card)
    assert any('lens/prime' in f for f in fails)


def test_only_universal_axes_is_a_failure():
    """price, weight and build are on every category. A card carrying just
    those is evidence of nothing, and must not read as a pass."""
    card = _card(axes=('price', 'weight', 'build'))
    fails, _, _ = run(card)
    assert any('exclusive to body' in f for f in fails)


def test_a_correctly_extracted_body_passes():
    fails, _, _ = run(_card())
    assert fails == []


def test_a_lens_and_a_tripod_pass_on_their_own_dictionaries():
    """The tool is not body-specific. It reads the card's own category."""
    assert run(_card('lens/prime', ('optical_performance', 'bokeh')))[0] == []
    assert run(_card('support', ('stability', 'price')))[0] == []


def test_new_categories_need_no_edit_to_this_tool():
    """The axis sets are derived. Authoring a category in phantom-ops makes
    it checkable here without a commit in this repo."""
    AXES['drone'] = {'flight_time': [], 'price': [], 'weight': [], 'build': []}
    LABELS['drone'] = {k: {'display': k.replace('_', ' ').title()}
                       for k in AXES['drone']}
    try:
        assert run(_card('drone', ('flight_time', 'price')))[0] == []
    finally:
        del AXES['drone'], LABELS['drone']


# ── Labels ───────────────────────────────────────────────────────────────

def test_dotted_axis_id_as_a_label_fails():
    """This string reaches card prose, the meta description and schema.org."""
    card = _card(axes=('sensor_performance',),
                 labels={'sensor_performance': 'Af Performance.Video'})
    fails, _, _ = run(card)
    assert any('raw axis id leaking' in f for f in fails)


def test_a_stale_label_warns_rather_than_blocks():
    """A heading that reads 'Evf Lcd' is worth fixing and is not worth
    holding a correct card at the gate for."""
    card = _card(axes=('evf_lcd',), labels={'evf_lcd': 'Evf Lcd'})
    fails, warns, _ = run(card)
    assert fails == []
    assert any('EVF & LCD' in w for w in warns)


# ── Freshness ────────────────────────────────────────────────────────────

def test_a_reset_mint_date_fails():
    new = _card(created='2026-07-28T04:00:00+00:00')
    live = _card(created='2026-06-19T10:00:00+00:00')
    fails, _, _ = run(new, live)
    assert any('mint date moved' in f for f in fails)


def test_a_preserved_mint_date_passes():
    fails, _, notes = run(_card(), _card())
    assert fails == []
    assert any('mint date preserved' in n for n in notes)


def test_staleness_days_fails():
    fails, _, _ = run(_card(staleness=True))
    assert any('staleness_days' in f for f in fails)


# ── Grounding ────────────────────────────────────────────────────────────

def test_a_lone_share_fails():
    card = _card(paragraph='Reviewers are 32% positive on video capability.')
    fails, _, _ = run(card)
    assert any('without its pos/neu/neg' in f for f in fails)


def test_a_grounded_triple_passes():
    card = _card(paragraph='Among the 55 reviews we compiled, video capability '
                           'reads as 32% positive, 6% neutral, 62% negative.')
    fails, _, notes = run(card)
    assert fails == []
    assert any('grounded share triple' in n for n in notes)


def test_prose_with_no_numbers_is_fine():
    card = _card(paragraph='Reviewers converge on the sensor and split on video.')
    assert run(card)[0] == []


@pytest.mark.parametrize('phrase', v.EXHAUSTIVE)
def test_exhaustiveness_language_fails(phrase):
    card = _card(paragraph=f'We read {phrase} of this camera.')
    fails, _, _ = run(card)
    assert any('exhaustiveness' in f for f in fails)


# ── Refusing to report a result it did not compute ───────────────────────

def test_missing_dictionary_raises_rather_than_passing():
    with pytest.raises(v.DictionaryUnavailable):
        v.check(_card(), None, dict_root='/nonexistent/phantom-ops')


def test_missing_dictionary_exits_2_not_0(tmp_path, capsys):
    """Exit 2 is 'could not check'. Exit 0 would be a lie, and 1 would send
    an operator hunting a card defect that is really a path problem."""
    p = tmp_path / 'card.json'
    p.write_text(json.dumps(_card()), encoding='utf-8')
    rc = v.main([str(p), '--dict-root', '/nonexistent/phantom-ops'])
    assert rc == 2
    assert 'CANNOT CHECK' in capsys.readouterr().out


def test_a_card_without_identity_category_cannot_be_checked():
    """Deprecated top-level `category` is null on most cards; identity is the
    authority. No category means no dictionary, which is exit 2 territory."""
    card = _card()
    card['identity'].pop('category')
    with pytest.raises(v.DictionaryUnavailable):
        run(card)


# ── Exit codes ───────────────────────────────────────────────────────────

def test_main_exits_1_on_a_bad_card(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(v, 'load_dictionary', fake_loader)
    p = tmp_path / 'card.json'
    p.write_text(json.dumps(_card(axes=('optical_performance',))), encoding='utf-8')
    assert v.main([str(p)]) == 1
    assert 'do NOT approve' in capsys.readouterr().out


def test_main_exits_0_on_a_good_card(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(v, 'load_dictionary', fake_loader)
    p = tmp_path / 'card.json'
    p.write_text(json.dumps(_card()), encoding='utf-8')
    assert v.main([str(p)]) == 0
    assert 'safe to approve' in capsys.readouterr().out
