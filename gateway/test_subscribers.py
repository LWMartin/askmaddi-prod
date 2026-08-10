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


# --- unsubscribe tokens + suppression ---------------------------------------

def test_unsub_token_roundtrip():
    t = subscribers.unsubscribe_token('Lee@Example.com', secret='k')
    assert t and len(t) == 32
    assert subscribers.verify_unsubscribe_token('lee@example.com', t, secret='k')
    # normalized input yields the same token (case/space-insensitive)
    assert subscribers.unsubscribe_token(' LEE@example.com ', secret='k') == t


def test_unsub_token_rejects_forgery_and_blanks():
    t = subscribers.unsubscribe_token('a@b.co', secret='k')
    assert not subscribers.verify_unsubscribe_token('a@b.co', t, secret='other')
    assert not subscribers.verify_unsubscribe_token('a@b.co', '', secret='k')
    assert not subscribers.verify_unsubscribe_token('a@b.co', t + 'x', secret='k')
    # no secret configured -> no token, and nothing validates (fail closed)
    assert subscribers.unsubscribe_token('a@b.co', secret='') is None
    assert not subscribers.verify_unsubscribe_token('a@b.co', 'x', secret='')


def test_suppress_idempotent_and_0600(tmp_path):
    import os
    import stat
    p = tmp_path / 'supp.jsonl'
    assert subscribers.suppress('A@B.co', path=p) == 'suppressed'
    assert subscribers.suppress('a@b.co', path=p) == 'exists'   # normalized dupe
    assert subscribers.suppress('nope', path=p) == 'invalid'
    assert subscribers.is_suppressed('a@b.co', path=p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_active_is_subscribers_minus_suppressed(tmp_path):
    subs = tmp_path / 's.jsonl'
    supp = tmp_path / 'x.jsonl'
    for e in ('a@b.co', 'c@d.co', 'e@f.co'):
        subscribers.add(e, path=subs)
    subscribers.suppress('c@d.co', path=supp)
    assert subscribers.active(subs_path=subs, supp_path=supp) == ['a@b.co', 'e@f.co']
