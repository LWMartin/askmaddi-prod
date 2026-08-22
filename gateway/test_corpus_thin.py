"""corpus_thin off-ramp + quality-first factory posture (2026-07-07).

Covers the askmaddi-prod half of the Stage 1b / corpus-floor wiring:
  - work_queue: corpus_thin is a real terminal state, marked without retry,
    requeueable after sourcing improves, never counted as a build.
  - card_factory.tick: EXIT_CORPUS_THIN (3) routes to the park, distinct from
    the failed/retry path; cap is untouched by an abstention.
  - build_card_runner: yt=True appends --yt to the build_card cmd.
  - DEFAULT_DAILY_CAP is pinned by test (the politeness budget).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import card_factory  # noqa: E402
import work_queue  # noqa: E402


@pytest.fixture
def qpath(tmp_path):
    return tmp_path / "work_queue.json"


def _enroll_and_claim(qpath, slug="sony-a7s-iii"):
    work_queue.enroll(slug, "Sony A7S III", "body", path=qpath)
    rec = work_queue.claim_next(path=qpath)
    assert rec["slug"] == slug and rec["state"] == "building"
    return slug


# ------------------------------------------------------------- work_queue

def test_corpus_thin_in_states():
    assert "corpus_thin" in work_queue.STATES


def test_mark_corpus_thin_parks_without_retry(qpath):
    slug = _enroll_and_claim(qpath)
    rec = work_queue.mark_corpus_thin(slug, "sources 4/12 claims 40/120",
                                      path=qpath)
    assert rec["state"] == "corpus_thin"
    assert "4/12" in rec["last_error"]
    assert rec.get("corpus_thin_at")
    # No retry: the record does NOT return to the resolved pool.
    assert work_queue.claim_next(path=qpath) is None


def test_mark_corpus_thin_requires_building(qpath):
    work_queue.enroll("idle-sku", "Idle", "body", path=qpath)
    with pytest.raises(ValueError):
        work_queue.mark_corpus_thin("idle-sku", "x", path=qpath)
    with pytest.raises(KeyError):
        work_queue.mark_corpus_thin("absent", "x", path=qpath)


def test_corpus_thin_is_requeueable(qpath):
    slug = _enroll_and_claim(qpath)
    work_queue.mark_corpus_thin(slug, "thin", path=qpath)
    rec = work_queue.requeue(slug, path=qpath)
    assert rec["state"] == "resolved"
    assert rec["requeued_from"] == "corpus_thin"
    assert rec["build_attempts"] == 0


def test_corpus_thin_counts_in_histogram(qpath):
    slug = _enroll_and_claim(qpath)
    work_queue.mark_corpus_thin(slug, "thin", path=qpath)
    assert work_queue.counts(path=qpath)["corpus_thin"] == 1


# ------------------------------------------------------------ card_factory

def test_tick_routes_exit_3_to_corpus_thin(qpath):
    slug = _enroll_and_claim(qpath)
    # Re-open by hand so tick can claim it itself (tick claims from resolved).
    q = work_queue.load_queue(qpath)
    q["queue"][slug]["state"] = "resolved"
    work_queue._atomic_write(q, qpath)

    runner = lambda record: (card_factory.EXIT_CORPUS_THIN, "", "floor: 4/12")  # noqa: E731
    out = card_factory.tick(runner, cap=2, path=qpath)
    assert out["action"] == "corpus_thin"
    assert out["slug"] == slug
    rec = work_queue.load_queue(qpath)["queue"][slug]
    assert rec["state"] == "corpus_thin"


def test_tick_abstention_does_not_burn_cap(qpath):
    _enroll_and_claim(qpath)
    q = work_queue.load_queue(qpath)
    for r in q["queue"].values():
        r["state"] = "resolved"
    work_queue._atomic_write(q, qpath)

    runner = lambda record: (card_factory.EXIT_CORPUS_THIN, "", "thin")  # noqa: E731
    card_factory.tick(runner, cap=2, path=qpath)
    assert work_queue.cap_remaining(2, path=qpath) == 2


def test_tick_generic_failure_still_retries(qpath):
    slug = _enroll_and_claim(qpath)
    q = work_queue.load_queue(qpath)
    q["queue"][slug]["state"] = "resolved"
    work_queue._atomic_write(q, qpath)

    runner = lambda record: (1, "", "boom")  # noqa: E731
    out = card_factory.tick(runner, cap=2, path=qpath)
    assert out["action"] == "retry"  # rc=1 keeps the existing retry budget path


def test_default_cap_is_pinned():
    """The cap is a politeness budget against upstream fetch volume, not a
    throughput dial, so its value is pinned — changing it must be a deliberate
    edit here rather than a drive-by tweak. 2 -> 4 on 2026-07-27 to move toward
    the breadth gate; 4 -> 6 on 2026-08-17 (eca61dd, Lee: cards are the moat —
    push toward critical mass); 6 -> 12 on 2026-08-22 (Lee: reservoir tap — the
    worklist backfill now feeds the drip so the queue no longer starves, double
    throughput to drain it). The ~20 YT attempts/card in build_card's drip
    profile make this ~240 jittered fetches/day."""
    assert card_factory.DEFAULT_DAILY_CAP == 12


def test_runner_appends_yt_flag(tmp_path, monkeypatch):
    captured = {}

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = [str(c) for c in cmd]
        return _Proc()

    monkeypatch.setattr(card_factory.subprocess, "run", fake_run)
    bc = tmp_path / "build_card.py"
    bc.write_text("# stub")
    record = {"slug": "sony-a7s-iii", "label": "Sony A7S III",
              "category": "body"}

    runner = card_factory.build_card_runner(build_card_path=bc, yt=True,
                                            out_root=tmp_path)
    runner(record)
    assert "--yt" in captured["cmd"]

    runner_bare = card_factory.build_card_runner(build_card_path=bc,
                                                 out_root=tmp_path)
    runner_bare(record)
    assert "--yt" not in captured["cmd"]
