"""Shopify Hydrogen / Remix metafield spec extractor.

A Hydrogen storefront (Remix SSR over the Shopify Storefront API — Peak Design,
and a growing class of DTC brands) renders its spec table CLIENT-SIDE from a
rich-text metafield. So the bare DOM carries no <dl>/<table> spec surface, and
even a headless render leaves the "Specs" accordion unmounted until a human
clicks it (proven live 2026-08-04: a 7s headless wait still yielded zero spec
rows). But the metafield DATA ships in the initial HTML anyway: the SSR loader
embeds it in the `window.__remixContext` hydration blob, under
`state.loaderData.<product-route>.rootProduct.infoBlocks`. This module lifts the
specs from THAT blob.

Why this beats the headless/proxy path it replaced:
  - one POLITE fetch, no browser, no residential proxy — never touches the
    robots-evasion / IP-proxy lines `ingest.polite_fetch` draws on purpose;
  - deterministic and cheap (JSON parse, no Chrome, no click choreography);
  - GENERAL by construction. The transport (remixContext -> infoBlocks
    rich-text) is Hydrogen's shape, not Peak Design's — a new Hydrogen brand is
    one line in `harvest.SPEC_SURFACES` (extractor='remix_metafield'), no code.

The rich-text PARSE (`parse_rich_text_specs`) reads the common authoring
convention: a **bold** run is a spec key, the plain run(s) that follow are its
newline-delimited value(s). Other rich-text shapes (heading-delimited, list
metafields) can be added here as brands that use them are registered — the
extraction stays additive, never a rewrite.
"""

from __future__ import annotations

import json
import re
from typing import Iterator, Optional

_CONTEXT_MARKER = "window.__remixContext"


def extract_remix_context(html: str) -> Optional[dict]:
    """Lift and parse the `window.__remixContext = {...}` hydration blob.

    Uses balanced-brace matching, NOT a lazy regex: the blob is ~150 KB and the
    product specs live deep inside it, so a `\\{.*?\\}` capture truncates at the
    first `}` and silently drops the specs (the bug that first made this page
    look like it needed headless). Returns None if the marker is absent or the
    slice will not parse — a non-Hydrogen page, handled by the caller.
    """
    start = html.find(_CONTEXT_MARKER)
    if start == -1:
        return None
    open_idx = html.find("{", start)
    if open_idx == -1:
        return None
    depth = 0
    end_idx = -1
    for i in range(open_idx, len(html)):
        c = html[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end_idx = i
                break
    if end_idx == -1:
        return None
    try:
        return json.loads(html[open_idx:end_idx + 1])
    except (ValueError, TypeError):
        return None


def find_root_product(ctx: dict) -> Optional[dict]:
    """Navigate the hydration blob to the product route's `rootProduct`.

    Keyed on the PRESENCE of a `rootProduct` dict rather than the route name
    (`routes/($lang).products.$handle`), so a locale/route-shape change doesn't
    break the lookup. Returns None if no product route is loaded (e.g. a
    collection or content page).
    """
    if not isinstance(ctx, dict):
        return None
    state = ctx.get("state", ctx)
    loader_data = state.get("loaderData") if isinstance(state, dict) else None
    if not isinstance(loader_data, dict):
        return None
    for value in loader_data.values():
        if isinstance(value, dict) and isinstance(value.get("rootProduct"), dict):
            return value["rootProduct"]
    return None


def _iter_rich_text_blocks(root_product: dict) -> Iterator[tuple[Optional[str], str]]:
    """Yield (section_title, rich_text_value) for every metafield under infoBlocks.

    `title` is the metafield's own heading (Shopify wraps it as {"value": ...}),
    used as the harvest section scope. Walks the whole infoBlocks subtree so the
    shape of the wrapper (references.nodes[], or a flatter arrangement) doesn't
    matter.
    """
    info_blocks = root_product.get("infoBlocks")
    if info_blocks is None:
        return

    def walk(node):
        if isinstance(node, dict):
            text_content = node.get("textContent")
            if isinstance(text_content, dict) and isinstance(text_content.get("value"), str):
                title = node.get("title")
                if isinstance(title, dict):
                    title = title.get("value")
                yield (title if isinstance(title, str) else None, text_content["value"])
            for child in node.values():
                yield from walk(child)
        elif isinstance(node, list):
            for child in node:
                yield from walk(child)

    yield from walk(info_blocks)


def _clean_value(runs: list[str]) -> str:
    """Join value runs, split on newlines, drop footnotes, join sub-values."""
    raw = "".join(runs)
    lines = [ln.strip() for ln in raw.split("\n")]
    # Drop blanks and footnote lines ("*Optimized for pro setups ..."), then
    # strip trailing footnote markers off kept lines ("9.1 kg (20 lbs)*").
    kept = [ln.rstrip("*").strip() for ln in lines if ln and not ln.startswith("*")]
    return " | ".join(kept)


def parse_rich_text_specs(value: str) -> list[tuple[str, str]]:
    """Parse a Shopify rich-text metafield into (key, value) spec pairs.

    Convention: a **bold** text run opens a spec key; the plain run(s) until the
    next bold run are its value (newline-delimited sub-values joined by ' | ',
    the same bundling shape the Sony <dl> harvester emits, so rung-2 span
    extraction splits them downstream). Returns [] on unparseable input.
    """
    try:
        doc = json.loads(value)
    except (ValueError, TypeError):
        return []

    runs: list[tuple[str, bool]] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                runs.append((node.get("value", ""), bool(node.get("bold"))))
            for child in node.get("children", []):
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(doc)

    pairs: list[tuple[str, str]] = []
    key: Optional[str] = None
    values: list[str] = []

    def flush():
        if key:
            cleaned = _clean_value(values)
            if cleaned:
                pairs.append((key, cleaned))

    for text, bold in runs:
        if bold and text.strip():
            flush()
            key = text.strip()
            values = []
        elif not bold:
            values.append(text)
    flush()
    return pairs


def harvest_remix_metafields(html: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """Full path: HTML -> (key, value, section) spec rows, or [] if not Hydrogen.

    Never raises on a non-Hydrogen or product-less page — returns []. The
    harvest layer treats an empty list from a REGISTERED surface as a loud
    curation signal (selector drift), which is correct here too: a Hydrogen host
    that stops yielding rows has changed its metafield shape.
    """
    ctx = extract_remix_context(html)
    if ctx is None:
        return []
    root_product = find_root_product(ctx)
    if root_product is None:
        return []

    rows: list[tuple[str, str, tuple[str, ...]]] = []
    for title, value in _iter_rich_text_blocks(root_product):
        section = (title,) if title else ()
        for key, val in parse_rich_text_specs(value):
            rows.append((key, val, section))
    return rows
