"""ENABLED_SITES doctrine guard.

Amazon was removed from the scrape path on 2026-07-27 — the same day Associates
was reinstated, and BECAUSE it was reinstated. Two independent reasons, either
one sufficient on its own:

  1. The Associates agreement forbids displaying Amazon price, availability,
     star ratings, review counts or imagery without Creators API credentials,
     and the scrape path existed to render exactly that. Leaving the links
     untagged does not cure it: the rule binds the Associate's site, not merely
     the tagged links on it.
  2. It forbids automated access to Amazon at all, so the fetch itself is
     exposure regardless of what we choose to display.

Amazon is NOT gone from the product — it is a tagged, price-free exit
(#amazon-crosscheck in index.html, plus the per-card rung from
build_site.amazon_cta). We surface OUR product data and link out.

This test exists because re-adding 'amazon' here is a one-word change that
looks harmless in a diff and silently restores both exposures. If Creators API
credentials ever land, delete this test DELIBERATELY, in its own commit, with
the credential source named in the message.

Parsed from source rather than imported: app_production imports flask_cors,
which is not installed in the sandbox (25 pre-existing collection errors), and
this guard must run everywhere the suite runs.
"""

import ast
from pathlib import Path

APP = Path(__file__).parent / "app_production.py"


def _enabled_sites():
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "ENABLED_SITES":
                    return set(ast.literal_eval(node.value))
    raise AssertionError("ENABLED_SITES not found in app_production.py")


def test_amazon_is_not_scraped():
    assert "amazon" not in _enabled_sites(), (
        "Amazon must not be in ENABLED_SITES: scraping it as an Associate is "
        "exposure both for the fetch and for displaying its price/rating data. "
        "The Amazon rung is a tagged, price-free LINK. See module docstring."
    )


def test_ebay_still_enabled():
    # Guard against over-correction: eBay is served by the official Browse API
    # and its manifest must still load so the frontend lists it as a source.
    assert "ebay" in _enabled_sites()


def test_amazon_manifest_kept_on_disk():
    # Deliberately retained: restoring the rung is a one-line ENABLED_SITES
    # change IF credentials ever land. Absence would mean someone deleted it,
    # which is a different decision than the one recorded here.
    assert (Path(__file__).parent / "manifests" / "amazon.json").exists()
