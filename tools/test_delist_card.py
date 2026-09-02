"""delist_card: the removal door for a retired/duplicate SKU.

The tool's job is to take a slug out of EVERY surface at once and to be safe
about the two it must not touch — the drifting spine (own reconcile path) and
the phantomops-owned queue (never rewritten). These cover the full sweep, the
two-mode split, idempotence on absent surfaces, and that the queue is reported
but left alone (loudly when a record is somehow still claimable).
"""
import json
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'gateway'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import skus_registry  # noqa: E402
import work_queue  # noqa: E402
import delist_card as dc  # noqa: E402


def _repo(tmp_path, slug='panasonic-s5-mirrorless', *, with_queue_state=None):
    """A miniature repo carrying every surface for `slug`."""
    (tmp_path / 'data' / 'cards').mkdir(parents=True)
    (tmp_path / 'browser' / 'cards' / slug).mkdir(parents=True)
    (tmp_path / 'browser' / 'images' / 'heroes').mkdir(parents=True)

    (tmp_path / 'data' / 'cards' / f'{slug}.json').write_text('{"card_id": "%s"}' % slug)
    (tmp_path / 'browser' / 'cards' / slug / 'index.html').write_text('<h1>x</h1>')
    (tmp_path / 'browser' / 'images' / 'heroes' / f'{slug}.jpg').write_bytes(b'\xff\xd8jpg')

    # selfhost registry is WRAPPED ({'images': {...}}); hero registry is FLAT.
    (tmp_path / 'data' / 'selfhost_images.json').write_text(json.dumps(
        {'_description': 'x', 'images': {slug: {'file': 'y'}, 'keep-me': {'file': 'z'}}}))
    (tmp_path / 'data' / 'hero_images.json').write_text(json.dumps(
        {slug: {'file': 'h'}, 'keep-me': {'file': 'k'}}))

    skus = tmp_path / 'data' / 'skus.json'
    skus_registry.upsert(slug, {'gtin': '00885170392595', 'facet': 'body',
                                'identity': {'brand': 'Panasonic'}}, path=skus)
    skus_registry.upsert('keep-me', {'gtin': '111', 'facet': 'body',
                                     'identity': {'brand': 'Keep'}}, path=skus)

    if with_queue_state:
        q = tmp_path / 'data' / 'work_queue.json'
        work_queue.enroll(slug, slug, 'body', path=q)
        if with_queue_state != 'resolved':
            # 'promoted' is a real inert state (published, not claimable) — the
            # shape the live panasonic-s5-mirrorless record is actually in.
            work_queue.claim_next(path=q)
            work_queue.mark_review_ready(slug, path=q)
            work_queue.mark_published(slug, path=q)
    return tmp_path


def _actions(report):
    return {surface: action for surface, action, _ in report}


def test_full_retirement_clears_every_file_and_the_spine(tmp_path):
    slug = 'panasonic-s5-mirrorless'
    repo = _repo(tmp_path, slug)
    skus = repo / 'data' / 'skus.json'

    report = dc.delist(slug, repo=repo, skus_path=skus,
                       do_files=True, do_spine=True, apply=True)
    a = _actions(report)
    assert a['card-json'] == 'removed'
    assert a['card-html'] == 'removed'
    assert a['hero-jpg'] == 'removed'
    assert a['selfhost-reg'] == 'removed'
    assert a['hero-reg'] == 'removed'
    assert a['spine'] == 'delisted'

    assert not (repo / 'data' / 'cards' / f'{slug}.json').exists()
    assert not (repo / 'browser' / 'cards' / slug).exists()
    assert not (repo / 'browser' / 'images' / 'heroes' / f'{slug}.jpg').exists()

    # siblings survive every registry
    self_reg = json.loads((repo / 'data' / 'selfhost_images.json').read_text())
    assert slug not in self_reg['images'] and 'keep-me' in self_reg['images']
    hero_reg = json.loads((repo / 'data' / 'hero_images.json').read_text())
    assert slug not in hero_reg and 'keep-me' in hero_reg
    spine = json.loads(skus.read_text())['skus']
    assert slug not in spine and 'keep-me' in spine


def test_dry_run_writes_nothing(tmp_path):
    slug = 'panasonic-s5-mirrorless'
    repo = _repo(tmp_path, slug)
    skus = repo / 'data' / 'skus.json'

    report = dc.delist(slug, repo=repo, skus_path=skus,
                       do_files=True, do_spine=True, apply=False)
    # reports the intent...
    assert _actions(report)['spine'] == 'delisted'
    assert _actions(report)['card-json'] == 'removed'
    # ...but touched nothing
    assert (repo / 'data' / 'cards' / f'{slug}.json').exists()
    assert slug in json.loads(skus.read_text())['skus']
    assert slug in json.loads((repo / 'data' / 'selfhost_images.json').read_text())['images']


def test_files_only_leaves_the_spine_intact(tmp_path):
    slug = 'panasonic-s5-mirrorless'
    repo = _repo(tmp_path, slug)
    skus = repo / 'data' / 'skus.json'

    report = dc.delist(slug, repo=repo, skus_path=skus,
                       do_files=True, do_spine=False, apply=True)
    a = _actions(report)
    assert 'card-json' in a and 'spine' not in a
    assert not (repo / 'data' / 'cards' / f'{slug}.json').exists()
    assert slug in json.loads(skus.read_text())['skus']  # spine untouched


def test_spine_only_leaves_files_intact(tmp_path):
    slug = 'panasonic-s5-mirrorless'
    repo = _repo(tmp_path, slug)
    skus = repo / 'data' / 'skus.json'

    report = dc.delist(slug, repo=repo, skus_path=skus,
                       do_files=False, do_spine=True, apply=True)
    a = _actions(report)
    assert a['spine'] == 'delisted' and 'card-json' not in a
    assert (repo / 'data' / 'cards' / f'{slug}.json').exists()  # files untouched
    assert slug not in json.loads(skus.read_text())['skus']


def test_idempotent_on_absent_surfaces(tmp_path):
    """A cardless phantom (spine entry only) retires without crashing."""
    slug = 'sony-a7r v'
    (tmp_path / 'data').mkdir(parents=True)
    skus = tmp_path / 'data' / 'skus.json'
    skus_registry.upsert(slug, {'gtin': '00027242893832', 'facet': 'body',
                                'identity': {'brand': 'Sony'}}, path=skus)

    report = dc.delist(slug, repo=tmp_path, skus_path=skus,
                       do_files=True, do_spine=True, apply=True)
    a = _actions(report)
    assert a['card-json'] == 'absent'
    assert a['card-html'] == 'absent'
    assert a['hero-jpg'] == 'absent'
    assert a['selfhost-reg'] == 'no-registry'
    assert a['spine'] == 'delisted'
    assert slug not in json.loads(skus.read_text())['skus']


def test_queue_is_reported_not_mutated(tmp_path):
    slug = 'panasonic-s5-mirrorless'
    repo = _repo(tmp_path, slug, with_queue_state='promoted')
    skus = repo / 'data' / 'skus.json'
    q = repo / 'data' / 'work_queue.json'
    before = q.read_text()

    report = dc.delist(slug, repo=repo, skus_path=skus,
                       do_files=True, do_spine=True, apply=True)
    wq = [row for row in report if row[0] == 'work-queue'][0]
    assert 'state=promoted' in wq[1] and 'inert' in wq[1] and 'left in place' in wq[2]
    assert q.read_text() == before  # never rewritten


def test_resolved_queue_record_is_flagged_loudly(tmp_path):
    slug = 'panasonic-s5-mirrorless'
    repo = _repo(tmp_path, slug, with_queue_state='resolved')
    skus = repo / 'data' / 'skus.json'

    report = dc.delist(slug, repo=repo, skus_path=skus,
                       do_files=False, do_spine=True, apply=False)
    wq = [row for row in report if row[0] == 'work-queue'][0]
    assert 'RESOLVED' in wq[1]


# ── the spine primitive itself ─────────────────────────────────────────────

def test_delist_primitive_removes_and_reports_missing(tmp_path):
    skus = tmp_path / 'skus.json'
    skus_registry.upsert('a', {'gtin': '1', 'facet': 'body', 'identity': {}}, path=skus)
    assert skus_registry.delist('a', path=skus) == 'delisted'
    assert skus_registry.delist('a', path=skus) == 'missing-slug'
    assert json.loads(skus.read_text())['skus'] == {}


def test_delist_keeps_store_group_readable(tmp_path):
    """The atomic rewrite must keep the store 0640 so phantomops builds can
    still read the spine (the cross-user seam _atomic_write exists for)."""
    skus = tmp_path / 'skus.json'
    skus_registry.upsert('a', {'gtin': '1', 'facet': 'body', 'identity': {}}, path=skus)
    skus_registry.upsert('b', {'gtin': '2', 'facet': 'body', 'identity': {}}, path=skus)
    skus_registry.delist('a', path=skus)
    mode = stat.S_IMODE(skus.stat().st_mode)
    assert mode == 0o640
