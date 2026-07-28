"""relabel_axes: renaming a heading without claiming we re-analyzed anything.

Two things must hold, and they pull in opposite directions:

  - the labels must actually change, and
  - nothing else may, above all the freshness clocks. last_built asserts that
    synthesis was recomputed. Moving it to publish a better heading would be
    the same overclaim ruled out on 2026-07-27, when the consensus paragraphs
    were regenerated in place rather than rebuilt.

The third thing is the refusal. A card extracted with the wrong dictionary has
no fixable labels — there is no body name for "optical performance" — and a
tool that silently skipped those would print "0 changes" on a card that needs
re-extraction, which reads exactly like a card that was already correct. That
is the failure shape this codebase keeps meeting, so it gets its own tests.

The dictionary is faked, as in test_verify_rebuilt_card: these must pass with
no phantom-ops checkout, and pinning them to real axis names would break them
the day a genuine label is authored.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import relabel_axes as r  # noqa: E402
from verify_rebuilt_card import DictionaryUnavailable  # noqa: E402


AXES = {
    'body': {'evf_lcd': [], 'burst_buffer': [], 'sensor_performance': [],
             'price': [], 'weight': []},
    'lens/prime': {'optical_performance': [], 'bokeh': [], 'price': [],
                   'weight': []},
}
LABELS = {
    'body': {'evf_lcd': {'display': 'EVF & LCD'},
             'burst_buffer': {'display': 'Burst & Buffer'},
             'sensor_performance': {'display': 'Sensor Performance'},
             'price': {'display': 'Price & Value'},
             'weight': {'display': 'Weight & Portability'}},
    'lens/prime': {k: {'display': k.replace('_', ' ').title()}
                   for k in AXES['lens/prime']},
}


def load_axes(c):
    return AXES[c]


def load_labels(c):
    return LABELS[c]


def _card(category='body', axes=None, freshness=None):
    axes = axes if axes is not None else [
        ('evf_lcd', 'Evf Lcd'),
        ('burst_buffer', 'Burst Buffer'),
        ('price', 'Price & Value'),
    ]
    return {
        'identity': {'category': category, 'slug': 'test-card',
                     'display_name': 'Test Camera'},
        'lead_axes': [{'axis_id': a, 'display_name': d} for a, d in axes],
        'detail_axes': [],
        'freshness': freshness or {
            'created_at': '2026-07-03T15:30:13+00:00',
            'last_built': '2026-07-20T18:06:23+00:00',
            'last_checked': '2026-07-20T18:06:23+00:00',
        },
        'synthesis': {'consensus_paragraph': 'Reviewers converge on the sensor.',
                      'aspect_breakdown': []},
        'pricing': {'used_market': {'price_updated_at': '2026-07-27T10:10:00+00:00'}},
        'sources': [{'source_id': 'x'}],
        'confidence': {'overall': 'high', 'per_axis': {'evf_lcd': 'high'}},
        'facts': {'specs': {}, 'provenance': {}, 'conflicts': []},
    }


def _write(tmp_path, card, slug='test-card'):
    d = tmp_path / 'cards'
    d.mkdir(exist_ok=True)
    p = d / f'{slug}.json'
    p.write_text(json.dumps(card, indent=2), encoding='utf-8')
    return p


# ── It relabels ──────────────────────────────────────────────────────────

def test_stale_labels_are_rewritten_from_the_dictionary():
    card = _card()
    changes, blocked = r.relabel(card, load_axes, load_labels)
    assert blocked == []
    assert {c[0] for c in changes} == {'evf_lcd', 'burst_buffer'}
    assert card['lead_axes'][0]['display_name'] == 'EVF & LCD'
    assert card['lead_axes'][1]['display_name'] == 'Burst & Buffer'


def test_an_already_correct_label_is_left_alone():
    card = _card()
    changes, _ = r.relabel(card, load_axes, load_labels)
    assert 'price' not in {c[0] for c in changes}


def test_an_axis_with_no_authored_label_is_left_not_blanked():
    """sigma carries a bare af_performance that is no longer a dictionary
    axis. A relabel cannot invent a name, and must not erase the one there."""
    card = _card(axes=[('sensor_performance', 'Sensor Performance'),
                       ('mystery_axis', 'Mystery Axis')])
    card['identity']['category'] = 'body'
    AXES['body']['mystery_axis'] = []          # in axes, absent from labels
    try:
        changes, blocked = r.relabel(card, load_axes, load_labels)
        assert blocked == []
        assert changes == []
        assert card['lead_axes'][1]['display_name'] == 'Mystery Axis'
    finally:
        del AXES['body']['mystery_axis']


# ── It refuses ───────────────────────────────────────────────────────────

def test_a_mis_extracted_card_is_blocked_not_silently_skipped():
    card = _card(axes=[('optical_performance', 'Optical Performance'),
                       ('bokeh', 'Bokeh')])
    changes, blocked = r.relabel(card, load_axes, load_labels)
    assert changes == []
    assert blocked and 'foreign to body' in blocked[0]


def test_the_refusal_points_at_the_tool_that_can_fix_it():
    """'0 changes' and 'this needs re-extraction' must not look alike."""
    card = _card(axes=[('optical_performance', 'Optical Performance')])
    _, blocked = r.relabel(card, load_axes, load_labels)
    assert 'requeue_rebuild' in blocked[0]


def test_no_category_cannot_be_relabelled():
    card = _card()
    card['identity'].pop('category')
    with pytest.raises(DictionaryUnavailable):
        r.relabel(card, load_axes, load_labels)


# ── It moves nothing else ────────────────────────────────────────────────

@pytest.mark.parametrize('block', r.UNTOUCHED)
def test_relabel_disturbs_no_other_block(block):
    card = _card()
    before = json.dumps(card.get(block), sort_keys=True)
    r.relabel(card, load_axes, load_labels)
    assert json.dumps(card.get(block), sort_keys=True) == before


def test_the_freshness_clocks_do_not_move(tmp_path):
    """The ruling this tool exists under: renaming a heading re-analyzes
    nothing, so a clock that asserts re-analysis must not move."""
    p = _write(tmp_path, _card())
    r.process(p, load_axes, load_labels, apply=True)
    after = json.loads(p.read_text(encoding='utf-8'))['freshness']
    assert after['created_at'] == '2026-07-03T15:30:13+00:00'
    assert after['last_built'] == '2026-07-20T18:06:23+00:00'
    assert after['last_checked'] == '2026-07-20T18:06:23+00:00'


def test_the_guard_fires_if_a_future_edit_widens_the_blast_radius(tmp_path,
                                                                  monkeypatch):
    """process() re-checks its own work. If relabel ever starts touching a
    protected block, the write is refused rather than shipped."""
    def sloppy(card, _a, _l):
        card['freshness']['last_built'] = '2026-07-28T00:00:00+00:00'
        return [('evf_lcd', 'Evf Lcd', 'EVF & LCD')], []

    monkeypatch.setattr(r, 'relabel', sloppy)
    p = _write(tmp_path, _card())
    with pytest.raises(RuntimeError, match='refusing to write'):
        r.process(p, load_axes, load_labels, apply=True)


# ── Dry run ──────────────────────────────────────────────────────────────

def test_dry_run_is_the_default(tmp_path):
    p = _write(tmp_path, _card())
    before = p.read_text(encoding='utf-8')
    changes, _ = r.process(p, load_axes, load_labels)
    assert changes
    assert p.read_text(encoding='utf-8') == before


def test_apply_writes(tmp_path):
    p = _write(tmp_path, _card())
    r.process(p, load_axes, load_labels, apply=True)
    card = json.loads(p.read_text(encoding='utf-8'))
    assert card['lead_axes'][0]['display_name'] == 'EVF & LCD'


def test_serialization_is_byte_exact_with_the_other_in_place_writer(tmp_path):
    """indent=2, ensure_ascii=False, no trailing newline — the convention
    regenerate_synthesis.py documents. An in-place edit should diff exactly
    the lines it meant to change; a stray newline adds one every time."""
    card = _card()
    p = _write(tmp_path, card)
    r.process(p, load_axes, load_labels, apply=True)
    written = p.read_text(encoding='utf-8')
    assert not written.endswith('\n')
    expected = json.dumps(json.loads(written), indent=2, ensure_ascii=False)
    assert written == expected


# ── Exit codes ───────────────────────────────────────────────────────────

def _patched_main(monkeypatch, argv):
    monkeypatch.setattr(r, 'load_dictionary',
                        lambda _root: (load_axes, load_labels, lambda: list(AXES)))
    return r.main(argv)


def test_main_exits_1_when_a_card_needs_re_extraction(tmp_path, monkeypatch, capsys):
    _write(tmp_path, _card(axes=[('optical_performance', 'Optical Performance')]),
           slug='broken')
    rc = _patched_main(monkeypatch, ['--slug', 'broken',
                                     '--cards-dir', str(tmp_path / 'cards')])
    assert rc == 1
    assert 'need re-extraction' in capsys.readouterr().out


def test_main_exits_0_and_names_the_rebuild_step(tmp_path, monkeypatch, capsys):
    """data/cards is not the deploy. The tool says so at the point of use."""
    _write(tmp_path, _card())
    rc = _patched_main(monkeypatch, ['--slug', 'test-card', '--apply',
                                     '--cards-dir', str(tmp_path / 'cards')])
    assert rc == 0
    assert 'build_site.py' in capsys.readouterr().out


def test_missing_dictionary_exits_2(tmp_path, capsys):
    _write(tmp_path, _card())
    rc = r.main(['--slug', 'test-card', '--cards-dir', str(tmp_path / 'cards'),
                 '--dict-root', '/nonexistent/phantom-ops'])
    assert rc == 2
    assert 'CANNOT CHECK' in capsys.readouterr().out
