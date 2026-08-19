"""Visible contract for resolver_xconfirm (rung D — authoritative cross-confirm).

Authored 2026-08-04 as the executable expectation BEFORE the worker. This is the
one rung that (a) fills relations (predecessor/competitor) — the raw material the
orchestrator's contamination-entry proposal needs — and (b) ADJUDICATES AMBIGUITY:
when a name maps to more than one product across eras (the panasonic-lumix-l10 vs
2007 DMC-L10 class), it abstains (identity=None, ambiguous=True, confidence below
floor) so the orchestrator routes to /admin rather than guessing.

INJECTED CLIENT CONTRACT (offline-testable, live-provable — same discipline as
GemmaDisambiguator):
  icecat(gtin=None, brand=None, product_code=None) -> dict|None
      hit: {"brand","name","gtin","mpn"}  ; miss: None
  wikidata(query: str) -> list[dict]
      each: {"qid","label","inception","predecessor":[str],"competitor":[str]}
      len>1 with differing inception years == ambiguous across eras.

SourceResolution shape (spec §Cells) + optional 'ambiguous' key. Floor = 0.70.
"""
from __future__ import annotations

from resolver_xconfirm import resolve

FLOOR = 0.70


def test_gtin_join_is_deterministic_and_fills_relations():
    hint = {"gtin": "4548736135574", "brand": "Sony", "canonical_model": "FX3"}
    icecat = lambda gtin=None, brand=None, product_code=None: {
        "brand": "Sony", "name": "FX3", "gtin": "4548736135574", "mpn": "ILME-FX3"}
    wikidata = lambda q: [{
        "qid": "Q1", "label": "Sony FX3", "inception": "2021",
        "predecessor": ["Sony FS5"], "competitor": ["Canon EOS C70"]}]
    r = resolve(hint, hint, icecat=icecat, wikidata=wikidata)
    assert r is not None
    assert r["source"] == "icecat_wikidata"
    assert r["deterministic"] is True
    assert not r.get("ambiguous")
    assert r["identity"]["gtin"] == "4548736135574"
    assert r["identity"]["mpn"] == "ILME-FX3"
    assert r["relations"]["predecessor"] == ["Sony FS5"]
    assert r["relations"]["competitor"] == ["Canon EOS C70"]
    assert r["confidence"] >= FLOOR


def test_ambiguous_across_eras_abstains_to_human_the_L10_case():
    # The live case: herald says "new fixed-lens L10", but the 2007 DMC-L10 exists.
    hint = {"brand": "Panasonic", "canonical_model": "Lumix L10"}
    icecat = lambda gtin=None, brand=None, product_code=None: None
    wikidata = lambda q: [
        {"qid": "Q1", "label": "Panasonic Lumix DMC-L10", "inception": "2007",
         "predecessor": [], "competitor": []},
        {"qid": "Q2", "label": "Panasonic Lumix L10", "inception": "2026",
         "predecessor": [], "competitor": []}]
    r = resolve(hint, hint, icecat=icecat, wikidata=wikidata)
    assert r is not None
    assert r["ambiguous"] is True
    assert r["identity"] is None          # never guess a spine identity under ambiguity
    assert r["confidence"] < FLOOR        # forces the orchestrator to /admin


def test_no_hint_identity_and_no_client_hit_returns_none():
    hint = {"brand": "", "canonical_model": ""}
    r = resolve(hint, hint,
                icecat=lambda **k: None,
                wikidata=lambda q: [])
    assert r is None


def test_single_wikidata_match_is_confirmed_not_ambiguous():
    hint = {"brand": "Canon", "canonical_model": "EOS R6"}
    icecat = lambda gtin=None, brand=None, product_code=None: {
        "brand": "Canon", "name": "EOS R6", "gtin": "0013803314915", "mpn": "4082C002"}
    wikidata = lambda q: [{
        "qid": "Q1", "label": "Canon EOS R6", "inception": "2020",
        "predecessor": ["Canon EOS 6D Mark II"], "competitor": ["Sony A7 III"]}]
    r = resolve(hint, hint, icecat=icecat, wikidata=wikidata)
    assert r is not None
    assert not r.get("ambiguous")
    assert r["relations"]["predecessor"] == ["Canon EOS 6D Mark II"]
