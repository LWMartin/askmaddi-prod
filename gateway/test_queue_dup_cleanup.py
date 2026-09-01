"""Tests for the one-shot dup-cleanup worklist queuer (offline, tmp stores)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_queue          # noqa: E402
import queue_dup_cleanup as q  # noqa: E402


def _seed(tmp_path, skus):
    p = tmp_path / 'skus.json'
    p.write_text(json.dumps({'version': '0.1.0', 'skus': skus}))
    return str(p)


def _e(vendor, model, *, mpn='', gtin=None, epid='', facet='drone'):
    return {'vendor': vendor, 'model': model, 'facet': facet, 'gtin': gtin,
            'identity': {'mpn': mpn, 'epid': epid},
            'marketplace_ids': {'ebay_epid': epid}}


def test_shared_mpn_pair_is_one_cluster(tmp_path):
    skus = _seed(tmp_path, {
        'skydio-2': _e('Skydio', 'Skydio 2', mpn='SKY300NA'),
        'skydio-2-sdrc2v1': _e('Skydio', 'Skydio 2 SDRC2V1', mpn='SKY300NA'),
    })
    import skus_registry
    clusters = q.find_clusters(skus_registry.load_registry(skus))
    assert len(clusters) == 1
    kind, value, slugs = clusters[0]
    assert (kind, value) == ('mpn', 'SKY300NA')
    assert slugs == ['skydio-2', 'skydio-2-sdrc2v1']


def test_pair_sharing_many_ids_collapses_to_one(tmp_path):
    skus = _seed(tmp_path, {
        'autel-a': _e('Autel', 'EVO II Pro', mpn='102000410', gtin='00889520011624', epid='5052510946'),
        'autel-b': _e('Autel', 'EVO 2 Pro V3 Rugged', mpn='102000410', gtin='00889520011624', epid='5052510946'),
    })
    import skus_registry
    clusters = q.find_clusters(skus_registry.load_registry(skus))
    assert len(clusters) == 1                 # not 3 (one per shared id)
    assert clusters[0][0] == 'mpn'            # strongest id is the representative


def test_placeholder_mpn_is_not_clustered(tmp_path):
    skus = _seed(tmp_path, {
        'dji-avata-2': _e('DJI', 'Avata 2', mpn='Dose not apply'),
        'dji-mavic-4-pro': _e('DJI', 'Mavic 4 Pro', mpn='Does not apply'),
    })
    import skus_registry
    assert q.find_clusters(skus_registry.load_registry(skus)) == []


def test_commit_enqueues_and_is_idempotent(tmp_path):
    skus = _seed(tmp_path, {
        'sony-a7r': _e('Sony', 'A7R', mpn='ILCE7RM5B', facet='body'),
        'sony-a7-v': _e('Sony', 'A7 V', mpn='ILCE7RM5B', facet='body'),
    })
    rq = tmp_path / 'review_queue.json'
    q.main(['--commit', '--skus-path', skus, '--queue-path', str(rq)])
    pend = review_queue.load_pending(rq)
    assert len(pend) == 1
    assert pend[0]['reason'] == 'duplicate_identity_contradiction'
    assert pend[0]['collision_with'] == 'sony-a7-v'      # senior sibling named
    assert pend[0]['proposed_slug'] == 'sony-a7r'        # junior is the subject
    q.main(['--commit', '--skus-path', skus, '--queue-path', str(rq)])
    assert len(review_queue.load_pending(rq)) == 1        # idempotent


def test_dry_run_writes_nothing(tmp_path):
    skus = _seed(tmp_path, {
        'sony-a7r': _e('Sony', 'A7R', mpn='ILCE7RM5B', facet='body'),
        'sony-a7-v': _e('Sony', 'A7 V', mpn='ILCE7RM5B', facet='body'),
    })
    rq = tmp_path / 'review_queue.json'
    q.main(['--skus-path', skus, '--queue-path', str(rq)])   # no --commit
    assert not rq.exists()
