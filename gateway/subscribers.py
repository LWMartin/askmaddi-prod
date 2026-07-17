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
import json
import os
import re
import time
from pathlib import Path

SUBSCRIBERS_PATH = Path(__file__).parent.parent / 'data' / 'subscribers.jsonl'

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
