"""
demand_log.py — append-only signal of UNMET product demand.
=============================================================
Demand-factory upstream primitive (maddi-skus-registry, review-queue decision
LOCKED 2026-06-24). Fired at the moment of an UNMET tap — a user reaching for a
product AskMaddi does not have a card for — BEFORE and INDEPENDENT of any build
attempt.

Why this is its own store, decoupled from the review queue:
  The review queue only ever holds the SUBSET of unmet taps that resolved to a
  real product AND tripped the slug gate. Demand is broader: it includes resolver
  failures, out-of-catalog taps, and (future) wants for products that aren't
  Amazon/eBay items at all. Capturing demand inside the queue record would
  silently scope it to "things that resolved far enough to get a slug conflict"
  and lose exactly the signal most worth having — where to expand next. So demand
  is logged here, upstream, where the tap happens.

  This is the durable signal. It measures WANT, not catalog coverage, so it keeps
  working when AskMaddi expands past its current affiliate catalog.

Privacy (LOCKED): category + timestamp + resolved identity (when there was one).
We NEVER persist the user's raw search/query text. This honors the existing
/ping line ("category only, never the query"). The thing tapped resolves to a
known product identity or to null; freetext is never written to disk.

Append-only JSONL: one event per line. A write-heavy signal stream wants append
semantics, not a whole-file rewrite per event — so this is a single open-append,
not the temp-file+os.replace dance skus_registry uses for its mutable map. The
append of one short line under O_APPEND is atomic on POSIX for the line sizes we
write, so concurrent unmet taps interleave cleanly without a half-line.
"""
import json
import os
import time
from pathlib import Path

DEMAND_LOG_PATH = Path(__file__).parent.parent / 'data' / 'demand_log.jsonl'

# Fields we will NEVER persist, asserted at write time so a future caller can't
# quietly start logging the query. The privacy line is enforced in code, not
# just documented.
_FORBIDDEN_FIELDS = ('query', 'q', 'search', 'raw_query', 'text', 'user', 'ip')


def _scrub_identity(identity):
    """Return identity unchanged if it's a plain resolved-identity dict, else None.

    Defensive: a caller must pass the resolved eBay identity block (epid, title,
    etc.) or nothing. We do not deep-inspect or transform it — the resolver
    already produced a privacy-clean identity — but we refuse anything that isn't
    a dict so a stray string (e.g. an accidental raw query) can't land in the log
    masquerading as identity.
    """
    if identity is None:
        return None
    if not isinstance(identity, dict):
        return None
    return identity


def log_unmet(category, identity=None, path=DEMAND_LOG_PATH, _now=None):
    """Append one unmet-demand event.

    Parameters
    ----------
    category : str
        The product category bucket (same controlled vocab cards use: 'body',
        'lens', 'support', ...). The ONLY demand dimension besides time and the
        resolved identity. Defaults to 'unknown' if falsy — we still want the
        event; a missing category is itself signal.
    identity : dict | None
        The resolved eBay identity block IF the tap resolved to a real product;
        None when it didn't (resolver failure, out-of-catalog tap). Either way
        the demand event is logged — the null-identity case is the most
        interesting demand, because it's want we currently can't fulfill.

    Returns the event dict that was written (handy for tests/callers); the side
    effect is one appended JSONL line.

    Raises ValueError if a forbidden (query-bearing) field is smuggled in via
    identity — the privacy line is enforced, not merely intended.
    """
    ident = _scrub_identity(identity)
    if ident is not None:
        lowered = {str(k).lower() for k in ident.keys()}
        leaked = lowered & set(_FORBIDDEN_FIELDS)
        if leaked:
            raise ValueError(
                f"demand_log refuses to persist forbidden field(s) {sorted(leaked)}: "
                f"the demand log is category + timestamp + resolved identity only, "
                f"never the raw query (privacy line, /ping parity)."
            )

    ts = _now if _now is not None else time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    event = {
        'category': category or 'unknown',
        'ts': ts,
        'identity': ident,
    }

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False) + '\n'
    # O_APPEND so concurrent writers never clobber; one short line is atomic.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode('utf-8'))
    finally:
        os.close(fd)
    return event


def read_events(path=DEMAND_LOG_PATH):
    """Read all demand events (tolerant of a missing file → empty list).

    Skips blank lines; a malformed line is a real error the caller should see,
    not silently dropped — same discipline as load_registry refusing a corrupt
    skus.json. Provided for tests and for the eventual demand-dashboard reader;
    the log itself is write-mostly.
    """
    path = Path(path)
    if not path.exists():
        return []
    events = []
    for ln in path.read_text(encoding='utf-8').splitlines():
        ln = ln.strip()
        if not ln:
            continue
        events.append(json.loads(ln))
    return events
