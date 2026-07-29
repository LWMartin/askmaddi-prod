"""Tests for tools/regather_card.py.

The tool's value is almost entirely in what it refuses and in the order it
does two writes, so that is what these pin. The registry is a fixture rather
than the real phantom-ops file: that repo is not a dependency of this one and
will not be present in CI, and a test that skipped itself when it was missing
would be green for the wrong reason.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import regather_card as R


@pytest.fixture
def agg(tmp_path):
    """A minimal aggregator root holding a contamination registry."""
    d = tmp_path / 'agg' / 'fixtures' / 'manifests'
    d.mkdir(parents=True)
    (d / 'contamination.json').write_text(json.dumps({'products': {
        'sony-a7r': {
            'vendor': 'Sony', 'model': 'A7R II',
            'self': {'aliases': ['a7r ii', 'a7rii', 'ilce-7rm2']},
            'category': 'body',
        },
        'auto-sku': {
            'vendor': 'Sony', 'model': 'A7 IV',
            'self': {'aliases': 'auto'}, 'category': 'body',
        },
    }}))
    return tmp_path / 'agg'


@pytest.fixture
def queue(tmp_path):
    def _make(state, **extra):
        p = tmp_path / f'wq-{state}.json'
        p.write_text(json.dumps({
            'queue': {'sony-a7r': dict({
                'slug': 'sony-a7r', 'label': 'Sony A7R', 'category': 'body',
                'aliases': ['a7r', 'a7r v'], 'state': state,
                'build_attempts': 2, 'max_attempts': 3,
                'enrolled_at': '2026-06-01T00:00:00Z',
            }, **extra)},
            'as_of': '2026-07-29', 'cap_date': '2026-07-29', 'built_today': 0,
        }))
        return p
    return _make


def _record(path):
    return json.loads(Path(path).read_text())['queue']['sony-a7r']


def _run(agg, qpath, *extra):
    return R.main(['--slug', 'sony-a7r', '--aggregator-root', str(agg),
                   '--queue-path', str(qpath), *extra])


# ── derivation, not duplication ──────────────────────────────────────────

def test_label_and_aliases_come_from_the_registry(agg, queue):
    q = queue('promoted')
    assert _run(agg, q, '--apply') == 0
    rec = _record(q)
    assert rec['label'] == 'Sony A7R II'
    assert rec['aliases'] == ['a7r ii', 'a7rii', 'ilce-7rm2']


def test_the_contaminating_aliases_are_replaced_not_merged(agg, queue):
    """The old aliases named later generations. Merging would keep them, and
    the corpus this tool exists to fix would come back."""
    q = queue('promoted')
    _run(agg, q, '--apply')
    assert 'a7r v' not in _record(q)['aliases']


# ── refuse, don't guess ──────────────────────────────────────────────────

def test_missing_registry_entry_refuses(agg, queue):
    q = queue('promoted')
    assert R.main(['--slug', 'not-in-registry', '--aggregator-root', str(agg),
                   '--queue-path', str(q)]) == 2


def test_auto_aliases_refuse_rather_than_approximate(agg, queue):
    """'auto' means the gate derives aliases at load time. Approximating them
    here would silently differ from what the gate uses — the exact class of
    drift this tool exists to remove."""
    q = queue('promoted')
    assert R.main(['--slug', 'auto-sku', '--aggregator-root', str(agg),
                   '--queue-path', str(q)]) == 2


def test_missing_registry_file_refuses(tmp_path, queue):
    q = queue('promoted')
    assert R.main(['--slug', 'sony-a7r', '--aggregator-root', str(tmp_path),
                   '--queue-path', str(q)]) == 2


@pytest.mark.parametrize('state', ['building', 'review_ready'])
def test_in_flight_records_are_refused(agg, queue, state):
    q = queue(state)
    assert _run(agg, q, '--apply') == 1
    assert _record(q)['label'] == 'Sony A7R', 'refused run must not write'


def test_dry_run_is_the_default(agg, queue):
    q = queue('promoted')
    assert _run(agg, q) == 0
    rec = _record(q)
    assert rec['state'] == 'promoted' and rec['label'] == 'Sony A7R'


# ── the forced order ─────────────────────────────────────────────────────

@pytest.mark.parametrize('state,expect_from', [
    ('promoted', 'promoted'),
    ('corpus_thin', None),
    ('rejected', None),
    ('resolved', None),
])
def test_every_reopenable_state_ends_relabelled(agg, queue, state, expect_from):
    """set_aliases refuses anything not at `resolved`, so a tool that wrote in
    the other order would re-open the record and leave the old label on it —
    and the next build would re-fetch against exactly the strings that caused
    the problem. The relabel landing is the proof the order held."""
    q = queue(state)
    assert _run(agg, q, '--apply') == 0
    rec = _record(q)
    assert rec['state'] == 'resolved'
    assert rec['label'] == 'Sony A7R II'
    if expect_from:
        assert rec.get('requeued_from') == expect_from


def test_promoted_reopens_as_a_full_regather_not_a_re_extract(agg, queue):
    """resume_stage must be absent. Present-and-'extract' would re-read the
    cached triples — the wrong-product corpus — with no fetch at all."""
    q = queue('promoted')
    _run(agg, q, '--apply')
    assert 'resume_stage' not in _record(q)


def test_attempt_budget_is_reset_on_reopen(agg, queue):
    """A record re-opened with 2 of 3 attempts spent would get one shot at a
    build it has never actually attempted under the corrected inputs."""
    q = queue('promoted')
    _run(agg, q, '--apply')
    assert _record(q)['build_attempts'] == 0
