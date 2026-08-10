"""Tests for mailer — the digest send leg.

Invariants under test (the reasons this module is allowed near a real list):
  - console/dry-run is the DEFAULT and writes no sent-log (a stray run is inert)
  - enqueue reads the PUBLISHED issue (permalink from canonical, subject from
    og:title) and is idempotent by week
  - recipients are subscribers.active() — a suppressed address is never mailed
  - (week, email) is delivered AT MOST ONCE (re-run is a no-op)
  - a live send is REFUSED without a postal address and an unsubscribe secret
  - the message carries a working unsubscribe (link in body + List-Unsubscribe
    header + RFC 8058 one-click)
"""
import json
import os
import stat

import pytest

import mailer
import subscribers


ISSUE_HTML = (
    '<!DOCTYPE html><html><head>'
    '<link rel="canonical" href="https://askmaddi.com/digest/2026-w30">'
    '<meta property="og:title" content="AskMaddi Digest — 2026-w30">'
    '</head><body>hi</body></html>'
)


def _publish(prod_root, week='2026-w30'):
    d = prod_root / 'browser' / 'digest' / week
    d.mkdir(parents=True)
    (d / 'index.html').write_text(ISSUE_HTML, encoding='utf-8')


class FakeTransport:
    def __init__(self, live):
        self.live = live
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


# --- enqueue -----------------------------------------------------------------

def test_enqueue_reads_published_issue(tmp_path):
    _publish(tmp_path)
    ob = tmp_path / 'outbox.jsonl'
    assert mailer.enqueue('2026-w30', prod_root=tmp_path, outbox_path=ob) == 'queued'
    row = json.loads(ob.read_text().strip())
    assert row['permalink'] == 'https://askmaddi.com/digest/2026-w30'
    assert row['subject'] == 'AskMaddi Digest — 2026-w30'
    assert row['status'] == 'queued'
    # idempotent
    assert mailer.enqueue('2026-w30', prod_root=tmp_path, outbox_path=ob) == 'exists'
    assert len(mailer._read_rows(ob)) == 1


def test_enqueue_refuses_unpublished_issue(tmp_path):
    with pytest.raises(mailer.MailerError):
        mailer.enqueue('2026-w99', prod_root=tmp_path,
                       outbox_path=tmp_path / 'o.jsonl')


# --- send: dry-run default ---------------------------------------------------

def _setup_send(tmp_path, emails, suppressed=()):
    subs = tmp_path / 'subs.jsonl'
    supp = tmp_path / 'supp.jsonl'
    for e in emails:
        subscribers.add(e, path=subs)
    for e in suppressed:
        subscribers.suppress(e, path=supp)
    ob = tmp_path / 'outbox.jsonl'
    _publish(tmp_path)
    mailer.enqueue('2026-w30', prod_root=tmp_path, outbox_path=ob)
    return subs, supp, ob, tmp_path / 'sent.jsonl'


def test_dry_run_is_default_and_records_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv('MAILER_UNSUB_SECRET', 'test-secret')
    subs, supp, ob, sent = _setup_send(tmp_path, ['a@b.co', 'c@d.co'])
    t = FakeTransport(live=False)
    summary = mailer.send_pending(transport=t, outbox_path=ob, sent_path=sent,
                                  subs_path=subs, supp_path=supp)
    assert summary['sent'] == 2 and summary['skipped'] == 0
    assert len(t.sent) == 2
    assert not sent.exists()          # dry run leaves no delivery ledger


def test_suppressed_address_never_mailed(tmp_path, monkeypatch):
    monkeypatch.setenv('MAILER_UNSUB_SECRET', 'test-secret')
    subs, supp, ob, sent = _setup_send(tmp_path, ['a@b.co', 'c@d.co'],
                                       suppressed=['a@b.co'])
    t = FakeTransport(live=False)
    summary = mailer.send_pending(transport=t, outbox_path=ob, sent_path=sent,
                                  subs_path=subs, supp_path=supp)
    assert summary['sent'] == 1
    assert t.sent[0]['To'] == 'c@d.co'


def test_no_double_send(tmp_path, monkeypatch):
    monkeypatch.setenv('MAILER_UNSUB_SECRET', 'test-secret')
    monkeypatch.setenv('MAILER_POSTAL_ADDRESS', 'A.I. Sciences LLC, CO')
    subs, supp, ob, sent = _setup_send(tmp_path, ['a@b.co', 'c@d.co'])
    t1 = FakeTransport(live=True)     # live -> writes the delivery ledger
    s1 = mailer.send_pending(transport=t1, outbox_path=ob, sent_path=sent,
                             subs_path=subs, supp_path=supp)
    assert s1['sent'] == 2 and s1['skipped'] == 0
    assert stat.S_IMODE(os.stat(sent).st_mode) == 0o600
    t2 = FakeTransport(live=True)
    s2 = mailer.send_pending(transport=t2, outbox_path=ob, sent_path=sent,
                             subs_path=subs, supp_path=supp)
    assert s2['sent'] == 0 and s2['skipped'] == 2
    assert t2.sent == []


def test_empty_outbox_is_a_noop(tmp_path):
    summary = mailer.send_pending(transport=FakeTransport(live=False),
                                  outbox_path=tmp_path / 'none.jsonl',
                                  sent_path=tmp_path / 's.jsonl',
                                  subs_path=tmp_path / 'subs.jsonl',
                                  supp_path=tmp_path / 'supp.jsonl')
    assert summary['sent'] == 0 and summary['week_id'] is None


# --- live guardrails ---------------------------------------------------------

def test_live_refused_without_postal(tmp_path, monkeypatch):
    monkeypatch.delenv('MAILER_POSTAL_ADDRESS', raising=False)
    monkeypatch.setenv('MAILER_UNSUB_SECRET', 'test-secret')
    subs, supp, ob, sent = _setup_send(tmp_path, ['a@b.co'])
    with pytest.raises(mailer.MailerError):
        mailer.send_pending(transport=FakeTransport(live=True),
                            outbox_path=ob, sent_path=sent,
                            subs_path=subs, supp_path=supp)


def test_live_refused_without_unsub_secret(tmp_path, monkeypatch):
    monkeypatch.setenv('MAILER_POSTAL_ADDRESS', 'A.I. Sciences LLC, CO')
    monkeypatch.delenv('MAILER_UNSUB_SECRET', raising=False)
    subs, supp, ob, sent = _setup_send(tmp_path, ['a@b.co'])
    with pytest.raises(mailer.MailerError):
        mailer.send_pending(transport=FakeTransport(live=True),
                            outbox_path=ob, sent_path=sent,
                            subs_path=subs, supp_path=supp)


def test_get_transport_refuses_live_without_smtp(monkeypatch):
    monkeypatch.delenv('MAILER_SMTP_USER', raising=False)
    monkeypatch.delenv('MAILER_SMTP_PASS', raising=False)
    assert not mailer.get_transport(live=False).live      # console default
    with pytest.raises(mailer.MailerError):
        mailer.get_transport(live=True)


# --- message shape -----------------------------------------------------------

def test_message_carries_working_unsubscribe(tmp_path):
    row = {'week_id': '2026-w30',
           'subject': 'AskMaddi Digest — 2026-w30',
           'permalink': 'https://askmaddi.com/digest/2026-w30'}
    unsub = 'https://askmaddi.com/unsubscribe?e=a%40b.co&t=deadbeef'
    msg = mailer.build_message(row, 'a@b.co', unsub,
                               from_addr='Maddi <info@aisciencecenter.com>',
                               postal='A.I. Sciences LLC, Woodland Park CO')
    assert msg['To'] == 'a@b.co'
    assert msg['Subject'] == 'AskMaddi Digest — 2026-w30'
    assert unsub in msg['List-Unsubscribe']
    assert 'info@aisciencecenter.com' in msg['List-Unsubscribe']
    assert msg['List-Unsubscribe-Post'] == 'List-Unsubscribe=One-Click'
    # both a text and an html part, unsubscribe + postal in the visible body
    text = msg.get_body(preferencelist=('plain',)).get_content()
    html = msg.get_body(preferencelist=('html',)).get_content()
    assert unsub in text and 'A.I. Sciences LLC' in text
    assert unsub in html and 'A.I. Sciences LLC' in html
