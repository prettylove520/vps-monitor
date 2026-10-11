"""Once a day, look up the latest GitHub release so the workbench can show "new version available".

Only the public release tag is requested; nothing about this installation is sent.
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
    request = urllib.request.Request('https://api.github.com/repos/%s/releases/latest' % REPO, headers={
        'Accept': 'application/vnd.github+json', 'User-Agent': 'VPS-Monitor/' + VERSION})
    with urllib.request.urlopen(request, timeout=10) as response:
        body = json.loads(response.read(200_000))
    tag = body.get('tag_name', '')
    url = body.get('html_url', '')
    if not parse(tag) or not url.startswith('https://github.com/'):
        raise ValueError('unexpected release data')
    return tag.lstrip('v'), url


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
