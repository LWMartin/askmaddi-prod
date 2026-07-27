"""needs_category: the factory parks a SKU it cannot classify.

build_card refuses to resolve a dictionary category when the SKU has no
authored `facet` in the spine, exiting EXIT_CATEGORY_UNRESOLVED before stage
1 rather than extracting the product as a prime lens. These tests cover this
side of that seam — what the factory does with the refusal.

Why a state rather than a failure. On 2026-07-27 seven camera bodies were
found published with lens axes: coma, bokeh, vignetting, filter thread. The
sensor, EVF, burst and battery discussion their reviewers actually produced
had no axis to land on and was dropped, so the A7 R card carried no sensor
axis at all. Nothing errored. Four independently reasonable fallbacks
composed into a confident build of the wrong product.

A card built as the wrong product is worse than a card not built, and the
repair is an authoring act — someone has to say what the thing is. So the
record parks where a human can see it, and requeue() re-opens it once the
facet exists.
"""
import json

import pytest

from gateway import card_factory, work_queue


def _queue_with_building(tmp_path, slug='sony-a7r'):
    """A queue holding one record mid-build, which is where a refusal lands."""
    path = tmp_path / 'work_queue.json'
    work_queue.enroll(slug, 'Sony A7R', 'body', path=path)
    work_queue.claim_next(path=path)
    return path


# ── The park ─────────────────────────────────────────────────────────────

def test_mark_needs_category_parks_the_record(tmp_path):
    path = _queue_with_building(tmp_path)
    rec = work_queue.mark_needs_category('sony-a7r', 'no authored facet',
                                         path=path)
    assert rec['state'] == 'needs_category'
    assert 'no authored facet' in rec['last_error']
    assert rec['needs_category_at']


def test_park_does_not_count_against_the_daily_cap(tmp_path):
    """A refusal is not a build. Spending cap on it would let a handful of
    unfacted SKUs starve the drip of its real work for the day."""
    path = _queue_with_building(tmp_path)
    before = work_queue.load_queue(path).get('built_today')
    work_queue.mark_needs_category('sony-a7r', 'no facet', path=path)
    assert work_queue.load_queue(path).get('built_today') == before


def test_only_a_building_record_can_park(tmp_path):
    path = tmp_path / 'work_queue.json'
    work_queue.enroll('sony-a7r', 'Sony A7R', 'body', path=path)
    with pytest.raises(ValueError):
        work_queue.mark_needs_category('sony-a7r', 'no facet', path=path)


def test_needs_category_is_a_known_state(tmp_path):
    """counts() histograms over STATES; an unlisted state would vanish from
    every operator surface that reports the queue."""
    assert 'needs_category' in work_queue.STATES
    path = _queue_with_building(tmp_path)
    work_queue.mark_needs_category('sony-a7r', 'no facet', path=path)
    assert work_queue.counts(path=path)['needs_category'] == 1


# ── The way out ──────────────────────────────────────────────────────────

def test_requeue_reopens_a_parked_record(tmp_path):
    """The half that makes parking a decision rather than a grave.

    Parking is only the right call if authoring the facet can bring the SKU
    back. Without this the state would be a one-way trip and the honest
    refusal would be worse than the silent wrong answer it replaced.
    """
    path = _queue_with_building(tmp_path)
    work_queue.mark_needs_category('sony-a7r', 'no facet', path=path)
    rec = work_queue.requeue('sony-a7r', path=path)
    assert rec['state'] == 'resolved'
    assert rec['requeued_from'] == 'needs_category'
    assert rec['build_attempts'] == 0


# ── The factory seam ─────────────────────────────────────────────────────

def test_tick_routes_the_refusal_to_the_park(tmp_path):
    path = _queue_with_building(tmp_path)

    def runner(record):
        return card_factory.EXIT_CATEGORY_UNRESOLVED, None, 'no authored facet'

    # The record is already `building` from _queue_with_building; tick claims
    # its own, so re-open first and let tick drive the whole transition.
    work_queue.mark_needs_category('sony-a7r', 'seed', path=path)
    work_queue.requeue('sony-a7r', path=path)

    out = card_factory.tick(runner, cap=5, path=path)
    assert out['action'] == 'needs_category'
    assert out['slug'] == 'sony-a7r'
    q = work_queue.load_queue(path)['queue']['sony-a7r']
    assert q['state'] == 'needs_category'


def test_refusal_is_not_treated_as_a_build_failure(tmp_path):
    """It must not go down mark_failed_or_retry: retrying re-resolves the
    same spine to the same refusal, burning the attempt budget on a settled
    outcome, and eventually marking `failed` — which reads as "this SKU is
    broken" when the truth is "nobody has said what it is yet"."""
    path = _queue_with_building(tmp_path)

    def runner(record):
        return card_factory.EXIT_CATEGORY_UNRESOLVED, None, 'no authored facet'

    work_queue.mark_needs_category('sony-a7r', 'seed', path=path)
    work_queue.requeue('sony-a7r', path=path)
    card_factory.tick(runner, cap=5, path=path)

    rec = work_queue.load_queue(path)['queue']['sony-a7r']
    assert rec['state'] != 'failed'
    assert rec.get('build_attempts', 0) <= 1


def test_exit_code_matches_build_card(tmp_path):
    """The mirrored constant. build_card lives in the phantom-ops workspace,
    so the two sides share a number and nothing else — if build_card
    renumbers, this is the tripwire."""
    assert card_factory.EXIT_CATEGORY_UNRESOLVED == 7
