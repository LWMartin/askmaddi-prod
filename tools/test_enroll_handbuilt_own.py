"""Own test suite for tools/enroll_handbuilt.py (author-side, not the hidden gate).

Covers the CLI entrypoint and default-queue-path plumbing, which the hidden
gate (tools/test_enroll_handbuilt.py) exercises only indirectly via explicit
queue_path.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(HERE))

import work_queue          # noqa: E402
import enroll_handbuilt     # noqa: E402


ROSTER = {
    "roster": [
        {"slug": "sony-a7iv", "label": "Sony A7 IV", "category": "body"},
        {"slug": "sigma-35-art-dg-dn-ii", "label": "Sigma 35mm f/1.4 DG DN Art II", "category": "lens"},
    ]
}


def _roster(tmp_path):
    p = tmp_path / "roster.json"
    p.write_text(json.dumps(ROSTER))
    return str(p)


def test_cli_dry_run_default_writes_nothing(tmp_path, capsys):
    rp = _roster(tmp_path)
    qp = tmp_path / "queue.json"
    rc = enroll_handbuilt.main(["--roster", rp, "--queue-path", str(qp)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "sony-a7iv" in out
    assert not qp.exists()


def test_cli_commit_writes_records(tmp_path, capsys):
    rp = _roster(tmp_path)
    qp = tmp_path / "queue.json"
    rc = enroll_handbuilt.main(["--roster", rp, "--queue-path", str(qp), "--commit"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "COMMIT" in out
    q = work_queue.load_queue(str(qp))["queue"]
    assert set(q) == {"sony-a7iv", "sigma-35-art-dg-dn-ii"}
    assert q["sony-a7iv"]["state"] == "resolved"


def test_enroll_missing_uses_work_queue_default_path_when_none(tmp_path, monkeypatch):
    # queue_path=None must call work_queue.load_queue()/enroll() with NO path
    # kwarg, so it honors work_queue's own default rather than passing None
    # through literally.
    rp = _roster(tmp_path)
    default_qp = tmp_path / "default_queue.json"
    monkeypatch.setattr(work_queue, "WORK_QUEUE_PATH", default_qp)
    monkeypatch.setattr(enroll_handbuilt.work_queue, "WORK_QUEUE_PATH", default_qp)

    def fake_load_queue(path=default_qp):
        return work_queue._empty_queue() if not Path(path).exists() else json.loads(Path(path).read_text())

    def fake_enroll(slug, label, category, *, path=default_qp, **kw):
        q = fake_load_queue(path)
        q.setdefault("queue", {})[slug] = {"slug": slug, "label": label, "category": category, "state": "resolved"}
        Path(path).write_text(json.dumps(q))
        return q["queue"][slug]

    monkeypatch.setattr(work_queue, "load_queue", fake_load_queue)
    monkeypatch.setattr(work_queue, "enroll", fake_enroll)

    summary = enroll_handbuilt.enroll_missing(rp, None, commit=True)
    assert set(summary["enrolled"]) == {"sony-a7iv", "sigma-35-art-dg-dn-ii"}
    assert default_qp.exists()


def test_roster_order_preserved_in_enrolled_list(tmp_path):
    rp = _roster(tmp_path)
    qp = str(tmp_path / "queue.json")
    summary = enroll_handbuilt.enroll_missing(rp, qp, commit=False)
    assert summary["enrolled"] == ["sony-a7iv", "sigma-35-art-dg-dn-ii"]
