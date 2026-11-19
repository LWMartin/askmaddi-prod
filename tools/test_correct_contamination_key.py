"""correct_contamination_key tool: dry run writes nothing; apply re-points with a
receipt; refuses a stale expect or a key not in the registry; reads the registry
piped on stdin (how it runs on the box: root pipes, askmaddi writes)."""
import io
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'gateway'))
import correct_contamination_key as t  # noqa: E402

REG = {'products': {'sony-generic': {'vendor': 'Sony', 'model': '(brand)'},
                    'sony-35-f14-gm': {'vendor': 'Sony', 'model': 'FE 35mm F1.4 GM',
                                       'self': {'aliases': ['35mm f1.4 gm']}}}}
ARGS = ['--slug', 'sony-fe-35mm-f1-4-gm', '--expect', 'sony-generic',
        '--new', 'sony-35-f14-gm', '--reason', 'first-class entry exists']


def _files(tmp_path):
    skus = tmp_path / 'skus.json'
    skus.write_text(json.dumps({'version': '0.1.0', 'skus': {'sony-fe-35mm-f1-4-gm': {
        'contamination_key': 'sony-generic', 'contamination_tier': 'brand_generic',
        'vendor': 'Sony', 'model': 'FE 35mm f/1.4 GM', 'facet': 'lens',
        'identity': {'mpn': 'SEL35F14GM'}}}}))
    reg = tmp_path / 'contamination.json'
    reg.write_text(json.dumps(REG))
    return skus, reg


def _run(argv):
    buf = io.StringIO()
    return t.main(argv, out=buf), buf.getvalue()


def test_dry_run_reports_and_writes_nothing(tmp_path):
    skus, reg = _files(tmp_path)
    before = skus.read_text()
    rc, out = _run(ARGS + ['--skus', str(skus), '--contam-json', str(reg)])
    assert rc == 0 and skus.read_text() == before
    assert "'sony-generic' -> 'sony-35-f14-gm'" in out
    assert "model 'FE 35mm F1.4 GM'" in out and 'dry run' in out


def test_apply_repoints_with_receipt(tmp_path):
    skus, reg = _files(tmp_path)
    rc, out = _run(ARGS + ['--skus', str(skus), '--contam-json', str(reg), '--apply'])
    assert rc == 0 and "corrected: now 'sony-35-f14-gm'  tier None" in out
    e = json.loads(skus.read_text())['skus']['sony-fe-35mm-f1-4-gm']
    assert e['contamination_key'] == 'sony-35-f14-gm' and 'contamination_tier' not in e
    assert e['contamination_key_provenance']['corrected_from'] == 'sony-generic'
    assert e['contamination_key_provenance']['reason'] == 'first-class entry exists'
    rc, out = _run(ARGS + ['--skus', str(skus), '--contam-json', str(reg), '--apply'])
    assert rc == 1 and 'not the expected' in out              # second apply: refused


def test_refusals_write_nothing(tmp_path):
    skus, reg = _files(tmp_path)
    before = skus.read_text()
    for argv, says in (
            (['--expect', 'sony-other'], 'not the expected'),
            (['--new', 'sony-35-typo'], 'not a product in the registry'),
            (['--slug', 'ghost'], 'no spine entry')):
        a = list(ARGS)
        for flag, val in zip(argv[::2], argv[1::2]):
            a[a.index(flag) + 1] = val
        rc, out = _run(a + ['--skus', str(skus), '--contam-json', str(reg), '--apply'])
        assert rc == 1 and says in out, (argv, out)
    rc, out = _run(ARGS + ['--skus', str(skus), '--contam-json', str(tmp_path / 'absent.json')])
    assert rc == 1 and 'registry unreadable' in out
    assert skus.read_text() == before


def test_registry_piped_on_stdin(tmp_path):
    for flag in ('-', '/dev/stdin'):     # both read the open descriptor, never re-open
        skus, reg = _files(tmp_path)
        r = subprocess.run([sys.executable, str(HERE / 'correct_contamination_key.py'),
                            '--contam-json', flag, '--skus', str(skus), '--apply'] + ARGS,
                           input=reg.read_text(), capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        assert json.loads(skus.read_text())['skus']['sony-fe-35mm-f1-4-gm'][
            'contamination_key'] == 'sony-35-f14-gm'
