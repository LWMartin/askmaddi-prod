#!/usr/bin/env python3
"""
build_spec_qa_pages.py — deterministic, factual per-card spec Q&A pages.

For each card, renders one Q&A pair per spec that is BOTH present on the card
(facts.specs) AND covered by SPEC_QUESTIONS below. This is an honest-door
surface: a spec with no value on the card gets no question, ever, and a spec
key we have no template for is silently skipped (not fabricated, not
crashed). Voice is neutral/factual — no verdict vocabulary. Each page emits
FAQPage JSON-LD (schema.org Question/Answer) and links back to the card's
own dossier page.

Usage:
  python3 tools/build_spec_qa_pages.py                 # data/cards -> browser/specs
  python3 tools/build_spec_qa_pages.py --base-url https://askmaddi.com
"""
import argparse
import json
import sys
from html import escape as _html_escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SITE_NAME = 'AskMaddi'

# spec_key -> {question: '...{name}...' template, unit: default unit word
# appended to the value when the spec's own 'unit' field is blank and the
# value doesn't already mention it}. Extending coverage is just adding an
# entry here — an unknown key is skipped, never crashes build_pages.
SPEC_QUESTIONS = {
    'resolution_mp': {
        'question': 'What is the resolution of the {name}?',
        'unit': 'megapixels',
    },
    'iso_native': {
        'question': 'What is the native ISO range of the {name}?',
        'unit': '',
    },
    'sensor_format': {
        'question': 'What sensor format does the {name} use?',
        'unit': '',
    },
    'card_slots': {
        'question': 'Does the {name} have dual card slots?',
        'unit': '',
    },
    'weight': {
        'question': 'How much does the {name} weigh?',
        'unit': 'g',
    },
    'weight_g': {
        'question': 'How much does the {name} weigh?',
        'unit': 'g',
    },
    'focal_length_mm': {
        'question': 'What is the focal length of the {name}?',
        'unit': 'mm',
    },
    'max_aperture': {
        'question': 'What is the maximum aperture of the {name}?',
        'unit': '',
    },
    'video_codecs': {
        'question': 'What video codecs does the {name} support?',
        'unit': '',
    },
    'evf_resolution_dots': {
        'question': 'What is the EVF resolution of the {name}?',
        'unit': 'dots',
    },
    'battery_model': {
        'question': 'What battery does the {name} use?',
        'unit': '',
    },
}


def esc(s):
    return _html_escape(str(s), quote=True)


def abs_url(base_url, path):
    return base_url.rstrip('/') + path


def _spec_value(entry):
    """The spec's own present value, or None if there isn't one. Only the
    'value' field counts as present — a null value is an absent spec even if
    other fields (anchor/low/high) are populated, so a partially-resolved
    spec never gets answered as if it were complete."""
    if isinstance(entry, dict):
        value = entry.get('value')
    else:
        value = entry
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _spec_unit(entry):
    if isinstance(entry, dict):
        return (entry.get('unit') or '').strip()
    return ''


def _format_answer(value, template, entry):
    unit = _spec_unit(entry) or template.get('unit', '')
    if unit and unit.lower() not in value.lower():
        return f'{value} {unit}'
    return value


def spec_qa_pairs(card):
    """Deterministically ordered (question, answer) pairs for every spec that
    is both present on the card and has a template — the honest-door filter.
    Order follows SPEC_QUESTIONS' own fixed order, independent of whatever
    order the card's specs dict happens to be in."""
    ident = card.get('identity', {}) or {}
    name = ident.get('display_name') or card.get('card_id', '')
    specs = ((card.get('facts') or {}).get('specs')) or {}

    pairs = []
    for key, template in SPEC_QUESTIONS.items():
        if key not in specs:
            continue
        value = _spec_value(specs[key])
        if value is None:
            continue
        question = template['question'].format(name=name)
        answer = _format_answer(value, template, specs[key])
        pairs.append((question, answer))
    return pairs


def _faq_jsonld(base_url, card_id, pairs):
    return {
        '@context': 'https://schema.org',
        '@type': 'FAQPage',
        'url': abs_url(base_url, f'/specs/{card_id}/'),
        'mainEntity': [
            {
                '@type': 'Question',
                'name': question,
                'acceptedAnswer': {'@type': 'Answer', 'text': answer},
            }
            for question, answer in pairs
        ],
    }


def render_spec_qa_page(card, base_url, pairs):
    card_id = card['card_id']
    ident = card.get('identity', {}) or {}
    name = ident.get('display_name') or card_id
    canonical = abs_url(base_url, f'/specs/{card_id}/')
    card_url = f'/cards/{card_id}/'

    title = f'{name} specs — Q&A'
    meta_desc = f'Answers to common spec questions about the {name}, drawn only from its published specifications.'

    jsonld = json.dumps(_faq_jsonld(base_url, card_id, pairs), indent=2, ensure_ascii=False).replace('</', '<\\/')

    items_html = ''.join(
        f'<div class="spec-qa-item">'
        f'<h2 class="spec-qa-question">{esc(question)}</h2>'
        f'<p class="spec-qa-answer">{esc(answer)}</p>'
        f'</div>'
        for question, answer in pairs
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(title)} | {esc(SITE_NAME)}</title>
  <meta name="description" content="{esc(meta_desc)}">
  <link rel="canonical" href="{esc(canonical)}">
  <meta property="og:title" content="{esc(title)}">
  <meta property="og:description" content="{esc(meta_desc)}">
  <meta property="og:type" content="website">
  <meta property="og:url" content="{esc(canonical)}">
  <script type="application/ld+json">
{jsonld}
  </script>
</head>
<body>
  <div class="container">
    <article class="spec-qa-page">
      <h1 class="spec-qa-title">{esc(name)} — spec questions and answers</h1>
      <p class="spec-qa-intro">Factual answers to common spec questions about the {esc(name)}, drawn only from its published specifications.</p>
      {items_html}
    </article>
    <footer class="card-footer">
      <a href="{esc(card_url)}">← Full {esc(name)} details</a>
    </footer>
  </div>
</body>
</html>
"""


def build_pages(cards, out_dir, base_url):
    """Write /specs/<card_id>/index.html for every card with at least one
    mappable, present spec. Returns the list of page URLs written, in
    deterministic card_id order. A card with zero mappable specs is skipped
    entirely — no directory, no return entry."""
    out = Path(out_dir)
    urls = []
    for card in sorted(cards, key=lambda c: c.get('card_id', '')):
        card_id = card.get('card_id')
        if not card_id:
            continue
        pairs = spec_qa_pairs(card)
        if not pairs:
            continue
        html = render_spec_qa_page(card, base_url, pairs)
        page_dir = out / 'specs' / card_id
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / 'index.html').write_text(html, encoding='utf-8')
        urls.append(abs_url(base_url, f'/specs/{card_id}/'))
    return urls


def _load_cards(cards_dir):
    cards = []
    for path in sorted(Path(cards_dir).glob('*.json')):
        try:
            cards.append(json.loads(path.read_text(encoding='utf-8')))
        except (json.JSONDecodeError, OSError):
            continue
    return cards


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Build factual per-card spec Q&A pages from published card JSONs.')
    parser.add_argument('--cards-dir', default=str(ROOT / 'data' / 'cards'))
    parser.add_argument('--out', default=str(ROOT / 'browser'))
    parser.add_argument('--base-url', default='https://askmaddi.com')
    args = parser.parse_args(argv)

    cards = _load_cards(args.cards_dir)
    urls = build_pages(cards, args.out, args.base_url)

    print(f'build_spec_qa_pages: {len(cards)} card(s) loaded, {len(urls)} spec Q&A page(s) written to {args.out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
