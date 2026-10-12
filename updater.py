"""One-click update from the workbench.

The web process runs as an unprivileged user, so it cannot update anything itself. All it does is drop an
empty-ish request file into the data directory. A root systemd path unit notices the file and runs
/opt/vps-watch/update.sh, which downloads the project from the configured repository, verifies it, installs it,
and rolls back if the new version does not come up. Progress is reported back through update-status.json.
Only install.sh turns this on (UPDATE_APPLY=1); Docker installs are updated by rebuilding the image.
"""
import json
import os
import time
from store import DATA, db
import updates

REQUEST = DATA / 'update-request'
STATUS = DATA / 'update-status.json'
STALE = 20 * 60  # the root script has a 15 minute limit; anything older is considered dead


def available():
    return os.environ.get('UPDATE_APPLY') == '1' and updates.enabled()


def status(now=None):
    """What the root updater last reported, or None."""
    now = time.time() if now is None else now
    try:
        info = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        info = None
    if not isinstance(info, dict):
        info = None
    pending = REQUEST.exists()
    if info and info.get('state') == 'running' and now - float(info.get('time') or 0) > STALE:
        info = dict(info, state='failed', message='更新长时间没有响应，请到服务器查看 journalctl -u vps-watch-update')
    if pending and (not info or info.get('state') != 'running'):
        info = {'state': 'queued', 'message': '已提交，等待更新程序启动', 'time': now}
    return info


def busy():
    info = status()
    return bool(info and info.get('state') in ('queued', 'running'))


def purchase_running():
    with db() as c:
        return bool(c.execute("SELECT 1 FROM checkout_preflights WHERE state IN ('queued','running') LIMIT 1").fetchone())


def request():
    if not available():
        raise ValueError('这台服务器不支持网页更新，请按部署文档手动更新')
    latest = updates.status()
    if not latest:
        raise ValueError('已经是最新版本')
    if busy():
        raise ValueError('更新正在进行中')
    if purchase_running():
        raise ValueError('有自动下单任务正在执行，请稍后再更新')
    tmp = DATA / 'update-request.tmp'
    tmp.write_text(json.dumps({'requested': time.time(), 'version': latest['version']}))
    os.replace(tmp, REQUEST)  # the path unit fires on the final name only
    return latest
