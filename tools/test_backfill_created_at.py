"""
Tests for tools/backfill_created_at.py — the one-time mint-date repair.

The tool rewrites a field that is a historical FACT, recovered from git rather
than recomputed, so the tests weight two things heavily:

  * the safety invariant (created_at only ever moves backward), and
  * timestamp comparison, which is where the first dry-run actually broke.

That second one is worth stating plainly because it looked cosmetic. git's
``%aI`` emits the committer's LOCAL offset while the assembler writes UTC, so
``2026-06-22T23:00:00-06:00`` sorts BEFORE ``2026-06-23T00:30:00+00:00`` as a
string while being an hour and a half LATER in time. Comparing the raw strings
therefore picked a later timestamp as the "earliest witness" and slipped it
past the never-move-forward guard — the guard compared strings too, so it
agreed. Correcting the comparison changed a real answer: peak-design-pro-tripod
went from a bogus 1-day "repair" to correctly unchanged.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import backfill_created_at as bf  # noqa: E402


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

class TestAsDt:

    def test_offset_aware_preserved(self):
        dt = bf._as_dt('2026-06-22T23:00:00-06:00')
        assert dt == datetime(2026, 6, 23, 5, 0, tzinfo=timezone.utc)

    def test_naive_assumed_utc(self):
        assert bf._as_dt('2026-06-04T12:00:00') == \
            datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)

    @pytest.mark.parametrize('bad', [
        None, '', '   ', 'yesterday', '2026-13-45T99:99:99', 1751587200, [],
    ])
    def test_unusable_yields_none(self, bad):
        assert bf._as_dt(bad) is None


# ---------------------------------------------------------------------------
# Witness selection + the safety invariant
# ---------------------------------------------------------------------------

def _witnesses(monkeypatch, recorded=None, first_add=None):
    monkeypatch.setattr(bf, 'earliest_recorded_created_at',
                        lambda *a, **k: (recorded, 'sha'))
    monkeypatch.setattr(bf, 'first_add_date',
                        lambda *a, **k: (first_add, 'sha'))


class TestRecoverMint:

    def test_picks_the_chronologically_earlier_witness(self, monkeypatch):
        _witnesses(monkeypatch, recorded='2026-07-15T10:00:00+00:00',
                   first_add='2026-07-17T10:00:00+00:00')
        found, label, refusal = bf.recover_mint('c.json',
                                                '2026-07-23T10:00:00+00:00')
        assert found == '2026-07-15T10:00:00+00:00'
        assert label == 'recorded'
        assert refusal is None

    def test_timezone_trap_does_not_win_on_string_order(self, monkeypatch):
        """THE regression. first-add sorts earlier as text but is later in
        time; the recorded witness must win, and the offset string must not
        sneak past the forward-move guard."""
        _witnesses(monkeypatch, recorded='2026-06-23T00:30:00+00:00',
                   first_add='2026-06-22T23:00:00-06:00')
        found, label, refusal = bf.recover_mint('c.json',
                                                '2026-06-23T00:30:00+00:00')
        assert refusal is None
        assert found is None, 'stored value IS the earliest — nothing to repair'
        assert label == 'recorded'

    def test_refuses_to_move_a_mint_date_forward(self, monkeypatch):
        _witnesses(monkeypatch, recorded='2026-07-30T00:00:00+00:00')
        found, _, refusal = bf.recover_mint('c.json',
                                            '2026-07-01T00:00:00+00:00')
        assert found is None
        assert refusal and 'refusing to move a mint date forward' in refusal

    def test_forward_move_refused_across_offsets(self, monkeypatch):
        """Same refusal, expressed only in the offsets: the candidate looks
        earlier as a string and is later in fact."""
        _witnesses(monkeypatch, recorded='2026-07-01T23:00:00-10:00')
        found, _, refusal = bf.recover_mint('c.json',
                                            '2026-07-02T00:00:00+00:00')
        assert found is None
        assert refusal is not None

    def test_equal_is_unchanged_not_a_repair(self, monkeypatch):
        _witnesses(monkeypatch, recorded='2026-07-01T00:00:00+00:00')
        found, _, refusal = bf.recover_mint('c.json',
                                            '2026-07-01T00:00:00+00:00')
        assert found is None and refusal is None

    def test_no_witnesses_is_reported(self, monkeypatch):
        _witnesses(monkeypatch)
        found, label, refusal = bf.recover_mint('c.json', '2026-07-01T00:00:00+00:00')
        assert found is None and label == 'no witness' and refusal is None

    def test_unparseable_witness_is_skipped_not_selected(self, monkeypatch):
        _witnesses(monkeypatch, recorded='not-a-date',
                   first_add='2026-06-01T00:00:00+00:00')
        found, label, _ = bf.recover_mint('c.json', '2026-07-01T00:00:00+00:00')
        assert found == '2026-06-01T00:00:00+00:00'
        assert label == 'first-add'


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

class TestCommit:

    def _card(self, tmp_path, created_at):
        d = tmp_path / 'cards'
        d.mkdir()
        card = {'card_id': 'x', 'freshness': {'created_at': created_at,
                                              'last_built': created_at},
                'synthesis': {'note': 'café —'}}
        p = d / 'x.json'
        p.write_text(json.dumps(card, indent=2, ensure_ascii=False),
                     encoding='utf-8')
        return d, p

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch, capsys):
        d, p = self._card(tmp_path, '2026-07-23T00:00:00+00:00')
        before = p.read_bytes()
        monkeypatch.setattr(bf, '_rel', lambda path: 'data/cards/x.json')
        monkeypatch.setattr(bf, 'repo_is_usable', lambda *a, **k: (True, 'ok'))
        _witnesses(monkeypatch, recorded='2026-07-01T00:00:00+00:00')
        bf.main(['--cards-dir', str(d)])
        assert p.read_bytes() == before

    def test_commit_changes_only_created_at(self, tmp_path, monkeypatch):
        d, p = self._card(tmp_path, '2026-07-23T00:00:00+00:00')
        monkeypatch.setattr(bf, '_rel', lambda path: 'data/cards/x.json')
        monkeypatch.setattr(bf, 'repo_is_usable', lambda *a, **k: (True, 'ok'))
        _witnesses(monkeypatch, recorded='2026-07-01T00:00:00+00:00')
        bf.main(['--cards-dir', str(d), '--commit'])
        after = json.loads(p.read_text(encoding='utf-8'))
        assert after['freshness']['created_at'] == '2026-07-01T00:00:00+00:00'
        assert after['freshness']['last_built'] == '2026-07-23T00:00:00+00:00'
        assert after['synthesis'] == {'note': 'café —'}

    def test_commit_preserves_byte_exact_serialization(self, tmp_path,
                                                       monkeypatch):
        """indent=2, ensure_ascii=False, no trailing newline — matched against
        all 11 published cards. Anything else buries a one-line repair in a
        whole-file diff and makes the change unreviewable."""
        d, p = self._card(tmp_path, '2026-07-23T00:00:00+00:00')
        monkeypatch.setattr(bf, '_rel', lambda path: 'data/cards/x.json')
        monkeypatch.setattr(bf, 'repo_is_usable', lambda *a, **k: (True, 'ok'))
        _witnesses(monkeypatch, recorded='2026-07-01T00:00:00+00:00')
        bf.main(['--cards-dir', str(d), '--commit'])
        raw = p.read_text(encoding='utf-8')
        assert raw == json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        assert not raw.endswith('\n')
        assert 'café' in raw          # not \u00e9-escaped

    def test_shallow_repo_refuses_to_run(self, tmp_path, monkeypatch):
        d, p = self._card(tmp_path, '2026-07-23T00:00:00+00:00')
        before = p.read_bytes()
        monkeypatch.setattr(bf, 'repo_is_usable',
                            lambda *a, **k: (False, 'shallow clone'))
        assert bf.main(['--cards-dir', str(d), '--commit']) == \
            bf.EXIT_UNUSABLE_REPO
        assert p.read_bytes() == before
