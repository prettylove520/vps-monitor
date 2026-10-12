"""Once a day, look up the newest version on GitHub so the workbench can show "new version available".

Only the public version file of the project is requested; nothing about this installation is sent.
Turn it off with UPDATE_CHECK=0, or point UPDATE_REPO at a fork.
"""
import json
import os
import re
import time
import urllib.request
from store import setting, set_setting
from version import VERSION

REPO = os.environ.get('UPDATE_REPO', 'prettylove520/vps-monitor')
INTERVAL = 86400


def enabled():
    return os.environ.get('UPDATE_CHECK', '1') != '0' and bool(re.fullmatch(r'[\w.-]+/[\w.-]+', REPO))


def parse(version):
    m = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', (version or '').strip())
    return tuple(map(int, m.groups())) if m else None


def newer(latest, current=VERSION):
    a, b = parse(latest), parse(current)
    return bool(a and b and a > b)


def fetch_latest():
    """Version number of the main branch. main only receives finished, tested versions, and this
    does not need a GitHub Release to exist."""
    request = urllib.request.Request('https://raw.githubusercontent.com/%s/main/version.py' % REPO, headers={
        'User-Agent': 'VPS-Monitor/' + VERSION})
    with urllib.request.urlopen(request, timeout=10) as response:
        text = response.read(4000).decode('utf-8', 'replace')
    match = re.search(r"VERSION\s*=\s*['\"](\d+\.\d+\.\d+)['\"]", text)
    if not match:
        raise ValueError('unexpected version file')
    return match.group(1), 'https://github.com/%s/blob/main/CHANGELOG.md' % REPO


def check_now(fetch=None, now=None):
    """Manual "check for updates" (at most once a minute). Returns 'ok', 'throttled', 'failed' or 'disabled'."""
    if not enabled():
        return 'disabled'
    now = time.time() if now is None else now
    if now - float(setting('update_checked', '0')) < 60:
        return 'throttled'
    set_setting('update_checked', str(now))
    try:
        version, url = (fetch or fetch_latest)()
    except Exception:
        return 'failed'
    set_setting('update_latest', json.dumps({'version': version, 'url': url}))
    return 'ok'


def maybe_check(now=None, fetch=fetch_latest):
    """Called from the worker loop; does nothing until a day has passed since the last attempt."""
    if not enabled():
        return
    now = time.time() if now is None else now
    if now - float(setting('update_checked', '0')) < INTERVAL:
        return
    set_setting('update_checked', str(now))
    try:
        version, url = fetch()
    except Exception:
        return  # offline, rate limited or no release yet: try again tomorrow
    set_setting('update_latest', json.dumps({'version': version, 'url': url}))


def status():
    try:
        latest = json.loads(setting('update_latest', '') or 'null')
    except ValueError:
        latest = None
    if not enabled() or not latest or not newer(latest.get('version')):
        return None
    return latest
