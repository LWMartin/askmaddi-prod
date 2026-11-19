"""review_rebuilds: lists only review_ready, labels roster vs drip, prints the
sidecar's dropped titles, counts a missing card as could-not-check."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_rebuilds as rr  # noqa: E402


def _setup(tmp_path):
    spool = tmp_path / 'spool' / 'a'
    spool.mkdir(parents=True)
    (spool / 'card.json').write_text('{}')
    (spool / 'relevance_sidecar.json').write_text(json.dumps({
        'mode': 'enforce', 'total_sources': 3, 'kept': 2, 'dropped': 1,
        'reason_counts': {'confusable_headline': 1},
        'near_miss': [{'decision': 'dropped', 'reason': 'confusable_headline',
                       'confusable_hits': ['sib x'], 'title': 'Sib X review'}]}))
    plans = tmp_path / 'plans'
    plans.mkdir()
    (plans / '2026-11-17-gated-rebuild.json').write_text(json.dumps({'slugs': ['a']}))
    q = tmp_path / 'q.json'
    q.write_text(json.dumps({'queue': {
        'a': {'state': 'review_ready', 'card_path': str(spool / 'card.json')},
        'b': {'state': 'review_ready', 'card_path': str(tmp_path / 'nope.json')},
        'c': {'state': 'promoted', 'card_path': str(spool / 'card.json')}}}))
    return q, plans


def test_digest(tmp_path, capsys, monkeypatch):
    q, plans = _setup(tmp_path)
    monkeypatch.setattr(rr, 'verify', lambda c, p: (0, ['VERDICT: safe to approve']))
    assert rr.main(['--queue', str(q), '--plans-dir', str(plans),
                    '--cards-dir', str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert '2 card(s) review_ready' in out
    assert '== a  [roster 2026-11-17-gated-rebuild.json]' in out
    assert '== b  [drip (not on a roster)]' in out
    assert '== c' not in out
    assert 'drop confusable_headline [sib x] Sib X review' in out
    assert 'summary: 1 safe, 0 do not approve, 1 could not check' in out


def test_no_sidecar_is_said(tmp_path):
    (tmp_path / 'card.json').write_text('{}')
    assert 'sidecar: none' in rr.sidecar_lines(tmp_path / 'card.json', 80)[0]


def test_parked_regathers_listed_with_delist_line(tmp_path, capsys, monkeypatch):
    cards = tmp_path / 'cards'
    cards.mkdir()
    (cards / 'thin-live.json').write_text('{}')
    q = tmp_path / 'q.json'
    q.write_text(json.dumps({'queue': {
        'thin-live': {'state': 'corpus_thin', 'requeued_at': 't',
                      'requeued_from': 'promoted', 'last_error': 'own reviews 3 < 6'},
        'thin-gone': {'state': 'corpus_thin', 'requeued_at': 't',
                      'requeued_from': 'promoted'},
        'thin-mint': {'state': 'corpus_thin', 'requeued_at': 't',
                      'requeued_from': 'failed'},
        'thin-unknown': {'state': 'corpus_thin', 'requeued_at': 't'},
        'crashed': {'state': 'failed', 'requeued_at': 't', 'last_error': 'boom'},
        'old-mint': {'state': 'corpus_thin'},          # never re-opened
        'still-going': {'state': 'resolved', 'requeued_at': 't'}}}))
    monkeypatch.setattr(rr, 'verify', lambda c, p: (0, []))
    assert rr.main(['--queue', str(q), '--plans-dir', str(tmp_path),
                    '--cards-dir', str(cards)]) == 0
    out = capsys.readouterr().out
    assert '4 re-opened card(s) parked corpus_thin, 1 failed' in out
    assert 'thin   thin-live  [live card up; from promoted]  own reviews 3 < 6' in out
    assert 'thin   thin-gone  [no live card; from promoted]' in out
    assert 'thin   thin-mint  [no live card; from failed]' in out
    assert 'failed crashed  [no live card; from ?]  boom' in out
    assert 'old-mint' not in out and 'still-going' not in out
    delist = [ln for ln in out.splitlines() if 'delist_card.py' in ln]
    assert delist == ['  delist (dry run; add --apply to write): '
                      'sudo -u askmaddi python3 tools/delist_card.py '
                      '--slug thin-gone --slug thin-live']
    assert out.rstrip().endswith('is the door): thin-mint thin-unknown')


def test_only_never_cards_prints_no_delist_line():
    """2026-11-19: om-system-20mm-f1-4 re-opened from failed, both fetch legs
    empty, no card ever — the digest offered to delist it. It must not."""
    lines = rr.parked_lines({'om-system-20mm-f1-4': {
        'state': 'corpus_thin', 'requeued_at': 't', 'requeued_from': 'failed'}},
        '/nonexistent', 80)
    assert not any('delist_card.py' in ln for ln in lines)
    assert lines[-1].endswith('om-system-20mm-f1-4')


def test_no_parked_no_delist_line(tmp_path):
    lines = rr.parked_lines({'a': {'state': 'review_ready'}}, tmp_path, 80)
    assert lines == ['0 re-opened card(s) parked corpus_thin, 0 failed']
