"""aerial_remint_plan: the read-only review artifact for aerial delist+re-mint.

Covers the signals it actually promises — contamination detection (doubled-brand
vs cruft), the core-identity match that decides re-mint fuel, aerial scoping, and
that build_plan wires spine + queue + spool + ledger into per-row verdicts.
Read-only: no test asserts any write beyond the opt-in --emit-slugs file.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'gateway'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import aerial_remint_plan as arp  # noqa: E402


def test_doubled_brand_regex():
    assert arp._DOUBLED_BRAND.match('dji-dji-air-3')
    assert arp._DOUBLED_BRAND.match('hoverair-hoverair-x1-pro')
    assert not arp._DOUBLED_BRAND.match('dji-air-3')          # clean single brand
    assert not arp._DOUBLED_BRAND.match('sony-a7-iv')


def test_aerial_scoping():
    assert arp._is_aerial('dji-mavic-3', 'DJI', 'Mavic 3')          # brand
    assert arp._is_aerial('some-drone-x', 'Acme', 'Racing Drone')   # keyword
    assert not arp._is_aerial('sony-a7-iv', 'Sony', 'A7 IV')        # not aerial


def test_core_drops_condition_noise_and_dup_brand():
    dirty = arp._core('DJI', 'DJI Mavic 2 Pro Only Flies Great')
    clean = arp._core('DJI', 'Mavic 2 Pro')
    # identity tokens survive, condition words are gone, single 'dji'
    assert {'mavic', '2', 'pro', 'dji'} <= dirty
    assert 'flies' not in dirty and 'great' not in dirty
    assert arp._match(dirty, [clean])          # same product matches across stores


def test_match_needs_two_shared_tokens():
    a = arp._core('DJI', 'Mavic 3')
    assert not arp._match(a, [arp._core('Sony', 'A7 IV')])   # unrelated → no match
    assert arp._match(a, [arp._core('DJI', 'Mavic 3 Cine')]) # variant → contains core


def _mini(tmp_path, skus, queue=None, spool=None):
    (tmp_path / 'data').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'data' / 'skus.json').write_text(json.dumps({'skus': skus}))
    (tmp_path / 'data' / 'work_queue.json').write_text(
        json.dumps({'queue': queue or {}}))
    spool_path = tmp_path / 'spool.json'
    spool_path.write_text(json.dumps(spool if spool is not None else []))
    return (str(tmp_path / 'data' / 'skus.json'),
            str(tmp_path / 'data' / 'work_queue.json'), str(spool_path))


def test_build_plan_flags_contaminated_with_fuel(tmp_path):
    sp, qp, spool = _mini(
        tmp_path,
        skus={
            'dji-dji-mavic-2-pro-only-flies-great': {'vendor': 'DJI', 'model': 'DJI Mavic 2 Pro Only Flies Great'},
            'dji-mavic-3': {'vendor': 'DJI', 'model': 'Mavic 3'},         # clean → not a target
            'sony-a7-iv': {'vendor': 'Sony', 'model': 'A7 IV'},           # not aerial → skipped
        },
        queue={'dji-dji-mavic-2-pro-only-flies-great': {'state': 'corpus_thin'}},
        spool=[{'vendor': 'DJI', 'model': 'DJI Mavic 2 Pro'}],           # re-mint fuel
    )
    rows = arp.build_plan(skus_path=sp, queue_path=qp, spool_path=spool,
                          ledger_path=str(tmp_path / 'noledger.json'))
    by = {r['slug']: r for r in rows}
    assert 'sony-a7-iv' not in by                                        # aerial filter
    dirty = by['dji-dji-mavic-2-pro-only-flies-great']
    assert dirty['contaminated'] and dirty['reason'] == 'doubled-brand'
    assert dirty['state'] == 'corpus_thin'
    assert dirty['will_remint'] is True                                  # spool fuel found
    clean = by['dji-mavic-3']
    assert not clean['contaminated']


def test_build_plan_flags_drop_when_no_spool_fuel(tmp_path):
    sp, qp, spool = _mini(
        tmp_path,
        skus={'autel-autel-evo-nano-pristine': {'vendor': 'Autel', 'model': 'Autel Evo Nano Pristine Condition'}},
        spool=[{'vendor': 'DJI', 'model': 'DJI Mavic 3'}],              # unrelated → no fuel
    )
    rows = arp.build_plan(skus_path=sp, queue_path=qp, spool_path=spool,
                          ledger_path=str(tmp_path / 'noledger.json'))
    r = rows[0]
    assert r['contaminated'] and r['will_remint'] is False              # delist→drop, flagged


def test_emit_slugs_writes_targets_only(tmp_path):
    sp, qp, spool = _mini(
        tmp_path,
        skus={
            'dji-dji-air-3-only': {'vendor': 'DJI', 'model': 'DJI Air 3 Only'},
            'dji-mavic-3': {'vendor': 'DJI', 'model': 'Mavic 3'},
        },
        spool=[{'vendor': 'DJI', 'model': 'DJI Air 3'}],
    )
    out = tmp_path / 'targets.txt'
    arp.main(['--skus-path', sp, '--queue-path', qp, '--spool-path', spool,
              '--ledger-path', str(tmp_path / 'noledger.json'),
              '--emit-slugs', str(out)])
    written = out.read_text().split()
    assert 'dji-dji-air-3-only' in written
    assert 'dji-mavic-3' not in written        # clean entry is not a delist target
