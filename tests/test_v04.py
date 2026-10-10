"""v0.4: restock history, task export/import, update notice, WHMCS domain step."""
import json
import time
import unittest
from unittest.mock import patch
import test_app
from test_app import CFG, FIXTURE
from store import db, set_setting
import adapters
import stock
import updates
import worker
from merchants import whmcs
from network import Page

SOLD = FIXTURE.replace('In stock', 'Sold out')
SENT = {'sent': ['telegram'], 'failed': {}, 'complete': True}


class AppCase(unittest.TestCase):
    """Reuse the app test helpers without re-running test_app's own tests."""
    setUp = test_app.Tests.setUp
    req = test_app.Tests.req
    login = test_app.Tests.login
    create = test_app.Tests.create


class StockHistoryTests(unittest.TestCase):
    def setUp(self):
        with db() as c:
            c.execute('DELETE FROM stock_events')
            c.execute('DELETE FROM tasks')
            self.id = c.execute("INSERT INTO tasks(config,created) VALUES ('{}',0)").lastrowid

    def test_transitions_ignore_errors_and_first_state(self):
        t = 1_000_000
        self.assertIsNone(stock.record(self.id, 'out_of_stock', now=t))  # starting state
        self.assertIsNone(stock.record(self.id, 'out_of_stock', now=t + 60))  # no change
        self.assertIsNone(stock.record(self.id, 'error', now=t + 120))  # not a stock state
        self.assertIsNone(stock.record(self.id, 'blocked', now=t + 130))
        self.assertEqual(stock.record(self.id, 'available', '9.99', now=t + 180), 'restock')
        self.assertIsNone(stock.record(self.id, 'error', now=t + 200))
        self.assertEqual(stock.record(self.id, 'price_exceeded', now=t + 300), 'soldout')
        self.assertEqual(stock.record(self.id, 'available', now=t + 1000), 'restock')
        periods = stock.restocks(self.id, now=t + 1100)
        self.assertEqual([(p['start'], p['end'], p['seconds']) for p in periods],
                         [(t + 1000, None, 100), (t + 180, t + 300, 120)])
        self.assertEqual(periods[1]['price'], '9.99')
        s = stock.summary(self.id, now=t + 1100)
        self.assertEqual((s['restocks_total'], s['restocks_30d'], s['in_stock_now'], s['median_seconds'], s['tracked_since']),
                         (2, 2, True, 120, t))
        self.assertEqual(stock.alert_line(self.id, now=t + 1100), '近 30 天第 2 次补货；以往通常 2 分钟 内售罄')

    def test_task_that_starts_in_stock_is_not_a_restock(self):
        self.assertIsNone(stock.record(self.id, 'available', now=1))
        self.assertEqual(stock.restocks(self.id), [])
        self.assertEqual(stock.alert_line(self.id), '')
        self.assertEqual(stock.record(self.id, 'out_of_stock', now=2), 'soldout')

    def test_old_restocks_drop_out_of_30_days(self):
        stock.record(self.id, 'out_of_stock', now=0)
        stock.record(self.id, 'available', now=10)
        stock.record(self.id, 'out_of_stock', now=20)
        s = stock.summary(self.id, now=40 * 86400)
        self.assertEqual((s['restocks_total'], s['restocks_30d']), (1, 0))

    def test_history_is_trimmed_and_deleted_with_task(self):
        for i in range(stock.KEEP + 20):
            stock.record(self.id, 'available' if i % 2 else 'out_of_stock', now=i)
        self.assertEqual(len(stock.events(self.id, 1000)), stock.KEEP)
        with db() as c:
            c.execute('DELETE FROM tasks WHERE id=?', (self.id,))
        self.assertEqual(stock.events(self.id), [])

    def test_duration_text(self):
        self.assertEqual([stock.duration(s) for s in (5, 180, 3600, 3900, 3 * 86400)],
                         ['5 秒', '3 分钟', '1 小时', '1 小时 5 分', '3 天'])


class WorkerHistoryTests(AppCase):
    def test_restock_is_recorded_and_mentioned_in_alert(self):
        id = self.create()
        self.req(f'/api/tasks/{id}/start')

        def run(html):
            with db() as c:
                c.execute('UPDATE tasks SET next_check=0 WHERE id=?', (id,))
                c.execute('DELETE FROM hosts')
            with patch('worker.get_html', return_value=html), patch('worker.notifications.send', return_value=SENT) as send:
                worker.perform(*worker.claim())
            return send

        run(SOLD)
        send = run(FIXTURE)
        self.assertEqual(send.call_count, 1)
        self.assertIn('近 30 天第 1 次补货', send.call_args[0][0])
        task = self.client.get('/api/dashboard').json['tasks'][0]
        self.assertEqual((task['stock']['restocks_total'], task['stock']['in_stock_now']), (1, True))
        history = self.client.get(f'/api/tasks/{id}/stock-history').json
        self.assertEqual(len(history['restocks']), 1)
        self.assertEqual(history['restocks'][0]['price'], '49.00')


class ExportImportTests(AppCase):
    def test_round_trip_skips_duplicates_and_starts_paused(self):
        id = self.create()
        self.req('/api/tasks', data=adapters.PRESETS['bero'] | {'name': 'BERO M'})
        self.req('/api/tasks', data=whmcs.PRESETS['whmcs'] | {'name': 'Host', 'url': 'https://my.example.com/cart.php?a=add&pid=7'})
        self.req(f'/api/tasks/{id}/start')
        exported = self.client.get('/api/tasks/export').json
        self.assertEqual((exported['format'], len(exported['tasks'])), ('vps-watch-tasks', 3))
        text = json.dumps(exported)
        for secret in ('password', 'token', 'policy', 'max_total'):
            self.assertNotIn(secret, text)
        r = self.req('/api/tasks/import', data=exported)
        self.assertEqual(r.json, {'imported': 0, 'skipped': 3})
        with db() as c:
            c.execute('DELETE FROM tasks')
        r = self.req('/api/tasks/import', data=exported)
        self.assertEqual(r.json, {'imported': 3, 'skipped': 0})
        tasks = self.client.get('/api/dashboard').json['tasks']
        self.assertEqual(sorted(t['config']['name'] for t in tasks), ['BERO M', 'Host', 'Test'])
        self.assertTrue(all(t['enabled'] == 0 for t in tasks))
        self.assertEqual(next(t for t in tasks if t['config']['adapter'] == 'whmcs')['config']['pid'], '7')

    def test_invalid_files_import_nothing(self):
        self.login()
        self.assertEqual(self.req('/api/tasks/import', data={'tasks': [CFG]}).status_code, 400)
        self.assertEqual(self.req('/api/tasks/import', data={'format': 'vps-watch-tasks', 'tasks': []}).status_code, 400)
        r = self.req('/api/tasks/import', data={'format': 'vps-watch-tasks', 'tasks': [CFG, CFG | {'url': 'http://x'}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn('第 2 个任务无效', r.json['error'])
        self.assertEqual(self.client.get('/api/dashboard').json['tasks'], [])

    def test_requires_login(self):
        self.assertEqual(self.client.get('/api/tasks/export').status_code, 401)


class UpdateTests(AppCase):
    def test_version_compare(self):
        self.assertTrue(updates.newer('0.10.0', '0.9.9'))
        self.assertTrue(updates.newer('v1.0.0', '0.4.0'))
        self.assertFalse(updates.newer('0.4.0', '0.4.0'))
        self.assertFalse(updates.newer('garbage', '0.4.0'))

    def test_daily_check_and_dashboard_notice(self):
        self.login()
        calls = []

        def fake():
            calls.append(1)
            return '99.0.0', 'https://github.com/prettylove520/vps-monitor/releases/tag/v99.0.0'

        updates.maybe_check(now=10**6, fetch=fake)
        updates.maybe_check(now=10**6 + 3600, fetch=fake)  # within a day: no second request
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.client.get('/api/dashboard').json['update']['version'], '99.0.0')
        set_setting('update_latest', json.dumps({'version': '0.0.1', 'url': 'https://github.com/x'}))
        self.assertIsNone(self.client.get('/api/dashboard').json['update'])

    def test_failure_is_silent_and_can_be_disabled(self):
        updates.maybe_check(now=time.time(), fetch=lambda: (_ for _ in ()).throw(OSError()))
        self.assertIsNone(updates.status())
        set_setting('update_latest', json.dumps({'version': '99.0.0', 'url': 'https://github.com/x'}))
        with patch.dict('os.environ', {'UPDATE_CHECK': '0'}):
            self.assertIsNone(updates.status())


class WhmcsDomainStepTests(unittest.TestCase):
    def test_domain_step_means_in_stock(self):
        page = Page('https://h/cart.php?a=add&pid=1', 200, '<form id="frmProductDomain" method="post"></form>')
        status, detail = whmcs.classify(page)
        self.assertEqual(status, 'available')
        self.assertIn('域名', detail)
        sold = Page('u', 200, '<form id="frmProductDomain"></form><p>Out of Stock</p>')
        with self.assertRaises(ValueError):
            whmcs.classify(sold)


if __name__ == '__main__':
    unittest.main()
