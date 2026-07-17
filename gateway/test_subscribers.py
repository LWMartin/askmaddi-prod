"""Tests for subscribers — the email vault."""
import json
import os
import stat

import subscribers


def test_add_valid(tmp_path):
    p = tmp_path / 's.jsonl'
    assert subscribers.add('Lee@Example.COM', path=p) == 'added'
    rec = json.loads(p.read_text().strip())
    assert rec['email'] == 'lee@example.com'    # normalized
    assert rec['source'] == 'site'


def test_dedupe_exact_after_normalize(tmp_path):
    p = tmp_path / 's.jsonl'
    assert subscribers.add('a@b.co', path=p) == 'added'
    assert subscribers.add('  A@B.CO ', path=p) == 'exists'
    assert subscribers.count(p) == 1


def test_invalid_rejected_no_write(tmp_path):
    p = tmp_path / 's.jsonl'
    for bad in ('', 'nope', 'a@b', 'a b@c.com', '@x.com', 'a@.com',
                'a' * 300 + '@x.com', None):
        assert subscribers.add(bad, path=p) == 'invalid'
    assert not p.exists()


def test_file_created_0600(tmp_path):
    p = tmp_path / 's.jsonl'
    subscribers.add('a@b.co', path=p)
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600


def test_source_whitelisted(tmp_path):
    p = tmp_path / 's.jsonl'
    subscribers.add('a@b.co', source='<img onerror=x>', path=p)
    rec = json.loads(p.read_text().strip())
    assert rec['source'] == 'site'


def test_count_skips_torn_lines(tmp_path):
    p = tmp_path / 's.jsonl'
    subscribers.add('a@b.co', path=p)
    with open(p, 'a', encoding='utf-8') as fh:
        fh.write('{"email": "torn\n')
        fh.write('garbage\n')
    subscribers.add('c@d.co', path=p)
    assert subscribers.count(p) == 2
