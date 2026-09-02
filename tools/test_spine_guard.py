"""Tests for spine_guard.py — the anti-revert gate on skus.json pushes.

Design rules under test (spine wipe 231->195, 2026-09-02):
  1. Clone spine ⊇ live spine  -> safe (exit 0), even with brand-new adds.
  2. Live has a key the clone lacks -> would revert it (exit 3), named.
  3. An intentional delist declares --allow-drop; that key stops being a
     wrongful drop (exit 0).
  4. A declared drop the clone STILL carries is harmless (superset holds).
  5. Unreadable / malformed spine -> exit 2, never a false "safe".

Run from repo root:  python -m pytest tools/test_spine_guard.py -q
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import spine_guard

GUARD = str(Path(__file__).with_name("spine_guard.py"))


def _write(p, keys):
    p.write_text(json.dumps({"skus": {k: {"gtin": k} for k in keys}}), encoding="utf-8")


# ── unit: evaluate() is the whole decision ────────────────────────────────
def test_superset_is_safe():
    assert spine_guard.evaluate({"a", "b", "c"}, {"a", "b"}, []) == set()


def test_missing_live_key_is_a_revert():
    assert spine_guard.evaluate({"a"}, {"a", "b"}, []) == {"b"}


def test_declared_drop_is_forgiven():
    assert spine_guard.evaluate({"a"}, {"a", "b"}, ["b"]) == set()


def test_declared_drop_still_present_is_fine():
    # clone still carries 'b' though it's declared droppable — superset holds.
    assert spine_guard.evaluate({"a", "b"}, {"a", "b"}, ["b"]) == set()


def test_new_adds_never_trip_the_guard():
    assert spine_guard.evaluate({"a", "b", "new"}, {"a", "b"}, []) == set()


# ── CLI: exit codes are the contract land.sh depends on ───────────────────
def _run(clone, live, *extra):
    return subprocess.run(
        [sys.executable, GUARD, "--clone-skus", str(clone), "--live-skus", str(live), *extra],
        capture_output=True, text=True,
    )


def test_cli_safe_exit0(tmp_path):
    _write(tmp_path / "clone.json", ["a", "b", "c"])
    _write(tmp_path / "live.json", ["a", "b"])
    r = _run(tmp_path / "clone.json", tmp_path / "live.json")
    assert r.returncode == 0


def test_cli_revert_exit3_names_key(tmp_path):
    _write(tmp_path / "clone.json", ["a"])
    _write(tmp_path / "live.json", ["a", "sony-a7"])
    r = _run(tmp_path / "clone.json", tmp_path / "live.json")
    assert r.returncode == 3
    assert "sony-a7" in r.stderr


def test_cli_allow_drop_exit0(tmp_path):
    _write(tmp_path / "clone.json", ["a"])
    _write(tmp_path / "live.json", ["a", "junk-slug"])
    r = _run(tmp_path / "clone.json", tmp_path / "live.json", "--allow-drop", "junk-slug")
    assert r.returncode == 0


def test_cli_missing_file_exit2(tmp_path):
    _write(tmp_path / "live.json", ["a"])
    r = _run(tmp_path / "nope.json", tmp_path / "live.json")
    assert r.returncode == 2


def test_cli_json_output_shape(tmp_path):
    _write(tmp_path / "clone.json", ["a"])
    _write(tmp_path / "live.json", ["a", "b"])
    r = _run(tmp_path / "clone.json", tmp_path / "live.json", "--json")
    payload = json.loads(r.stdout)
    assert payload["safe"] is False
    assert payload["would_revert"] == ["b"]
    assert payload["live_count"] == 2
