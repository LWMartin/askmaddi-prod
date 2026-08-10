"""
mailer.py — the send leg: weekly digest → subscriber inboxes.
================================================================================
maddi-digest v1.0 §"Email (phase 3)": the digest is a PUBLIC PAGE first; email
is a THIN ENVELOPE pointing at that page. This module is that envelope + the
send machinery. It computes no content — the issue already lives at its
permalink; the email is a subject line, two sentences, the link, and a lawful
footer (unsubscribe + postal address, per CAN-SPAM).

DEPENDENCY INVERSION (why this is safe to ship now): sending is decoupled from
content. The page carries the surface whether or not a single email ever goes
out, so a deliverability problem here blocks nothing upstream.

TRANSPORT is pluggable and env-selected, so the sender decision is config, not
code (maddi-distribution reserves it for Lee, on deliverability merits):
  - console (DEFAULT): a dry run — prints what WOULD send, touches no network,
    no SMTP creds required. Every invocation is console unless explicitly told
    otherwise, so a stray run can never blast the list.
  - smtp: authenticated submission. v1 = Fastmail as info@aisciencecenter.com
    (that domain is SPF+DKIM+DMARC authenticated; askmaddi.com is not yet, so
    it is NOT a usable From until its DNS is set up). Swapping to a branded
    askmaddi.com From, or a transactional API later, is an env change here.

STATE (all box-local, all PII/runtime, all gitignored — see .gitignore):
  - data/digest_outbox.jsonl : {week_id, subject, permalink, rendered_at,
    status} — one row per published issue, appended by `enqueue`.
  - data/digest_sent.jsonl   : {ts, week_id, email} — one row per delivered
    email. The dedup ledger: (week_id, email) is sent AT MOST ONCE, so a
    re-run is a no-op and a subscriber who joins after an issue went out
    simply gets FUTURE issues, never a backfill blast.

RECIPIENTS come only from subscribers.active() (captured MINUS suppressed) —
this module never reads subscribers.jsonl directly, so an unsubscribe is always
honored before a send.
"""
import argparse
import json
import os
import re
import smtplib
import sys
import time
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path
from urllib.parse import quote

import subscribers

_DATA = Path(__file__).parent.parent / 'data'
OUTBOX_PATH = _DATA / 'digest_outbox.jsonl'
SENTLOG_PATH = _DATA / 'digest_sent.jsonl'
PROD_ROOT = Path(__file__).parent.parent  # askmaddi-prod checkout root


class MailerError(RuntimeError):
    """Refuse-to-send condition (missing config, no such issue, etc.)."""


# --- config (env, resolved lazily so tests/imports don't need a populated env)

def _site():
    return os.environ.get('MAILER_SITE', 'https://askmaddi.com').rstrip('/')


def _from_addr():
    return os.environ.get('MAILER_FROM', 'Maddi <info@aisciencecenter.com>')


# ---------------------------------------------------------------------------
# jsonl helpers (torn-line tolerant, matching subscribers.py)
# ---------------------------------------------------------------------------

def _read_rows(path):
    rows = []
    if not path.exists():
        return rows
    with open(path, 'r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                rows.append(rec)
    return rows


def _append_row(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    # 0600: outbox/sent-log carry subscriber addresses (sent-log) and internal
    # subjects. Created owner-only before first write, like the vault.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# enqueue: published issue -> outbox envelope
# ---------------------------------------------------------------------------

_CANON_RE = re.compile(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', re.I)
_OGTITLE_RE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', re.I)


def issue_meta(week_id, prod_root=None):
    """Read the PUBLISHED issue for its permalink + subject. The page is the
    source of truth (its canonical URL is what a reader gets); we never invent a
    URL the site might not serve. Raises MailerError if the issue is not
    published yet."""
    root = Path(prod_root) if prod_root else PROD_ROOT
    issue = root / 'browser' / 'digest' / week_id / 'index.html'
    if not issue.exists():
        raise MailerError(
            f"no published issue at browser/digest/{week_id}/ — publish it "
            f"before enqueuing (the email points at the live page)")
    html = issue.read_text(encoding='utf-8')
    m = _CANON_RE.search(html)
    permalink = m.group(1) if m else f"{_site()}/digest/{week_id}"
    t = _OGTITLE_RE.search(html)
    subject = t.group(1) if t else f"AskMaddi Digest — {week_id}"
    return {'permalink': permalink, 'subject': subject}


def enqueue(week_id, prod_root=None, outbox_path=None, now=None):
    """Append an outbox envelope for a published issue. Idempotent by week_id
    (re-enqueue returns 'exists'). Returns 'queued' | 'exists'."""
    path = Path(outbox_path) if outbox_path else OUTBOX_PATH
    for row in _read_rows(path):
        if row.get('week_id') == week_id:
            return 'exists'
    meta = issue_meta(week_id, prod_root=prod_root)
    _append_row(path, {
        'week_id': week_id,
        'subject': meta['subject'],
        'permalink': meta['permalink'],
        'rendered_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)),
        'status': 'queued',
    })
    return 'queued'


# ---------------------------------------------------------------------------
# message construction (thin envelope, multipart text+html, lawful footer)
# ---------------------------------------------------------------------------

def _bodies(subject, permalink, unsub_url, postal):
    text = (
        "This week's AskMaddi digest is up:\n"
        f"  {permalink}\n\n"
        "New research dossiers, what people reached for, the sources we set "
        "aside, and a price worth knowing. One page, no tracking.\n\n"
        "Private in. Evidence out. Always improving.\n"
        "— Maddi\n\n"
        "———\n"
        f"Unsubscribe: {unsub_url}\n"
        f"{postal}\n"
    )
    esc_link = permalink.replace('"', '%22')
    html = (
        '<div style="font:16px/1.55 -apple-system,\'Segoe UI\',Georgia,serif;'
        'color:#1e2430;max-width:520px;margin:0 auto;padding:8px">'
        f'<p>This week’s AskMaddi digest is up:</p>'
        f'<p><a href="{esc_link}" style="color:#14532d;font-weight:600">'
        f'{subject}</a></p>'
        '<p>New research dossiers, what people reached for, the sources we set '
        'aside, and a price worth knowing. One page, no tracking.</p>'
        '<p style="color:#6b7280">Private in. Evidence out. Always improving.'
        '<br>— Maddi</p>'
        '<hr style="border:none;border-top:1px solid #d8d4cc;margin:20px 0">'
        '<p style="color:#6b7280;font-size:13px">'
        f'<a href="{unsub_url}" style="color:#6b7280">Unsubscribe</a><br>'
        f'{postal}</p></div>'
    )
    return text, html


def build_message(row, recipient, unsub_url, from_addr=None, postal=None):
    """Assemble one EmailMessage for one recipient. Includes List-Unsubscribe
    (mailto + https) and List-Unsubscribe-Post for RFC 8058 one-click, which
    Gmail/Outlook reward for deliverability and which make opt-out frictionless.
    """
    from_addr = from_addr or _from_addr()
    postal = postal or os.environ.get('MAILER_POSTAL_ADDRESS', '')
    subject = row.get('subject') or f"AskMaddi Digest — {row.get('week_id')}"
    permalink = row.get('permalink') or f"{_site()}/digest/{row.get('week_id')}"
    text, html = _bodies(subject, permalink, unsub_url, postal)

    msg = EmailMessage()
    msg['From'] = from_addr
    msg['To'] = recipient
    msg['Subject'] = subject
    _, from_email = parseaddr(from_addr)
    if from_email:
        msg['Reply-To'] = from_email
        msg['List-Unsubscribe'] = (
            f"<{unsub_url}>, <mailto:{from_email}?subject=unsubscribe>")
    else:
        msg['List-Unsubscribe'] = f"<{unsub_url}>"
    msg['List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click'
    msg.set_content(text)
    msg.add_alternative(html, subtype='html')
    return msg


# ---------------------------------------------------------------------------
# transports
# ---------------------------------------------------------------------------

class ConsoleTransport:
    """Dry run: print a one-screen summary, send nothing. The default."""
    live = False

    def send(self, msg):
        print(f"    [dry-run] To: {msg['To']}  |  Subject: {msg['Subject']}")
        print(f"              List-Unsubscribe: {msg['List-Unsubscribe']}")


class SMTPTransport:
    """Authenticated submission. 465 => implicit TLS (SMTP_SSL); anything else
    => STARTTLS. Fastmail: smtp.fastmail.com:465, user=info@aisciencecenter.com,
    pass=an app-specific password."""
    live = True

    def __init__(self, host, port, user, password):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password

    def send(self, msg):
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=30) as s:
                s.login(self.user, self.password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=30) as s:
                s.ehlo()
                s.starttls()
                s.ehlo()
                s.login(self.user, self.password)
                s.send_message(msg)


def get_transport(live=False):
    """Resolve the transport. Console unless live is explicitly requested AND
    smtp is configured. Refuses live with missing SMTP creds rather than
    silently degrading to a dry run (a silent no-op on a 'live' send is the
    worst failure — you'd think it went out)."""
    if not live:
        return ConsoleTransport()
    host = os.environ.get('MAILER_SMTP_HOST', 'smtp.fastmail.com')
    port = os.environ.get('MAILER_SMTP_PORT', '465')
    user = os.environ.get('MAILER_SMTP_USER', '')
    pw = os.environ.get('MAILER_SMTP_PASS', '')
    missing = [k for k, v in (('MAILER_SMTP_USER', user),
                              ('MAILER_SMTP_PASS', pw)) if not v]
    if missing:
        raise MailerError(
            "live send requested but SMTP not configured — set "
            + ", ".join(missing) + " in gateway/.env")
    return SMTPTransport(host, port, user, pw)


# ---------------------------------------------------------------------------
# send: drain the outbox for a week
# ---------------------------------------------------------------------------

def _sent_pairs(path=None):
    path = Path(path) if path else SENTLOG_PATH
    return {(r.get('week_id'), r.get('email')) for r in _read_rows(path)}


def _record_sent(week_id, email, path=None, now=None):
    _append_row(Path(path) if path else SENTLOG_PATH, {
        'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)),
        'week_id': week_id,
        'email': email,
    })


def _target_week(outbox_path, week=None):
    """Which issue to send. Explicit --week wins; otherwise the MOST RECENTLY
    enqueued issue (last outbox row). We deliberately do NOT drain every queued
    week at once — that would let a late enqueue blast old issues."""
    rows = _read_rows(Path(outbox_path) if outbox_path else OUTBOX_PATH)
    if not rows:
        return None
    if week:
        for r in rows:
            if r.get('week_id') == week:
                return r
        raise MailerError(f"week {week} is not in the outbox — enqueue it first")
    return rows[-1]


def send_pending(week=None, live=False, transport=None,
                 outbox_path=None, sent_path=None,
                 subs_path=None, supp_path=None, throttle=0.0):
    """Send the target issue to every active subscriber who has not already
    received it. Returns a summary dict. Dry run by default; live requires SMTP
    creds AND (CAN-SPAM) a postal address AND an unsubscribe secret — an email
    with no working opt-out is unlawful, so we refuse rather than send one."""
    row = _target_week(outbox_path, week)
    if row is None:
        return {'week_id': None, 'sent': 0, 'skipped': 0,
                'note': 'outbox empty — enqueue a published issue first'}
    week_id = row['week_id']

    if transport is None:
        transport = get_transport(live=live)

    if transport.live:
        if not os.environ.get('MAILER_POSTAL_ADDRESS'):
            raise MailerError(
                "live send blocked: MAILER_POSTAL_ADDRESS is required "
                "(CAN-SPAM mandates a physical mailing address in the footer)")
        if not os.environ.get('MAILER_UNSUB_SECRET'):
            raise MailerError(
                "live send blocked: MAILER_UNSUB_SECRET is required "
                "(no secret => no valid unsubscribe link => unlawful send)")

    recipients = subscribers.active(subs_path=subs_path, supp_path=supp_path)
    already = _sent_pairs(sent_path)
    from_addr = _from_addr()
    postal = os.environ.get('MAILER_POSTAL_ADDRESS', '')

    sent = skipped = 0
    for email in recipients:
        if (week_id, email) in already:
            skipped += 1
            continue
        # Real token when a secret is set (always true on a live send, guarded
        # above); a PREVIEW placeholder only in a secret-less dry run so the
        # console output is shaped correctly.
        token = subscribers.unsubscribe_token(email) or 'PREVIEW'
        unsub_url = f"{_site()}/unsubscribe?e={quote(email)}&t={token}"
        msg = build_message(row, email, unsub_url,
                            from_addr=from_addr, postal=postal)
        transport.send(msg)
        if transport.live:
            _record_sent(week_id, email, path=sent_path)
        sent += 1
        if throttle and transport.live:
            time.sleep(throttle)

    return {'week_id': week_id, 'subject': row.get('subject'),
            'permalink': row.get('permalink'),
            'recipients': len(recipients), 'sent': sent, 'skipped': skipped,
            'live': transport.live}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    # Load gateway/.env so a plain cron/shell invocation sees the same secrets
    # the gateway service does (same pattern as resolve_pass).
    try:
        import env_bootstrap
        env_bootstrap.load_dotenv()
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="AskMaddi weekly digest mailer")
    sub = ap.add_subparsers(dest='cmd', required=True)

    q = sub.add_parser('enqueue', help='queue a published issue for sending')
    q.add_argument('--week', required=True, help='ISO week id, e.g. 2026-w30')

    s = sub.add_parser('send', help='send the queued issue (dry-run by default)')
    s.add_argument('--week', default=None,
                   help='issue to send (default: most recently enqueued)')
    s.add_argument('--live', action='store_true',
                   help='ACTUALLY send via SMTP (default: dry-run to console)')
    s.add_argument('--throttle', type=float, default=0.0,
                   help='seconds to pause between live sends')

    ls = sub.add_parser('list', help='show the outbox')

    args = ap.parse_args(argv)
    try:
        if args.cmd == 'enqueue':
            status = enqueue(args.week)
            print(f"[mailer] {args.week}: {status}")
            return 0
        if args.cmd == 'send':
            summary = send_pending(week=args.week, live=args.live,
                                   throttle=args.throttle)
            mode = 'LIVE' if summary.get('live') else 'dry-run'
            print(f"[mailer] {mode} {summary.get('week_id')}: "
                  f"sent={summary['sent']} skipped={summary['skipped']} "
                  f"of {summary.get('recipients', 0)} active")
            if not summary.get('live') and summary['sent']:
                print("[mailer] nothing was actually sent — re-run with --live")
            return 0
        if args.cmd == 'list':
            for r in _read_rows(OUTBOX_PATH):
                print(f"  {r.get('week_id')}  {r.get('status', '?'):8}  "
                      f"{r.get('subject', '')}")
            return 0
    except MailerError as e:
        print(f"[mailer] REFUSED: {e}", file=sys.stderr)
        return 2
    return 1


if __name__ == '__main__':
    sys.exit(main())
