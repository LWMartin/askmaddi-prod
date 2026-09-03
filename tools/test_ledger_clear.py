"""ledger_clear: clear stale resolve-attempts entries so delisted SKUs re-mint.

Covers selection (slugs vs pattern vs decontaminated-only), the aerial default,
dry-run-writes-nothing, and that --apply removes only matched keys.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger_clear as lc  # noqa: E402


def _ledger(tmp_path, data):
    p = tmp_path / 'resolve-attempts.json'
    p.write_text(json.dumps(data))
    return p


LEDGER = {
    'dji-mavic-4-pro': 'decontaminated',
    'dji-mini-2': '2026-09-03T10:00:00+00:00',
    'sony-a7-iv': 'decontaminated',
    'sigma-35mm-f1-4': '2026-09-01T10:00:00+00:00',
}


def test_select_aerial_pattern_default():
    hits = lc.select(LEDGER, pattern=lc._DEFAULT_AERIAL)
    assert set(hits) == {'dji-mavic-4-pro', 'dji-mini-2'}     # aerial only
    assert 'sony-a7-iv' not in hits and 'sigma-35mm-f1-4' not in hits


def test_select_explicit_slugs():
    hits = lc.select(LEDGER, slugs=['sony-a7-iv'])
    assert hits == ['sony-a7-iv']


def test_select_decontaminated_only():
    hits = lc.select(LEDGER, pattern=lc._DEFAULT_AERIAL, decontaminated_only=True)
    assert hits == ['dji-mavic-4-pro']                        # skips the cooling dji-mini-2


def test_dry_run_writes_nothing(tmp_path):
    p = _ledger(tmp_path, dict(LEDGER))
    lc.main(['--ledger-path', str(p)])                        # no --apply
    assert json.loads(p.read_text()) == LEDGER                # untouched


def test_apply_removes_only_aerial(tmp_path):
    p = _ledger(tmp_path, dict(LEDGER))
    lc.main(['--ledger-path', str(p), '--apply'])             # aerial default
    after = json.loads(p.read_text())
    assert 'dji-mavic-4-pro' not in after and 'dji-mini-2' not in after
    assert 'sony-a7-iv' in after and 'sigma-35mm-f1-4' in after  # non-aerial kept


def test_apply_slugs_file(tmp_path):
    p = _ledger(tmp_path, dict(LEDGER))
    lst = tmp_path / 'slugs.txt'
    lst.write_text('# clear these\ndji-mavic-4-pro\n\nsony-a7-iv\n')
    lc.main(['--ledger-path', str(p), '--slugs-file', str(lst), '--apply'])
    after = json.loads(p.read_text())
    assert 'dji-mavic-4-pro' not in after and 'sony-a7-iv' not in after
    assert 'dji-mini-2' in after     # slugs-file given → aerial default NOT auto-applied
