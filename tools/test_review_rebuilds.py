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
