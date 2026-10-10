"""Restock history: record when a task switches between in stock and sold out.

Only real stock states count. Errors, blocked pages and unchecked tasks keep the last known state,
so a temporary 503 during a sold-out period does not create a fake restock.
"""
import time
from store import db

IN_STOCK = 'available'
SOLD = ('out_of_stock', 'price_exceeded')
KEEP = 200  # events kept per task
DAY = 86400


def initialize():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS stock_events (
          id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          created REAL NOT NULL, state TEXT NOT NULL, price TEXT);
        CREATE INDEX IF NOT EXISTS idx_stock_task ON stock_events(task_id,id);
        ''')


initialize()


def _state(status):
    if status == IN_STOCK:
        return 'in'
    if status in SOLD:
        return 'out'
    return None


def record(task_id, status, price=None, now=None):
    """Store a transition. Returns 'restock', 'soldout' or None (no change / not a stock state)."""
    state = _state(status)
    if not state:
        return None
    now = time.time() if now is None else now
    with db() as c:
        last = c.execute('SELECT state FROM stock_events WHERE task_id=? ORDER BY id DESC LIMIT 1', (task_id,)).fetchone()
        if last and last['state'] == state:
            return None
        c.execute('INSERT INTO stock_events(task_id,created,state,price) VALUES (?,?,?,?)',
                  (task_id, now, state, None if price is None else str(price)))
        c.execute('DELETE FROM stock_events WHERE task_id=? AND id NOT IN '
                  '(SELECT id FROM stock_events WHERE task_id=? ORDER BY id DESC LIMIT ?)', (task_id, task_id, KEEP))
    if state == 'in':
        # The very first observation of a task is not a restock, just its starting state.
        return 'restock' if last else None
    return 'soldout' if last else None


def events(task_id, limit=KEEP):
    with db() as c:
        rows = c.execute('SELECT created,state,price FROM stock_events WHERE task_id=? ORDER BY id DESC LIMIT ?',
                         (task_id, limit)).fetchall()
    return [dict(r) for r in rows]


def restocks(task_id, now=None):
    """Restock periods, newest first: {start, end (None while still in stock), seconds, price}."""
    now = time.time() if now is None else now
    rows = list(reversed(events(task_id)))
    periods = []
    for i, row in enumerate(rows):
        if row['state'] != 'in' or i == 0:
            continue  # the first event is the starting state, not a restock
        end = next((r['created'] for r in rows[i + 1:] if r['state'] == 'out'), None)
        periods.append({'start': row['created'], 'end': end, 'seconds': round((end or now) - row['created']),
                        'price': row['price']})
    return list(reversed(periods))


def summary(task_id, now=None):
    now = time.time() if now is None else now
    periods = restocks(task_id, now)
    recent = [p for p in periods if p['start'] >= now - 30 * DAY]
    finished = [p['seconds'] for p in periods if p['end']]
    return {
        'last_restock': periods[0]['start'] if periods else None,
        'in_stock_now': bool(periods and periods[0]['end'] is None),
        'restocks_30d': len(recent),
        'restocks_total': len(periods),
        'median_seconds': sorted(finished)[len(finished) // 2] if finished else None,
        'tracked_since': (events(task_id, KEEP) or [{}])[-1].get('created'),
    }


def duration(seconds):
    """Short Chinese duration, e.g. 45 秒, 3 分钟, 2 小时 5 分, 3 天."""
    seconds = int(seconds or 0)
    if seconds < 60:
        return '%d 秒' % seconds
    if seconds < 3600:
        return '%d 分钟' % (seconds // 60)
    if seconds < DAY:
        h, m = divmod(seconds // 60, 60)
        return '%d 小时' % h + (' %d 分' % m if m else '')
    return '%d 天' % (seconds // DAY)


def alert_line(task_id, now=None):
    """One line for the restock notification, or '' when there is no history yet."""
    s = summary(task_id, now)
    if not s['restocks_total']:
        return ''
    parts = ['近 30 天第 %d 次补货' % s['restocks_30d']]
    if s['median_seconds']:
        parts.append('以往通常 %s 内售罄' % duration(s['median_seconds']))
    return '；'.join(parts)
