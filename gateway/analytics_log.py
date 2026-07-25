"""
analytics_log.py — append-only, anonymous distribution-signal store.
====================================================================
Phase 0 of maddi-distribution v2.0 (spec'd 2026-07-17): before any distribution
push, we must be able to MEASURE. Two event families, both category-level:

  outbound     — a visitor clicked an affiliate CTA. Fields: category, retailer.
  ai_referral  — a page load arrived with an AI-engine referrer. Field: engine.

Why its own store (parallel to demand_log, not inside it):
  demand_log measures WANT (unmet taps — where to expand next). This measures
  CONVERSION SURFACE (met demand flowing outward) and CHANNEL (which AI engine
  sent the visitor). Different questions, different readers, same append-only
  JSONL discipline. Mixing them would make the demand signal noisier and give
  every future reader two schemas to skip over.

Privacy (LOCKED, inherits the /ping line: "category only, never the query"):
  - No user identifier, no IP, no URL, no query text, no email. Ever.
  - Enforced in code (_FORBIDDEN_FIELDS assertion at write time), not just
    documented — same doctrine as demand_log.
  - Values are WHITELISTED, not free-form: an event with an unknown retailer or
    engine is coerced to 'other'. Free text from the client can never land on
    disk, so the beacon cannot be abused to write arbitrary strings here.

Append-only JSONL, O_APPEND single-line atomic on POSIX (demand_log rationale).
Runtime-written on the box by the gateway (user: askmaddi) — gitignored in the
same commit that introduces this writer (clean-tree preflight failure class,
see .gitignore history: 2026-06-25 and 2026-06-30 incidents).
"""
import json
import os
import time
from pathlib import Path

ANALYTICS_LOG_PATH = Path(__file__).parent.parent / 'data' / 'analytics_log.jsonl'

_FORBIDDEN_FIELDS = ('query', 'q', 'search', 'raw_query', 'text', 'user', 'ip',
                     'email', 'url', 'href', 'referrer')

# Whitelists — the ONLY values that can be persisted. Anything else -> 'other'.
EVENT_TYPES = ('outbound', 'ai_referral')

RETAILERS = ('amazon', 'ebay', 'adorama', 'other')

# AI engines we recognize in document.referrer, keyed by the value the beacon
# sends. Additions here must be mirrored in browser/js/beacon.js.
AI_ENGINES = ('chatgpt', 'perplexity', 'gemini', 'copilot', 'claude', 'other')


def _coerce(value, whitelist):
    """Whitelist coercion: unknown/missing -> 'other'. Never free text."""
    v = str(value or '').strip().lower()
    return v if v in whitelist else 'other'


def log_event(event, category=None, retailer=None, engine=None, path=None,
              now=None):
    """Append one anonymous event. Returns the record written, or None if the
    event type is unknown (unknown types are DROPPED, not coerced — an event
    stream where 'other' events accumulate silently is a bug magnet).

    category is whatever category string the site already uses at the /ping
    seam (e.g. 'camera', 'lens'); it is length-capped and lowercased but not
    whitelisted here because the category vocabulary belongs to the card layer
    and grows with the catalog. It is never a query: the beacon reads it from
    a data-attribute the BUILD wrote, not from user input.
    """
    if event not in EVENT_TYPES:
        return None

    record = {
        'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)),
        'event': event,
        'category': (str(category or 'unknown').strip().lower()[:40]
                     or 'unknown'),
    }
    if event == 'outbound':
        record['retailer'] = _coerce(retailer, RETAILERS)
    elif event == 'ai_referral':
        record['engine'] = _coerce(engine, AI_ENGINES)

    for f in _FORBIDDEN_FIELDS:
        assert f not in record, f"privacy violation: '{f}' in analytics record"

    target = Path(path) if path else ANALYTICS_LOG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + '\n')
    return record


def read_counts(path=None):
    """Cheap aggregate reader: {'outbound': {category: {retailer: n}},
    'ai_referral': {engine: n}}. Tolerates a torn/garbage line (skip)."""
    target = Path(path) if path else ANALYTICS_LOG_PATH
    counts = {'outbound': {}, 'ai_referral': {}}
    if not target.exists():
        return counts
    with open(target, 'r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = rec.get('event')
            if ev == 'outbound':
                cat = rec.get('category', 'unknown')
                ret = rec.get('retailer', 'other')
                counts['outbound'].setdefault(cat, {})
                counts['outbound'][cat][ret] = \
                    counts['outbound'][cat].get(ret, 0) + 1
            elif ev == 'ai_referral':
                eng = rec.get('engine', 'other')
                counts['ai_referral'][eng] = \
                    counts['ai_referral'].get(eng, 0) + 1
    return counts
