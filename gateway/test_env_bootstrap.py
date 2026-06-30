"""Tests for env_bootstrap.py — the shared gateway/.env loader.

The reason this module exists (and is tested for the first time): the cron entry
point resolve_pass imports ebay_api directly, ebay_api reads EBAY_APP_ID /
EBAY_CERT_ID at module-level, and a plain crontab invocation inherits a minimal
env — so without an explicit .env load NOTHING populated os.environ and every
resolve failed "not configured". What these lock down:

  - The cron-simulation case: a clean env + a temp .env => the keys land in
    os.environ (the exact failure mode that was breaking the cron path).
  - Precedence: a key ALREADY in the environment is NOT overridden by the .env
    (a real shell export / systemd EnvironmentFile still wins — .env is fallback).
  - Parsing discipline: comments, blanks, and lines without '=' are skipped;
    surrounding single/double quotes on the value are stripped.
  - Absent file => silent no-op (the sandbox/test default), returns [].
  - A malformed/unreadable file degrades to "creds absent", never raises (the
    consumer's is_configured() handles the absence loudly).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import env_bootstrap  # noqa: E402


@pytest.fixture
def clean_env(monkeypatch):
    """Simulate the bare crontab environment: the EBAY_* / test keys absent."""
    for k in ('EBAY_APP_ID', 'EBAY_CERT_ID', 'EB_TEST_A', 'EB_TEST_B',
              'EB_QUOTED', 'EB_PRECEDENCE'):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _write_env(tmp_path, body):
    p = tmp_path / '.env'
    p.write_text(body)
    return str(p)


# ── the cron-simulation case: the bug this module fixes ─────────────────────

def test_clean_env_plus_dotenv_populates_creds(clean_env, tmp_path):
    """The exact cron failure mode, now passing: bare env + a .env => the eBay
    creds reach os.environ, so a subsequently-imported ebay_api is configured."""
    import os
    env_path = _write_env(tmp_path,
                          'EBAY_APP_ID=app-123\nEBAY_CERT_ID=cert-456\n')
    set_keys = env_bootstrap.load_dotenv(env_path)
    assert os.environ['EBAY_APP_ID'] == 'app-123'
    assert os.environ['EBAY_CERT_ID'] == 'cert-456'
    assert set(set_keys) == {'EBAY_APP_ID', 'EBAY_CERT_ID'}


# ── precedence: a real export / EnvironmentFile still wins ──────────────────

def test_existing_env_is_not_overridden(clean_env, tmp_path):
    import os
    os.environ['EB_PRECEDENCE'] = 'from-shell'
    env_path = _write_env(tmp_path, 'EB_PRECEDENCE=from-dotenv\n')
    set_keys = env_bootstrap.load_dotenv(env_path)
    # shell/EnvironmentFile value survives; .env did not override it
    assert os.environ['EB_PRECEDENCE'] == 'from-shell'
    assert 'EB_PRECEDENCE' not in set_keys


# ── parsing discipline ──────────────────────────────────────────────────────

def test_skips_comments_blanks_and_non_assignments(clean_env, tmp_path):
    import os
    env_path = _write_env(tmp_path,
        '# a comment\n'
        '\n'
        'this line has no equals sign\n'
        'EB_TEST_A=alpha\n'
        '   # indented comment\n'
        'EB_TEST_B=beta\n')
    set_keys = env_bootstrap.load_dotenv(env_path)
    assert os.environ['EB_TEST_A'] == 'alpha'
    assert os.environ['EB_TEST_B'] == 'beta'
    assert set(set_keys) == {'EB_TEST_A', 'EB_TEST_B'}


def test_strips_surrounding_quotes(clean_env, tmp_path):
    import os
    env_path = _write_env(tmp_path,
        'EB_QUOTED="double-quoted"\nEB_TEST_A=\'single-quoted\'\n')
    env_bootstrap.load_dotenv(env_path)
    assert os.environ['EB_QUOTED'] == 'double-quoted'
    assert os.environ['EB_TEST_A'] == 'single-quoted'


# ── absent / unreadable file => safe no-op ─────────────────────────────────

def test_absent_file_is_silent_noop(clean_env, tmp_path):
    missing = str(tmp_path / 'nope.env')
    assert env_bootstrap.load_dotenv(missing) == []


def test_default_path_is_gateway_dotenv():
    """The default path resolves to gateway/.env beside this module — so any
    caller in gateway/ (app_production, resolve_pass) loads the same file."""
    assert env_bootstrap._DEFAULT_ENV_PATH.endswith('gateway/.env') or \
           env_bootstrap._DEFAULT_ENV_PATH.endswith('.env')
    # the dir is this file's dir
    assert env_bootstrap._GATEWAY_DIR == str(Path(__file__).resolve().parent)


def test_unreadable_file_degrades_not_raises(clean_env, tmp_path, monkeypatch):
    # Point at a directory (open() will fail) — must warn-and-return, not raise.
    set_keys = env_bootstrap.load_dotenv(str(tmp_path))
    assert set_keys == []
