"""
test_card_factory.py — offline tests for the card-factory drip loop.

All tests inject a FAKE runner (no build_card.py subprocess, no box, no network)
and a tmp work_queue.json. They prove:
  - tick claims oldest resolved, builds, advances to review_ready, attaches card_path
  - cap enforcement: tick returns 'capped' once built_today hits cap; failed builds
    do NOT burn cap
  - retry routing: a failing runner sends a record back to resolved, then parks it
    failed when the budget is spent
  - idle: tick returns 'idle' when nothing is resolved
  - run_loop: deterministic with injected sleep/log; max_ticks bounds it; built and
    idle naps differ
"""
import time
import pytest

import work_queue as wq
import card_factory as cf


def _path(tmp_path):
    return tmp_path / 'work_queue.json'


def _ok_runner(card_path='out/x/card.json'):
    """A runner that always succeeds."""
    def run(record):
        return 0, f"out/{record['slug']}/card.json", 'ok'
    return run


def _fail_runner(detail='exit 2'):
    """A runner that always fails."""
    def run(record):
        return 2, f"out/{record['slug']}/card.json", detail
    return run


def _seed(p, *slugs, cap_attempts=None):
    for i, s in enumerate(slugs):
        kw = {}
        if cap_attempts is not None:
            kw['max_attempts'] = cap_attempts
        wq.enroll(s, s.replace('-', ' ').title(), 'body', path=p, **kw)
        time.sleep(0.005)  # deterministic FIFO ordering


# ── tick: happy path ──────────────────────────────────────────────────────────
def test_tick_builds_oldest_and_advances(tmp_path):
    p = _path(tmp_path)
    _seed(p, 'sony-a7iv', 'sony-a7s-iii')
    out = cf.tick(_ok_runner(), cap=10, path=p)
    assert out['action'] == 'built'
    assert out['slug'] == 'sony-a7iv'                 # oldest first
    rec = wq.get('sony-a7iv', path=p)
    assert rec['state'] == 'review_ready'
    assert rec['card_path'] == 'out/sony-a7iv/card.json'   # attached for /admin
    assert wq.counts(path=p)['built_today'] == 1


def test_tick_idle_when_nothing_resolved(tmp_path):
    p = _path(tmp_path)
    out = cf.tick(_ok_runner(), cap=10, path=p)
    assert out['action'] == 'idle'
    assert out['remaining'] == 10


# ── tick: cap enforcement ─────────────────────────────────────────────────────
def test_tick_capped_when_budget_spent(tmp_path):
    p = _path(tmp_path)
    _seed(p, 'a', 'b', 'c')
    # cap of 2 → two builds, then capped even though 'c' is still resolved
    assert cf.tick(_ok_runner(), cap=2, path=p)['action'] == 'built'
    assert cf.tick(_ok_runner(), cap=2, path=p)['action'] == 'built'
    out = cf.tick(_ok_runner(), cap=2, path=p)
    assert out['action'] == 'capped'
    assert wq.get('c', path=p)['state'] == 'resolved'  # untouched, waits for tomorrow


def test_failed_build_does_not_burn_cap(tmp_path):
    p = _path(tmp_path)
    _seed(p, 'good', 'bad', 'good2', cap_attempts=1)
    # bad fails (parks failed, no cap burn); good + good2 should both still build
    cf.tick(_ok_runner(), cap=2, path=p)              # good -> built (cap used 1)
    # force 'bad' to be next by leaving it oldest-resolved; fail it
    cf.tick(_fail_runner(), cap=2, path=p)            # bad -> failed (no cap burn)
    out = cf.tick(_ok_runner(), cap=2, path=p)        # good2 -> built (cap used 2)
    assert out['action'] == 'built'
    assert out['slug'] == 'good2'
    assert wq.counts(path=p)['built_today'] == 2       # only clean builds counted
    assert wq.get('bad', path=p)['state'] == 'failed'


# ── tick: retry routing ───────────────────────────────────────────────────────
def test_tick_retry_then_fail(tmp_path):
    p = _path(tmp_path)
    _seed(p, 'flaky', cap_attempts=2)

    # Deterministic errors only — timeout-shaped strings route to 'cooldown'
    # (turtle doctrine) and never burn attempts. See test_tick_transient_cooldown.
    out1 = cf.tick(_fail_runner('extract: parser crashed'), cap=10, path=p)
    assert out1['action'] == 'retry'
    assert wq.get('flaky', path=p)['state'] == 'resolved'   # back for another go
    assert out1['attempts'] == 1

    out2 = cf.tick(_fail_runner('extract: parser crashed again'), cap=10, path=p)
    assert out2['action'] == 'failed'                       # budget spent
    assert wq.get('flaky', path=p)['state'] == 'failed'
    assert out2['attempts'] == 2


def test_tick_transient_cooldown(tmp_path):
    # A timeout is weather: no attempts burn, record cools down, action says so.
    p = _path(tmp_path)
    _seed(p, 'weather', cap_attempts=2)

    out = cf.tick(_fail_runner('socket.timeout: timed out'), cap=10, path=p)
    assert out['action'] == 'cooldown'
    assert out['attempts'] == 0                             # defect budget untouched
    assert out['transient_retries'] == 1
    assert out['cooldown_until'] is not None

    rec = wq.get('weather', path=p)
    assert rec['state'] == 'resolved'
    # ...but invisible to claim_next until the cooldown expires:
    assert cf.tick(_fail_runner('should never run'), cap=10, path=p)['action'] == 'idle'


def test_retry_keeps_failure_detail(tmp_path):
    p = _path(tmp_path)
    _seed(p, 'flaky', cap_attempts=1)
    cf.tick(_fail_runner('assemble: schema invalid'), cap=10, path=p)
    rec = wq.get('flaky', path=p)
    assert 'schema invalid' in rec['last_error']


# ── run_loop: deterministic with injected sleep/log ───────────────────────────
def test_run_loop_bounded_by_max_ticks(tmp_path):
    p = _path(tmp_path)
    _seed(p, 'a', 'b', 'c')
    naps, logs = [], []
    ticks = cf.run_loop(
        _ok_runner(), cap=10, tick_sleep=300, idle_sleep=1800,
        max_ticks=3, path=p,
        sleep=lambda n: naps.append(n), log=lambda m: logs.append(m),
    )
    assert ticks == 3
    assert wq.counts(path=p)['built_today'] == 3
    # 3 builds; last tick does not sleep (loop breaks) → at most 2 naps recorded
    assert all(n == 300 for n in naps)                  # working nap, not idle


def test_run_loop_idles_when_empty(tmp_path):
    p = _path(tmp_path)
    # nothing enrolled → every tick is idle, uses idle_sleep
    naps, logs = [], []
    cf.run_loop(
        _ok_runner(), cap=10, tick_sleep=300, idle_sleep=1800,
        max_ticks=2, path=p,
        sleep=lambda n: naps.append(n), log=lambda m: logs.append(m),
    )
    assert naps == [1800]                                # one nap (2nd tick breaks)
    assert any('idling' in m for m in logs)


def test_run_loop_idle_nap_after_cap(tmp_path):
    p = _path(tmp_path)
    _seed(p, 'a', 'b')
    naps = []
    # cap 1: tick1 builds (working nap 300), tick2 capped (idle nap 1800), tick3 break
    cf.run_loop(
        _ok_runner(), cap=1, tick_sleep=300, idle_sleep=1800,
        max_ticks=3, path=p,
        sleep=lambda n: naps.append(n), log=lambda m: None,
    )
    assert naps == [300, 1800]


# ── runner contract (build_card_runner shape, no subprocess) ──────────────────
def test_build_card_runner_returns_triple_shape(tmp_path, monkeypatch):
    # Stub subprocess.run so we exercise the CLI assembly without a real build.
    captured = {}

    class _Proc:
        returncode = 0
        stderr = ''
        stdout = ''

    def fake_run(cmd, cwd, capture_output, text):
        captured['cmd'] = cmd
        return _Proc()

    monkeypatch.setattr(cf.subprocess, 'run', fake_run)
    runner = cf.build_card_runner(
        build_card_path='/tmp/aggregator-build/build_card.py',
        enrich_client='mock')
    rc, card_path, detail = runner({
        'slug': 'sony-a7iv', 'label': 'Sony A7 IV', 'category': 'body',
        'seed_urls': 'fixtures/seed-urls/sony-a7iv.json',
        'aliases': ['a7iv'], 'mount': 'E',
    })
    assert rc == 0
    assert detail == 'ok'
    assert card_path.endswith('out/sony-a7iv/card.json')
    cmd = captured['cmd']
    # the assemble-stop seam + field mapping
    assert '--stop-stage' in cmd and 'assemble' in cmd
    assert '--sku-id' in cmd and 'sony-a7iv' in cmd
    assert '--sku-label' in cmd and 'Sony A7 IV' in cmd
    assert '--seed-urls' in cmd
    assert '--alias' in cmd and 'a7iv' in cmd
    assert '--mount' in cmd and 'E' in cmd
    assert '--enrich-client' in cmd and 'mock' in cmd
    # No askmaddi_prod configured -> no spine argv (sandbox/manual posture)
    assert '--spine' not in cmd


def test_build_card_runner_passes_spine_with_askmaddi_prod(tmp_path, monkeypatch):
    """images-on-spine step 4: the factory passes --spine explicitly,
    derived from the askmaddi_prod root it already knows."""
    captured = {}

    class _Proc:
        returncode = 0
        stderr = ''
        stdout = ''

    def fake_run(cmd, cwd, capture_output, text):
        captured['cmd'] = cmd
        return _Proc()

    monkeypatch.setattr(cf.subprocess, 'run', fake_run)
    runner = cf.build_card_runner(
        build_card_path='/tmp/aggregator-build/build_card.py',
        askmaddi_prod='/opt/askmaddi-prod',
        enrich_client='mock')
    rc, _, _ = runner({'slug': 'sony-a7s-iii', 'label': 'Sony A7S III',
                       'category': 'body'})
    assert rc == 0
    cmd = captured['cmd']
    i = cmd.index('--spine')
    assert cmd[i + 1] == '/opt/askmaddi-prod/data/skus.json'
    j = cmd.index('--askmaddi-prod')
    assert cmd[j + 1] == '/opt/askmaddi-prod'


def test_build_card_runner_failure_detail(tmp_path, monkeypatch):
    class _Proc:
        returncode = 2
        stderr = 'line one\n[build_card] ERROR assemble failed: schema invalid'
        stdout = ''

    monkeypatch.setattr(cf.subprocess, 'run',
                        lambda cmd, cwd, capture_output, text: _Proc())
    runner = cf.build_card_runner(build_card_path='/tmp/x/build_card.py')
    rc, card_path, detail = runner(
        {'slug': 's', 'label': 'L', 'category': 'body'})
    assert rc == 2
    assert 'schema invalid' in detail


# ── out_root: the cross-user card spool seam (decision 2026-06-30) ──────────

def test_runner_out_root_threads_spool_to_subprocess_and_card_path(tmp_path, monkeypatch):
    """The record's card_path and build_card's --out MUST be the same root —
    /admin previews record['card_path']; a divergence is an empty gate."""
    captured = {}

    def fake_run(cmd, **kw):
        captured['cmd'] = cmd
        class R: returncode, stderr, stdout = 0, '', ''
        return R()

    monkeypatch.setattr(cf.subprocess, 'run', fake_run)
    runner = cf.build_card_runner(
        build_card_path=tmp_path / 'build_card.py',
        out_root='/var/lib/askmaddi-cards', enrich_client='mock')
    rc, card_path, detail = runner({'slug': 'sony-a7s-iii',
                                    'label': 'Sony A7S III',
                                    'category': 'body'})
    assert rc == 0
    i = captured['cmd'].index('--out')
    assert captured['cmd'][i + 1] == '/var/lib/askmaddi-cards/sony-a7s-iii'
    assert card_path == '/var/lib/askmaddi-cards/sony-a7s-iii/card.json'


def test_runner_default_keeps_historical_out(tmp_path, monkeypatch):
    def fake_run(cmd, **kw):
        assert '--out' not in cmd                     # historical path: no flag
        class R: returncode, stderr, stdout = 0, '', ''
        return R()
    monkeypatch.setattr(cf.subprocess, 'run', fake_run)
    runner = cf.build_card_runner(
        build_card_path=tmp_path / 'build_card.py', enrich_client='mock')
    rc, card_path, _ = runner({'slug': 's1', 'label': 'L', 'category': 'lens'})
    assert card_path == str(tmp_path / 'out' / 's1' / 'card.json')
