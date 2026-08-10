"""
subscribers.py — the vault: append-only email-capture store.
============================================================
Phase 0 of maddi-distribution v2.0: the owned audience is the one channel no
platform can revoke, so capture starts on day one — before there is anything
to send. A weekly "new cards" note comes later (Phase 3); this module only
guarantees the list exists, is valid, and is safe.

PII DOCTRINE (this store is DIFFERENT from every other data/ file):
  - subscribers.jsonl contains email addresses — personal data.
  - It must NEVER be committed, banked, bot_pushed, or otherwise enter git.
    Gitignored in the same commit that introduces this writer. The nightly
    banker's allowlist does not include it and must never grow to.
  - It is written 0600 (owner-only) — the gateway user (askmaddi) is the sole
    reader. The spine's cross-user 0640 relaxation does NOT apply here; no
    other user has any business reading the list.
  - Backup is a deliberate human decision (Lee's), outside this module.

Validation: syntactic only (one @, a dot in the domain, sane length). We are
not an email verifier; a confirmation send belongs to the future mailer, not
the capture seam. Honeypot filtering happens at the endpoint (the bot fills
the hidden field; we 200 it and write nothing — never teach the bot).

Dedupe: exact-match scan on append. O(n) per insert is correct at every list
size this store will see before a real mailing system replaces it; an index
would be premature and a second file to corrupt.
"""
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path

SUBSCRIBERS_PATH = Path(__file__).parent.parent / 'data' / 'subscribers.jsonl'
# Suppression list (unsubscribes). Same PII doctrine as subscribers.jsonl:
# NEVER committed/banked/pushed, written 0600, box-local. Kept SEPARATE from
# subscribers.jsonl (append-only capture stays intact) — the active list is
# subscribers MINUS suppressions, computed at send time. Honoring an
# unsubscribe is a legal obligation (CAN-SPAM), so this file is load-bearing.
SUPPRESSIONS_PATH = Path(__file__).parent.parent / 'data' / 'suppressions.jsonl'

_EMAIL_RE = re.compile(r'^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$')
MAX_EMAIL_LEN = 254  # RFC 5321 path limit


def normalize(email):
    """Lowercase + strip. Returns None if syntactically invalid."""
    e = str(email or '').strip().lower()
    if not e or len(e) > MAX_EMAIL_LEN or not _EMAIL_RE.match(e):
        return None
    return e


def _existing(path):
    emails = set()
    if not path.exists():
        return emails
    with open(path, 'r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get('email'):
                emails.add(rec['email'])
    return emails


def add(email, source='site', path=None, now=None):
    """Add a subscriber. Returns one of:
       'added'    — written
       'exists'   — already on the list (no write)
       'invalid'  — failed syntactic validation (no write)
    source is whitelisted ('site' only for now) so the endpoint can't be used
    to write free text into the record."""
    target = Path(path) if path else SUBSCRIBERS_PATH
    e = normalize(email)
    if e is None:
        return 'invalid'
    if e in _existing(target):
        return 'exists'

    record = {
        'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)),
        'email': e,
        'source': 'site' if source == 'site' else 'site',
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    # Create 0600 BEFORE first write so the file never exists world-readable.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception:
        # os.fdopen owns fd on success; on fdopen failure close it ourselves.
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return 'added'


def count(path=None):
    target = Path(path) if path else SUBSCRIBERS_PATH
    return len(_existing(target))


# ---------------------------------------------------------------------------
# Unsubscribe + suppression (CAN-SPAM: every send MUST offer opt-out and honor
# it). The token is a keyed HMAC over the normalized address, so an unsubscribe
# link is unguessable without the server secret but needs no per-user state and
# no expiry (unsubscribing is idempotent and low-risk). The secret lives in
# gateway/.env (MAILER_UNSUB_SECRET), never git — same store as every other
# gateway secret.
# ---------------------------------------------------------------------------

def _unsub_secret(secret=None):
    """Resolve the HMAC secret: explicit arg (tests) > env > None."""
    return secret if secret is not None else os.environ.get('MAILER_UNSUB_SECRET')


def unsubscribe_token(email, secret=None):
    """Stable, unguessable opt-out token for an address. Returns None if the
    address is invalid or no secret is configured (so callers fail closed
    rather than mint a forgeable/empty token)."""
    e = normalize(email)
    key = _unsub_secret(secret)
    if e is None or not key:
        return None
    return hmac.new(key.encode('utf-8'), e.encode('utf-8'),
                    hashlib.sha256).hexdigest()[:32]


def verify_unsubscribe_token(email, token, secret=None):
    """Constant-time check that `token` matches `email`. False on any missing
    piece — never let a blank token validate."""
    expected = unsubscribe_token(email, secret=secret)
    if not expected or not token:
        return False
    return hmac.compare_digest(expected, str(token))


def suppress(email, path=None, now=None):
    """Record an opt-out. Idempotent: returns 'suppressed' on first record,
    'exists' if already suppressed, 'invalid' if the address is malformed.
    Written 0600 like the vault — it is a list of real addresses."""
    target = Path(path) if path else SUPPRESSIONS_PATH
    e = normalize(email)
    if e is None:
        return 'invalid'
    if e in _existing(target):
        return 'exists'
    record = {
        'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)),
        'email': e,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return 'suppressed'


def is_suppressed(email, path=None):
    e = normalize(email)
    if e is None:
        return False
    target = Path(path) if path else SUPPRESSIONS_PATH
    return e in _existing(target)


def active(subs_path=None, supp_path=None):
    """The mailable list: captured subscribers MINUS suppressions, as a sorted
    list of normalized addresses. This is the ONLY function a sender should use
    to choose recipients — never read subscribers.jsonl directly for a send."""
    subs = _existing(Path(subs_path) if subs_path else SUBSCRIBERS_PATH)
    supp = _existing(Path(supp_path) if supp_path else SUPPRESSIONS_PATH)
    return sorted(subs - supp)
