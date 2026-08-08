"""Holdout gate for tools/enroll_handbuilt.py — the hand-built card enroll tool.

Authored BLIND by the spec-writer and proven satisfiable by a scratch impl,
which was then reverted for dispatch. The worker re-implements enroll_handbuilt.py
from the manifest intent; this file is the independent outer gate it never sees.

Contract under test (the tool's public interface):
  load_roster(roster_path) -> list[dict]   each {'slug','label','category'}
  enroll_missing(roster_path, queue_path, *, commit=False) -> dict
      {'enrolled': [slug,...], 'skipped': [slug,...], 'committed': bool}
      - a roster slug ALREADY in the queue (any state) -> skipped, left untouched
      - a roster slug NOT in the queue -> enrolled at state 'resolved'
        (actually WRITTEN only when commit=True)
      - commit=False (default) is a dry run: compute the split, persist NOTHING
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "gateway"))   # work_queue lives here
sys.path.insert(0, str(HERE))               # enroll_handbuilt lives here

import work_queue          # noqa: E402
import enroll_handbuilt    # noqa: E402


ROSTER = {
    "roster": [
        {"slug": "sony-a7iv", "label": "Sony A7 IV", "category": "body"},
        {"slug": "sigma-35-art-dg-dn-ii", "label": "Sigma 35mm f/1.4 DG DN Art II", "category": "lens"},
        {"slug": "peak-design-pro-tripod", "label": "Peak Design Pro Tripod", "category": "support"},
        {"slug": "peak-design-travel-tripod", "label": "Peak Design Travel Tripod", "category": "support"},
    ]
}


def _roster(tmp_path):
    p = tmp_path / "roster.json"
    p.write_text(json.dumps(ROSTER))
    return str(p)


def _slugs():
    return {e["slug"] for e in ROSTER["roster"]}


def test_load_roster_returns_all_entries(tmp_path):
    entries = enroll_handbuilt.load_roster(_roster(tmp_path))
    assert {e["slug"] for e in entries} == _slugs()
    for e in entries:
        assert e["label"] and e["category"]


def test_enrolls_all_missing_into_empty_queue(tmp_path):
    qp = str(tmp_path / "queue.json")
    summary = enroll_handbuilt.enroll_missing(_roster(tmp_path), qp, commit=True)
    assert set(summary["enrolled"]) == _slugs()
    assert summary["skipped"] == []
    q = work_queue.load_queue(qp)["queue"]
    assert set(q) == _slugs()
    for e in ROSTER["roster"]:
        rec = q[e["slug"]]
        assert rec["label"] == e["label"]
        assert rec["category"] == e["category"]
        assert rec["state"] == "resolved"


def test_skips_slug_already_in_queue_and_leaves_it_untouched(tmp_path):
    qp = str(tmp_path / "queue.json")
    # Pre-seed sony-a7iv, then push it PAST resolved with a different label.
    work_queue.enroll("sony-a7iv", "Pre-existing", "body", path=qp)
    q0 = work_queue.load_queue(qp)
    q0["queue"]["sony-a7iv"]["state"] = "promoted"
    work_queue._atomic_write(q0, qp)

    summary = enroll_handbuilt.enroll_missing(_roster(tmp_path), qp, commit=True)
    assert "sony-a7iv" in summary["skipped"]
    assert set(summary["enrolled"]) == _slugs() - {"sony-a7iv"}
    q = work_queue.load_queue(qp)["queue"]
    # untouched: state + label preserved, NOT reset to resolved / the roster label
    assert q["sony-a7iv"]["state"] == "promoted"
    assert q["sony-a7iv"]["label"] == "Pre-existing"


def test_idempotent_second_run_enrolls_nothing(tmp_path):
    rp = _roster(tmp_path)
    qp = str(tmp_path / "queue.json")
    enroll_handbuilt.enroll_missing(rp, qp, commit=True)
    summary2 = enroll_handbuilt.enroll_missing(rp, qp, commit=True)
    assert summary2["enrolled"] == []
    assert set(summary2["skipped"]) == _slugs()
    assert set(work_queue.load_queue(qp)["queue"]) == _slugs()   # no growth/dupes


def test_dry_run_reports_but_writes_nothing(tmp_path):
    qp = str(tmp_path / "queue.json")
    summary = enroll_handbuilt.enroll_missing(_roster(tmp_path), qp, commit=False)
    assert set(summary["enrolled"]) == _slugs()   # would-enroll is reported
    assert summary["committed"] is False
    assert work_queue.load_queue(qp).get("queue", {}) == {}   # nothing persisted
