"""Tests for analytics_log — the Phase 0 measurement store."""
import json

import analytics_log


def test_outbound_written_and_whitelisted(tmp_path):
    p = tmp_path / 'a.jsonl'
    rec = analytics_log.log_event('outbound', category='Camera',
                                  retailer='amazon', path=p)
    assert rec['event'] == 'outbound'
    assert rec['category'] == 'camera'          # lowercased
    assert rec['retailer'] == 'amazon'
    on_disk = json.loads(p.read_text().strip())
    assert on_disk == rec


def test_unknown_retailer_coerced_never_freetext(tmp_path):
    p = tmp_path / 'a.jsonl'
    rec = analytics_log.log_event('outbound', category='camera',
                                  retailer='<script>evil</script>', path=p)
    assert rec['retailer'] == 'other'
    assert 'script' not in p.read_text()


def test_unknown_engine_coerced(tmp_path):
    p = tmp_path / 'a.jsonl'
    rec = analytics_log.log_event('ai_referral', engine='DEFINITELY_NOT_REAL',
                                  path=p)
    assert rec['engine'] == 'other'


def test_known_engines_pass(tmp_path):
    p = tmp_path / 'a.jsonl'
    for eng in ('chatgpt', 'perplexity', 'gemini', 'copilot', 'claude'):
        rec = analytics_log.log_event('ai_referral', engine=eng, path=p)
        assert rec['engine'] == eng


def test_unknown_event_type_dropped(tmp_path):
    p = tmp_path / 'a.jsonl'
    assert analytics_log.log_event('pageview', category='camera',
                                   path=p) is None
    assert not p.exists()


def test_category_capped_and_defaulted(tmp_path):
    p = tmp_path / 'a.jsonl'
    rec = analytics_log.log_event('outbound', category='x' * 200,
                                  retailer='ebay', path=p)
    assert len(rec['category']) == 40
    rec2 = analytics_log.log_event('outbound', category='',
                                   retailer='ebay', path=p)
    assert rec2['category'] == 'unknown'


def test_no_forbidden_fields_in_records(tmp_path):
    p = tmp_path / 'a.jsonl'
    rec = analytics_log.log_event('outbound', category='camera',
                                  retailer='amazon', path=p)
    for f in analytics_log._FORBIDDEN_FIELDS:
        assert f not in rec


def test_read_counts_aggregates(tmp_path):
    p = tmp_path / 'a.jsonl'
    analytics_log.log_event('outbound', category='camera', retailer='amazon',
                            path=p)
    analytics_log.log_event('outbound', category='camera', retailer='amazon',
                            path=p)
    analytics_log.log_event('outbound', category='camera', retailer='ebay',
                            path=p)
    analytics_log.log_event('outbound', category='tripod', retailer='ebay',
                            path=p)
    analytics_log.log_event('ai_referral', engine='perplexity', path=p)
    analytics_log.log_event('ai_referral', engine='perplexity', path=p)
    analytics_log.log_event('ai_referral', engine='chatgpt', path=p)
    counts = analytics_log.read_counts(p)
    assert counts['outbound']['camera'] == {'amazon': 2, 'ebay': 1}
    assert counts['outbound']['tripod'] == {'ebay': 1}
    assert counts['ai_referral'] == {'perplexity': 2, 'chatgpt': 1}


def test_read_counts_skips_torn_lines(tmp_path):
    p = tmp_path / 'a.jsonl'
    analytics_log.log_event('outbound', category='camera', retailer='amazon',
                            path=p)
    with open(p, 'a', encoding='utf-8') as fh:
        fh.write('{"event": "outbound", "cat\n')      # torn line
        fh.write('not json at all\n')
    analytics_log.log_event('ai_referral', engine='gemini', path=p)
    counts = analytics_log.read_counts(p)
    assert counts['outbound']['camera'] == {'amazon': 1}
    assert counts['ai_referral'] == {'gemini': 1}


def test_read_counts_missing_file(tmp_path):
    counts = analytics_log.read_counts(tmp_path / 'nope.jsonl')
    assert counts == {'outbound': {}, 'ai_referral': {}}
