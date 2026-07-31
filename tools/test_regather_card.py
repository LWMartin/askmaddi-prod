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


# ── the spine/registry id join (2026-07-31) ──────────────────────────────

@pytest.fixture
def spine(tmp_path):
    """A spine whose slug and contamination_key differ, as three live SKUs do."""
    p = tmp_path / 'skus.json'
    p.write_text(json.dumps({'skus': {
        # the divergent case: slug != registry id
        'sigma-35mm-f1-2-dg-dn-art': {'vendor': 'Sigma',
                                      'contamination_key': 'sigma-35-f12-dg-dn'},
        # the common case: they coincide
        'sony-a7r': {'vendor': 'Sony', 'contamination_key': 'sony-a7r'},
        # no key declared at all
        'keyless': {'vendor': 'Sony'},
    }}))
    return p


@pytest.fixture
def agg_with_canonical_id(tmp_path):
    """A registry keyed by canonical product ID, not by spine slug."""
    d = tmp_path / 'agg2' / 'fixtures' / 'manifests'
    d.mkdir(parents=True)
    (d / 'contamination.json').write_text(json.dumps({'products': {
        'sigma-35-f12-dg-dn': {
            'vendor': 'Sigma', 'model': '35mm F1.2 DG DN Art',
            'self': {'aliases': ['35mm f1.2 dg dn', 'f12 dg dn']},
            'category': 'lens',
        },
    }}))
    return tmp_path / 'agg2'


def test_registry_key_follows_the_spine_when_the_ids_diverge(spine):
    """contamination.json is keyed by canonical product ID and the spine by
    marketplace slug. They coincided for 11 of 14 live SKUs, which is exactly
    why looking up by slug went unnoticed — it worked everywhere anyone had
    tried it, and refused precisely the three that differ."""
    assert R.registry_key('sigma-35mm-f1-2-dg-dn-art',
                          spine_path=spine) == 'sigma-35-f12-dg-dn'


def test_registry_key_is_the_slug_when_they_coincide(spine):
    assert R.registry_key('sony-a7r', spine_path=spine) == 'sony-a7r'


@pytest.mark.parametrize("slug", ['keyless', 'not-in-the-spine-at-all'])
def test_registry_key_falls_back_to_the_slug(spine, slug):
    """No declared key is not an error — most SKUs have none, and a wrong
    key produces the same loud refusal as before, never a silent miss."""
    assert R.registry_key(slug, spine_path=spine) == slug


def test_a_missing_or_unreadable_spine_falls_back_rather_than_raising(tmp_path):
    assert R.registry_key('x', spine_path=tmp_path / 'nope.json') == 'x'
    bad = tmp_path / 'bad.json'
    bad.write_text('{not json')
    assert R.registry_key('x', spine_path=bad) == 'x'


def test_the_divergent_sku_now_resolves(agg_with_canonical_id, spine):
    """The regression: this refused with 'author one before re-gathering' for
    an entry that already existed under another name."""
    entry = R.load_entry(agg_with_canonical_id, 'sigma-35mm-f1-2-dg-dn-art',
                         spine_path=spine)
    assert entry['model'] == '35mm F1.2 DG DN Art'


def test_a_genuinely_absent_entry_still_refuses_and_names_the_key_tried(
        agg_with_canonical_id, spine):
    """Resolving through the spine must not soften the refusal — and the
    message has to say WHICH key was looked up, or an operator debugging a
    divergent SKU is told to author an entry under the wrong name."""
    with pytest.raises(R.RegistryUnavailable) as exc:
        R.load_entry(agg_with_canonical_id, 'sony-a7r', spine_path=spine)
    assert 'sony-a7r' in str(exc.value)
