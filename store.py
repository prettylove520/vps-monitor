import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DATA = Path(os.environ.get('WATCH_DATA', Path(__file__).parent / 'data'))
DATA.mkdir(parents=True, exist_ok=True, mode=0o700)

def secret(name):
    path = DATA / name
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    value = secrets.token_hex(32)
    with os.fdopen(fd, 'w') as f:
        f.write(value)
    return value

@contextmanager
def db():
    conn = sqlite3.connect(DATA / 'watch.db', timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init():
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks (
          id INTEGER PRIMARY KEY, config TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'pending', result TEXT, last_check REAL,
          next_check REAL NOT NULL DEFAULT 0, queued INTEGER NOT NULL DEFAULT 0,
          lease REAL NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
          failures INTEGER NOT NULL DEFAULT 0, notified INTEGER NOT NULL DEFAULT 0,
          created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(next_check,lease);
        CREATE TABLE IF NOT EXISTS logs (
          id INTEGER PRIMARY KEY, task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
          created REAL NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_logs_task ON logs(task_id,id);
        CREATE TABLE IF NOT EXISTS hosts (host TEXT PRIMARY KEY, next_check REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts (ip TEXT PRIMARY KEY, count INTEGER, until REAL);
        CREATE TABLE IF NOT EXISTS task_notifications (
          task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
          revision INTEGER NOT NULL, channels TEXT NOT NULL);
        ''')

def setting(key, default=''):
    with db() as c:
        r = c.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return r['value'] if r else default

def set_setting(key, value):
    with db() as c:
        c.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

def log(task_id, level, message):
    with db() as c:
        c.execute('INSERT INTO logs(task_id,created,level,message) VALUES (?,?,?,?)', (task_id, time.time(), level, message[:1000]))
        c.execute('DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 5000)')

def task_dict(row):
    return dict(row) | {'config': json.loads(row['config']), 'result': json.loads(row['result']) if row['result'] else None}

init()
