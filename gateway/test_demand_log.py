"""Tests for demand_log.py — the upstream unmet-demand signal.

Covers: append + read round-trip, null-identity (unfulfillable demand) events,
default category, and the LOCKED privacy invariant — the log refuses to persist
any query-bearing field.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demand_log  # noqa: E402


def test_append_and_read_roundtrip(tmp_path):
    p = tmp_path / 'demand_log.jsonl'
    e1 = demand_log.log_unmet('body', identity={'epid': '123', 'title': 'Sony A7 IV'},
                              path=p, _now='2026-06-24T00:00:00Z')
    e2 = demand_log.log_unmet('lens', identity=None, path=p,
                              _now='2026-06-24T00:01:00Z')
    events = demand_log.read_events(p)
    assert len(events) == 2
    assert events[0] == e1
    assert events[1] == e2


def test_null_identity_is_logged(tmp_path):
    """Unfulfillable demand (no resolved product) is the MOST interesting demand —
    it must still be logged, with identity=None."""
    p = tmp_path / 'd.jsonl'
    e = demand_log.log_unmet('support', identity=None, path=p)
    assert e['identity'] is None
    assert demand_log.read_events(p)[0]['identity'] is None


def test_default_category_when_falsy(tmp_path):
    p = tmp_path / 'd.jsonl'
    e = demand_log.log_unmet('', path=p)
    assert e['category'] == 'unknown'


def test_non_dict_identity_scrubbed_to_none(tmp_path):
    """A stray string (e.g. an accidental raw query) passed as identity must not
    land in the log masquerading as identity — it's scrubbed to None."""
    p = tmp_path / 'd.jsonl'
    e = demand_log.log_unmet('body', identity='sony a7 iv cheap', path=p)
    assert e['identity'] is None


@pytest.mark.parametrize('bad_field', ['query', 'q', 'search', 'raw_query', 'text'])
def test_privacy_refuses_query_fields(tmp_path, bad_field):
    """LOCKED privacy line: the demand log is category + ts + resolved identity
    only. A forbidden query-bearing field inside identity is a hard ValueError —
    the line is enforced in code, not merely documented."""
    p = tmp_path / 'd.jsonl'
    with pytest.raises(ValueError, match='forbidden'):
        demand_log.log_unmet('body', identity={'epid': '1', bad_field: 'leak'}, path=p)
    # And nothing was written.
    assert demand_log.read_events(p) == []


def test_privacy_field_check_is_case_insensitive(tmp_path):
    p = tmp_path / 'd.jsonl'
    with pytest.raises(ValueError):
        demand_log.log_unmet('body', identity={'Query': 'leak'}, path=p)


def test_read_missing_file_is_empty(tmp_path):
    assert demand_log.read_events(tmp_path / 'nope.jsonl') == []


def test_append_is_additive_across_calls(tmp_path):
    p = tmp_path / 'd.jsonl'
    for i in range(5):
        demand_log.log_unmet('body', path=p, _now=f'2026-06-24T00:0{i}:00Z')
    assert len(demand_log.read_events(p)) == 5


def test_malformed_line_raises_on_read(tmp_path):
    p = tmp_path / 'd.jsonl'
    p.write_text('{"category":"body","ts":"x","identity":null}\nNOT JSON\n')
    with pytest.raises(json.JSONDecodeError):
        demand_log.read_events(p)
