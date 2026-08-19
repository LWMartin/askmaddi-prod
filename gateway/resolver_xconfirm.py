"""resolver_xconfirm — rung D, the authoritative Icecat/Wikidata cross-confirm.

Given a GTIN/MPN or Brand+ProductCode recovered by an earlier rung (carried in
`hint`), confirms the product is a single distinct real product, surfaces
predecessor/competitor relations from the graph, and sets `deterministic=True`
on an exact GTIN or Brand+Code join. This is the one rung that fills
`relations` — the raw material the contamination-entry proposal needs.

THE AMBIGUITY RULE: when Wikidata reports more than one product across eras
for this name (the panasonic-lumix-l10 vs 2007 DMC-L10 class), resolve()
returns `ambiguous=True`, `identity=None`, and confidence below FLOOR, so the
orchestrator routes to /admin instead of guessing a spine.

`icecat` / `wikidata` are injected clients (same discipline as
GemmaDisambiguator): None falls back to a live box client. Offline tests
always inject mocks, so routing is unit-tested without network:

  icecat(gtin=None, brand=None, product_code=None) -> dict|None
      hit: {"brand", "name", "gtin", "mpn"}; miss: None
  wikidata(query: str) -> list[dict]
      each: {"qid", "label", "inception", "predecessor": [str], "competitor": [str]}
      len>1 with differing inception years == ambiguous across eras.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

FLOOR = 0.70
_SOURCE = "icecat_wikidata"

_ICECAT_API = "https://live.icecat.biz/api"
_WIKIDATA_API = "https://www.wikidata.org/w/api.php"
_UA = "AskMaddi-resolver-xconfirm/0.1 (https://askmaddi.com; identity cross-confirm)"


def _live_icecat(gtin=None, brand=None, product_code=None):
    """Real Icecat lookup (box only — needs ICECAT_USERNAME/ICECAT_API_TOKEN and
    network egress the factory sandbox does not have). Same request shape as
    probe_icecat_coverage.py. Never raises; any miss or error is just a miss."""
    username = os.environ.get("ICECAT_USERNAME", "")
    if not username or not (gtin or (brand and product_code)):
        return None
    params = ({"GTIN": str(gtin)} if gtin
              else {"Brand": brand, "ProductCode": str(product_code).upper()})
    params.update({"lang": "en", "shopname": username, "content": "essentialinfo"})
    headers = {"User-Agent": _UA}
    token = os.environ.get("ICECAT_API_TOKEN", "")
    if token:
        headers["api-token"] = token
    req = urllib.request.Request(_ICECAT_API + "?" + urllib.parse.urlencode(params),
                                  headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    info = (data.get("data") or {}).get("GeneralInfo") or {}
    if not info:
        return None
    return {
        "brand": info.get("Brand") or brand,
        "name": info.get("Title") or info.get("ProductName"),
        "gtin": info.get("GTIN") or gtin,
        "mpn": info.get("ProductCode") or product_code,
    }


def _wikidata_labels(qids):
    """Batch-resolve QIDs -> English labels. Best-effort; missing -> the QID."""
    if not qids:
        return {}
    try:
        req = urllib.request.Request(
            _WIKIDATA_API + "?" + urllib.parse.urlencode({
                "action": "wbgetentities", "ids": "|".join(qids), "props": "labels",
                "languages": "en", "format": "json"}),
            headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            entities = (json.loads(resp.read().decode("utf-8", "replace"))
                        .get("entities") or {})
    except Exception:
        return {}
    return {qid: ((entities.get(qid) or {}).get("labels") or {})
            .get("en", {}).get("value", qid) for qid in qids}


def _claim_year(claims, prop):
    for c in claims.get(prop, []):
        t = (((c.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}).get("time")
        if t:
            return t.lstrip("+").split("-")[0]
    return None


def _claim_qids(claims, prop):
    out = []
    for c in claims.get(prop, []):
        qid = (((c.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}).get("id")
        if qid:
            out.append(qid)
    return out


def _live_wikidata(query):
    """Real Wikidata lookup (box only). Best-effort: predecessor via P155
    ('follows'). Wikidata has no clean 'competitor' property, so the live path
    always reports competitor=[] (mocks in tests carry it explicitly)."""
    if not query:
        return []
    try:
        req = urllib.request.Request(
            _WIKIDATA_API + "?" + urllib.parse.urlencode({
                "action": "wbsearchentities", "search": query, "language": "en",
                "format": "json", "type": "item", "limit": 5}),
            headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            hits = json.loads(resp.read().decode("utf-8", "replace")).get("search") or []
    except Exception:
        return []

    out = []
    for h in hits:
        qid = h.get("id")
        if not qid:
            continue
        try:
            req = urllib.request.Request(
                _WIKIDATA_API + "?" + urllib.parse.urlencode({
                    "action": "wbgetentities", "ids": qid, "props": "claims",
                    "format": "json"}),
                headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                claims = ((json.loads(resp.read().decode("utf-8", "replace"))
                           .get("entities") or {}).get(qid) or {}).get("claims") or {}
        except Exception:
            claims = {}
        predecessor_qids = _claim_qids(claims, "P155")
        labels = _wikidata_labels(predecessor_qids)
        out.append({
            "qid": qid,
            "label": h.get("label", ""),
            "inception": _claim_year(claims, "P571"),
            "predecessor": [labels.get(q, q) for q in predecessor_qids],
            "competitor": [],
        })
    return out


def _wikidata_query(brand, canonical_model, mpn, gtin):
    if brand and canonical_model:
        return f"{brand} {canonical_model}"
    return canonical_model or brand or mpn or gtin


def resolve(target, hint, *, icecat=None, wikidata=None):
    """Rung D. Cross-confirm identity via Icecat and/or Wikidata.

    Returns SourceResolution|None. `target` and `hint` are read the same way
    (hint wins) so callers may pass one dict for both, as earlier rungs do.
    """
    if icecat is None:
        icecat = _live_icecat
    if wikidata is None:
        wikidata = _live_wikidata

    hint = hint or {}
    target = target or {}

    gtin = hint.get("gtin") or target.get("gtin")
    mpn = hint.get("mpn") or target.get("mpn")
    brand = hint.get("brand") or target.get("brand")
    canonical_model = hint.get("canonical_model") or target.get("canonical_model")

    has_join_key = bool(gtin) or bool(mpn) or (bool(brand) and bool(canonical_model))
    if not has_join_key:
        return None

    icecat_hit = icecat(gtin=gtin, brand=brand, product_code=canonical_model or mpn)
    wikidata_hits = wikidata(_wikidata_query(brand, canonical_model, mpn, gtin)) or []

    if not icecat_hit and not wikidata_hits:
        return None

    if len(wikidata_hits) > 1:
        eras = {h.get("inception") for h in wikidata_hits if h.get("inception")}
        if len(eras) > 1:
            return {
                "source": _SOURCE,
                "identity": None,
                "confidence": round(FLOOR - 0.30, 2),
                "deterministic": False,
                "aliases": [h["label"] for h in wikidata_hits if h.get("label")],
                "relations": {"predecessor": [], "competitor": []},
                "raw": {"icecat": icecat_hit, "wikidata": wikidata_hits},
                "why": (f"{len(wikidata_hits)} distinct products across eras "
                        f"({', '.join(sorted(eras))}) match this name — ambiguous, "
                        "routing to human review instead of guessing a spine"),
                "ambiguous": True,
            }

    wd = wikidata_hits[0] if wikidata_hits else {}
    deterministic = bool(icecat_hit)

    aliases = []
    for name in ((icecat_hit or {}).get("name"), wd.get("label")):
        if name and name not in aliases:
            aliases.append(name)

    why_bits = []
    if icecat_hit:
        join_kind = "gtin" if gtin and icecat_hit.get("gtin") == gtin else "brand+product_code"
        why_bits.append(f"{join_kind} exact join via Icecat")
    if wd:
        why_bits.append(f"Wikidata confirms a single distinct product ({wd.get('qid')})")

    return {
        "source": _SOURCE,
        "identity": {
            "gtin": (icecat_hit or {}).get("gtin") or gtin,
            "mpn": (icecat_hit or {}).get("mpn") or mpn,
            "brand": (icecat_hit or {}).get("brand") or brand,
            "canonical_model": canonical_model or (icecat_hit or {}).get("name") or wd.get("label"),
        },
        "confidence": 1.0 if deterministic else (0.75 if wd else FLOOR),
        "deterministic": deterministic,
        "aliases": aliases,
        "relations": {
            "predecessor": list(wd.get("predecessor") or []),
            "competitor": list(wd.get("competitor") or []),
        },
        "raw": {"icecat": icecat_hit, "wikidata": wikidata_hits},
        "why": "; ".join(why_bits) or "cross-confirm",
    }
