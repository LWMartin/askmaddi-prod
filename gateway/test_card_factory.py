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


def test_build_card_runner_passes_prior_card_with_askmaddi_prod(tmp_path,
                                                                monkeypatch):
    """Mint-date wire (2026-07-27): the factory passes the PUBLISHED card
    explicitly, derived from the same root as --spine.

    Before this, every rebuild reset freshness.created_at — 11 of 11 published
    cards had lost 1-18 days of mint history by the time it was found. The
    build root alone is not sufficient as a witness: /var/lib/askmaddi-cards
    can be wiped, and at 2 cards/day a silent reset would have propagated
    across the whole catalog during the rebuild."""
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
    rc, _, _ = runner({'slug': 'sony-a7iv', 'label': 'Sony A7 IV',
                       'category': 'body'})
    assert rc == 0
    cmd = captured['cmd']
    i = cmd.index('--prior-card')
    assert cmd[i + 1] == '/opt/askmaddi-prod/data/cards/sony-a7iv.json'


def test_prior_card_passed_even_when_absent(tmp_path, monkeypatch):
    """Deliberately NOT gated on existence, unlike --spine.

    A first build has no published card and that is normal, not an error.
    build_card/assemble degrade to the build root and report it on stdout, so
    the drip log carries the evidence. Gating here would silently drop the
    argv and make the fallback invisible — the exact shape of the original
    bug."""
    captured = {}

    class _Proc:
        returncode = 0
        stderr = ''
        stdout = ''

    monkeypatch.setattr(cf.subprocess, 'run',
                        lambda cmd, cwd, capture_output, text:
                        (captured.__setitem__('cmd', cmd), _Proc())[1])
    runner = cf.build_card_runner(
        build_card_path='/tmp/aggregator-build/build_card.py',
        askmaddi_prod=str(tmp_path / 'nonexistent-root'),
        enrich_client='mock')
    runner({'slug': 'brand-new-sku', 'label': 'Brand New', 'category': 'body'})
    assert '--prior-card' in captured['cmd']


def test_no_prod_root_no_prior_card(tmp_path, monkeypatch):
    """Sandbox/manual posture: no root means no derived published card, same
    as --spine."""
    captured = {}

    class _Proc:
        returncode = 0
        stderr = ''
        stdout = ''

    monkeypatch.setattr(cf.subprocess, 'run',
                        lambda cmd, cwd, capture_output, text:
                        (captured.__setitem__('cmd', cmd), _Proc())[1])
    runner = cf.build_card_runner(
        build_card_path='/tmp/aggregator-build/build_card.py',
        enrich_client='mock')
    runner({'slug': 'sony-a7iv', 'label': 'Sony A7 IV', 'category': 'body'})
    assert '--prior-card' not in captured['cmd']


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


# ─── failure capture rework (2026-07-17) ─────────────────────────────────────
# Pins the a7-v shape: advisory [!] warning as stderr's last line, real fatal
# in stdout — old capture kept the warning, new capture surfaces the fatal.

class _Proc:
    def __init__(self, rc, out='', err=''):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _mk_runner(tmp_path, monkeypatch, proc):
    monkeypatch.setattr(cf.subprocess, 'run', lambda cmd, **kw: proc)
    return cf.build_card_runner(
        build_card_path=tmp_path / 'build_card.py',
        out_root=str(tmp_path / 'spool'), enrich_client='mock')


_REC = {'slug': 'sony-a7-v', 'label': 'Sony A7 V', 'category': 'body'}


def test_failure_detail_surfaces_fatal_over_advisory(tmp_path, monkeypatch):
    proc = _Proc(1,
                 out="stage fetch: 0 pages\nERROR: fetch produced no corpus\n",
                 err="  [!] sku_id 'sony-a7-v' not in SKU_IDENTITY_REGISTRY "
                     "and no identity override - falling back\n")
    runner = _mk_runner(tmp_path, monkeypatch, proc)
    rc, _, detail = runner(_REC)
    assert rc == 1
    assert 'ERROR: fetch produced no corpus' in detail       # the fatal leads
    assert detail.startswith('exit 1')
    assert '[stderr]' in detail                              # advisory kept as context


def test_failure_detail_traceback_takes_exception_line(tmp_path, monkeypatch):
    proc = _Proc(1, err="Traceback (most recent call last):\n"
                        "  File \"build_card.py\", line 9\n"
                        "PermissionError: [Errno 13] denied: data/skus.json\n")
    runner = _mk_runner(tmp_path, monkeypatch, proc)
    _, _, detail = runner(_REC)
    assert 'PermissionError' in detail


def test_build_log_persisted_on_success_and_failure(tmp_path, monkeypatch):
    for rc in (0, 1):
        proc = _Proc(rc, out=f'stage log rc={rc}\n', err='warn\n')
        runner = _mk_runner(tmp_path, monkeypatch, proc)
        runner(_REC)
        log = (tmp_path / 'spool' / 'sony-a7-v' / 'build.log').read_text()
        assert f'exit: {rc}' in log
        assert f'stage log rc={rc}' in log     # stdout no longer discarded
        assert 'warn' in log


def test_log_write_failure_never_converts_outcome(tmp_path, monkeypatch):
    """The best-effort guard lives INSIDE _persist_build_log — a failing
    write must neither raise nor flip the build result."""
    proc = _Proc(0, out='ok\n')
    monkeypatch.setattr(cf.subprocess, 'run', lambda cmd, **kw: proc)
    monkeypatch.setattr(cf.Path, 'write_text',
                        lambda self, *a, **kw: (_ for _ in ()).throw(
                            OSError('disk full')))
    runner = cf.build_card_runner(
        build_card_path=tmp_path / 'build_card.py',
        out_root=str(tmp_path / 'spool'), enrich_client='mock')
    rc, _, detail = runner(_REC)      # would raise here if the guard leaked
    assert rc == 0
    assert detail == 'ok'


def test_detail_bounded_under_work_queue_cap(tmp_path, monkeypatch):
    proc = _Proc(1, out='x' * 2000, err='ERROR: ' + 'y' * 2000)
    runner = _mk_runner(tmp_path, monkeypatch, proc)
    _, _, detail = runner(_REC)
    assert len(detail) <= 480


def test_runner_passes_require_spine_with_spine(tmp_path, monkeypatch):
    """Factory posture: wherever --spine goes, --require-spine goes."""
    captured = {}

    def fake_run(cmd, **kw):
        captured['cmd'] = cmd
        return _Proc(0, out='ok')

    monkeypatch.setattr(cf.subprocess, 'run', fake_run)
    runner = cf.build_card_runner(
        build_card_path=tmp_path / 'build_card.py',
        out_root=str(tmp_path / 'spool'), enrich_client='mock',
        askmaddi_prod=str(tmp_path / 'prod'))
    runner(_REC)
    cmd = captured['cmd']
    assert '--spine' in cmd
    assert '--require-spine' in cmd


def test_runner_no_prod_root_no_require_spine(tmp_path, monkeypatch):
    """Sandbox/manual posture unchanged: no prod root, no spine, no mandate."""
    captured = {}

    def fake_run(cmd, **kw):
        captured['cmd'] = cmd
        return _Proc(0, out='ok')

    monkeypatch.setattr(cf.subprocess, 'run', fake_run)
    runner = cf.build_card_runner(
        build_card_path=tmp_path / 'build_card.py',
        out_root=str(tmp_path / 'spool'), enrich_client='mock')
    runner(_REC)
    assert '--require-spine' not in captured['cmd']


# ─── enrich_partial: the factory's fifth verb (2026-07-17) ───────────────────

def test_enrich_partial_no_strike_and_resume(tmp_path, monkeypatch):
    """rc 6 = still working: no attempt burned, state back to resolved,
    resume_stage set — and the NEXT runner invocation resumes at enrich
    instead of rebuilding the corpus (the 1609/929 checkpoint-orphan loop
    that parked sony-a7-v at 3/3 twice)."""
    qp = tmp_path / 'wq.json'
    wq.enroll('sony-a7-v', 'Sony A7 V', 'body', path=qp)  # enroll lands at 'resolved'

    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        return _Proc(6, out='PARTIAL: 240/929 checkpointed (689 pending). '
                            'Re-run the same command to resume.')

    monkeypatch.setattr(cf.subprocess, 'run', fake_run)
    runner = cf.build_card_runner(
        build_card_path=tmp_path / 'build_card.py',
        out_root=str(tmp_path / 'spool'), enrich_client='mock')

    out = cf.tick(runner, path=qp)
    assert out['action'] == 'enrich_partial'
    rec = wq.load_queue(qp)['queue']['sony-a7-v']
    assert rec['state'] == 'resolved'            # re-claimable next tick
    assert rec['build_attempts'] == 0            # no strike burned
    assert rec['resume_stage'] == 'enrich'
    assert rec['enrich_partial_ticks'] == 1
    assert 'PARTIAL' in rec['last_error']
    assert '--start-stage' not in cmds[0]        # first tick: full chain

    out2 = cf.tick(runner, path=qp)              # next tick resumes
    assert out2['action'] == 'enrich_partial'
    i = cmds[1].index('--start-stage')
    assert cmds[1][i + 1] == 'enrich'
    rec2 = wq.load_queue(qp)['queue']['sony-a7-v']
    assert rec2['enrich_partial_ticks'] == 2
    assert rec2['build_attempts'] == 0           # still unburned


def test_enrich_partial_then_success_clears_resume(tmp_path, monkeypatch):
    qp = tmp_path / 'wq.json'
    wq.enroll('sony-a7-v', 'Sony A7 V', 'body', path=qp)
    rcs = iter([6, 0])
    monkeypatch.setattr(cf.subprocess, 'run',
                        lambda cmd, **kw: _Proc(next(rcs), out='x'))
    runner = cf.build_card_runner(
        build_card_path=tmp_path / 'build_card.py',
        out_root=str(tmp_path / 'spool'), enrich_client='mock')
    cf.tick(runner, path=qp)
    out = cf.tick(runner, path=qp)
    assert out['action'] == 'built'
    rec = wq.load_queue(qp)['queue']['sony-a7-v']
    assert rec['state'] == 'review_ready'
    assert 'resume_stage' not in rec             # lifecycle closed


# ── single-flight gate (2026-07-19: midnight cap-reset pile-up, sony-a7c) ─────
def test_single_flight_second_acquire_locked_out(tmp_path):
    """flock is per open-file-description: a second open of the same path in the
    same process contends exactly like a second cron-fired factory process."""
    lock = tmp_path / '.factory.lock'
    fd = cf.single_flight(lock)
    assert fd is not None
    assert cf.single_flight(lock) is None   # held -> locked out
    import os
    os.close(fd)


def test_single_flight_release_allows_reacquire(tmp_path):
    import os
    lock = tmp_path / '.factory.lock'
    fd = cf.single_flight(lock)
    os.close(fd)                             # process exit stand-in
    fd2 = cf.single_flight(lock)
    assert fd2 is not None                   # kernel released it, no stale state
    os.close(fd2)


def test_single_flight_lockfile_group_writable(tmp_path):
    """Mirror work_queue's fchmod discipline: 0664 regardless of umask, so the
    pipeline group keeps rw on shared runtime files."""
    import os
    lock = tmp_path / '.factory.lock'
    fd = cf.single_flight(lock)
    try:
        assert (os.fstat(fd).st_mode & 0o777) == 0o664
    finally:
        os.close(fd)


def test_main_once_locked_out_exits_zero_and_skips_tick(tmp_path, monkeypatch, capsys):
    """A locked-out cron tick is a routine skip, not an error: exit 0, one log
    line in the standard outcome format, and tick() never runs."""
    import os
    lock = tmp_path / '.factory.lock'
    holder = cf.single_flight(lock)          # simulate the running invocation
    called = []
    monkeypatch.setattr(cf, 'tick', lambda *a, **k: called.append(1) or {'action': 'idle'})
    rc = cf.main(['--once', '--lock', str(lock)])
    assert rc == 0
    assert called == []                      # gate sits BEFORE any queue touch
    assert "locked_out" in capsys.readouterr().out
    os.close(holder)


def test_main_once_free_lock_ticks(tmp_path, monkeypatch, capsys):
    """With the lock free, --once proceeds to exactly one tick."""
    called = []
    monkeypatch.setattr(cf, 'tick', lambda *a, **k: called.append(1) or {'action': 'idle', 'remaining': 2})
    rc = cf.main(['--once', '--lock', str(tmp_path / '.factory.lock')])
    assert rc == 0
    assert called == [1]
    assert "idle" in capsys.readouterr().out


# ── drip-log timestamps (papercut fix, 2026-07-20) ───────────────────────────

def test_drip_log_lines_are_utc_stamped(tmp_path, monkeypatch, capsys):
    """Every line the cron appends to drip.log self-dates: ISO-8601 Z prefix on
    both the --once outcome line and the locked_out line. Third-session papercut
    — 30 undatable idle ticks in the 2026-07-20 morning verify."""
    import re
    stamp = r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \[factory\] tick: '
    lock = tmp_path / '.factory.lock'

    monkeypatch.setattr(cf, 'tick', lambda *a, **k: {'action': 'idle', 'remaining': 2})
    rc = cf.main(['--once', '--lock', str(lock)])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert re.match(stamp, out), out

    # main() keeps its lock fd for the process lifetime, so within this test
    # process the first call above IS the running invocation — the second call
    # is naturally locked out. No manual holder needed.
    rc = cf.main(['--once', '--lock', str(lock)])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert re.match(stamp, out) and 'locked_out' in out, out
