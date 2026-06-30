"""
env_bootstrap.py — the one shared gateway/.env loader.
================================================================================
Extracted from app_production._load_dotenv (2026-06-30, minting-wire item 3) so
the gateway service AND the cron entry points load the SAME secrets file the SAME
way. The duplication this replaces was a latent cron break:

  - The gateway (app_production) ran _load_dotenv() at import, so by the time it
    imported ebay_api (which reads EBAY_APP_ID / EBAY_CERT_ID from os.environ at
    MODULE-LEVEL), the vars were present.
  - resolve_pass (the emit->resolve cron entry point) imports ebay_api directly
    and never imported app_production — so under a plain crontab invocation (the
    box's actual scheduled-job mechanism; the auto-pull is `crontab -u askmaddi`,
    not a systemd timer) NOTHING populated os.environ, ebay_api read empty
    strings, and every resolve failed "not configured" before eBay was touched.

A crontab job inherits a minimal environment — no systemd EnvironmentFile, none
of the gateway's exports. So the cron entry point must load gateway/.env itself,
BEFORE it imports ebay_api. This module is that loader, shared so there is one
parsing rule, one precedence rule, one place to fix.

PRECEDENCE (unchanged from the original): only sets keys NOT already in the
environment, so a real systemd EnvironmentFile or a shell export still wins. The
.env is the fallback, never an override.

PATH: defaults to `<this dir>/.env` == gateway/.env. Every caller lives in the
gateway/ directory (app_production, resolve_pass), and the box auto-pulls the
repo to /opt/askmaddi-prod, so this resolves to /opt/askmaddi-prod/gateway/.env
— the real secrets file (EBAY_*, PROXY_*), readable by the `askmaddi` service
user who owns both the gateway service and the crontab jobs.
"""
import os


_GATEWAY_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_ENV_PATH = os.path.join(_GATEWAY_DIR, '.env')


def load_dotenv(env_path=None):
    """Parse a .env file into os.environ if present (fallback, never override).

    No external dependency (python-dotenv is not guaranteed on the VPS). Lines
    are `KEY=value`; blanks and `#` comments are skipped; surrounding single or
    double quotes on the value are stripped. Only sets keys NOT already in the
    environment, so a systemd EnvironmentFile or shell export takes precedence.
    Silent no-op if the file is absent (the common case in the sandbox / tests).

    Returns the list of keys it actually set (handy for tests and a one-line
    boot log), empty if the file was absent or every key was already set.
    """
    path = env_path or _DEFAULT_ENV_PATH
    if not os.path.exists(path):
        return []
    set_keys = []
    try:
        with open(path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
                    set_keys.append(key)
    except Exception as e:
        # Never fatal — a malformed/unreadable .env degrades to "creds absent",
        # which the consumers (ebay_api.is_configured) already handle loudly.
        print(f"[env_bootstrap] WARN: .env load failed: {type(e).__name__}")
    return set_keys
