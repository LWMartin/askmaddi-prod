"""requeue_rebuild: the operator surface for re-opening live cards.

The tool's whole job is refusing to do the wrong thing, so that is what
these cover: the preconditions, the dry-run default, and the roster.

The roster is not a convenience list. sony-a7iv is absent from it on
purpose, and a test pins that — it was built with the body dictionary and
is correct, so rebuilding it to improve three headings would move
last_built for work that re-analyzed nothing. That is the freshness
overclaim ruled out on 2026-07-27, and a roster is exactly the sort of
thing that quietly grows to "all the cards".
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'gateway'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import work_queue  # noqa: E402
import requeue_rebuild as rr  # noqa: E402


def _promoted(tmp_path, slug='sony-a7r'):
    q = tmp_path / 'wq.json'
    work_queue.enroll(slug, slug, 'body', path=q)
    work_queue.claim_next(path=q)
    work_queue.mark_review_ready(slug, path=q)
    work_queue.mark_published(slug, path=q)
    return q


def _with_triples(monkeypatch, tmp_path):
    """Pretend the cached triples exist, without a spool."""
    d = tmp_path / 'triples'
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(rr, 'triples_dir', lambda slug: d)


# ── The roster ───────────────────────────────────────────────────────────

def test_roster_is_the_seven_mis_extracted_bodies():
    assert set(rr.BODIES_2026_07_27) == {
        'canon-r5', 'canon-r6', 'sony-a1', 'sony-a7-v',
        'sony-a7c', 'sony-a7r', 'sony-a7s-iii'}


def test_correct_cards_are_not_on_the_roster():
    """a7iv was extracted with the body dictionary; sigma and the tripods
    were correct too. Rebuilding a sound card to improve a label would move
    a clock that asserts synthesis was recomputed, for work that recomputed
    nothing."""
    for slug in ('sony-a7iv', 'sigma-35-art-dg-dn-ii',
                 'peak-design-pro-tripod', 'peak-design-travel-tripod'):
        assert slug not in rr.BODIES_2026_07_27


# ── Preconditions ────────────────────────────────────────────────────────

def test_missing_record_is_skipped(tmp_path):
    q = _promoted(tmp_path)
    ok, why = rr.inspect('never-enrolled', q)
    assert not ok and 'no work-queue record' in why


def test_non_promoted_record_is_skipped(tmp_path):
    q = tmp_path / 'wq.json'
    work_queue.enroll('sony-a7r', 'Sony A7R', 'body', path=q)
    ok, why = rr.inspect('sony-a7r', q)
    assert not ok and 'not promoted' in why


def test_missing_triples_blocks(tmp_path, monkeypatch):
    """Absent triples fail loudly at extract's _require and burn an attempt.
    Catching it here costs nothing; catching it there costs a build."""
    q = _promoted(tmp_path)
    monkeypatch.setattr(rr, 'triples_dir',
                        lambda slug: tmp_path / 'nope' / 'triples')
    ok, why = rr.inspect('sony-a7r', q)
    assert not ok and 'no cached triples' in why


def test_promoted_with_triples_is_ready(tmp_path, monkeypatch):
    q = _promoted(tmp_path)
    _with_triples(monkeypatch, tmp_path)
    ok, _ = rr.inspect('sony-a7r', q)
    assert ok


# ── Dry run ──────────────────────────────────────────────────────────────

def test_dry_run_is_the_default_and_writes_nothing(tmp_path, monkeypatch, capsys):
    q = _promoted(tmp_path)
    _with_triples(monkeypatch, tmp_path)
    before = q.read_text(encoding='utf-8')
    rc = rr.main(['--slug', 'sony-a7r', '--queue-path', str(q)])
    assert rc == 0
    assert q.read_text(encoding='utf-8') == before
    assert 'dry-run' in capsys.readouterr().out


def test_apply_requeues_from_cached_triples(tmp_path, monkeypatch):
    """resume_stage='extract' is the point: same evidence pool, current
    gates, zero fetch spend. A full re-fetch would change what the rebuild
    is a rebuild OF."""
    q = _promoted(tmp_path)
    _with_triples(monkeypatch, tmp_path)
    rr.main(['--slug', 'sony-a7r', '--queue-path', str(q), '--apply'])
    rec = work_queue.load_queue(q)['queue']['sony-a7r']
    assert rec['state'] == 'resolved'
    assert rec.get('resume_stage') == 'extract'
    assert rec.get('requeued_from') == 'promoted'


def test_nothing_to_do_is_an_error(tmp_path):
    with pytest.raises(SystemExit):
        rr.main(['--queue-path', str(tmp_path / 'wq.json')])


# ── The spool path ───────────────────────────────────────────────────────

def test_spool_default_matches_the_crontab():
    """The live crontab runs card_factory with --out /var/lib/askmaddi-cards,
    so that is where factory builds keep their triples."""
    assert str(rr.DEFAULT_BUILD_ROOT) == '/var/lib/askmaddi-cards'


def test_triples_are_looked_for_in_the_spool_not_the_source_tree():
    """The bug this tool shipped with, pinned.

    It first derived the build root from build_card's location — the same
    derivation card_factory uses when its out_root is None. That points at
    aggregator-build/out/, which holds only the four HAND-built cards. Those
    four are exactly the four that came out correct, because a hand build
    passes --category while the factory never told extract anything. So the
    wrong path found the four cards that need nothing and declared the seven
    that need rebuilding to be missing their spool.
    """
    got = str(rr.triples_dir('sony-a7r'))
    assert got == '/var/lib/askmaddi-cards/sony-a7r/triples'
    assert 'aggregator-build' not in got
    assert 'phantom-ops' not in got


def test_build_root_is_overridable(tmp_path, monkeypatch):
    q = _promoted(tmp_path)
    spool = tmp_path / 'spool'
    (spool / 'sony-a7r' / 'triples').mkdir(parents=True)
    rr.main(['--slug', 'sony-a7r', '--queue-path', str(q),
             '--build-root', str(spool)])
    assert rr.triples_dir('sony-a7r') == spool / 'sony-a7r' / 'triples'
