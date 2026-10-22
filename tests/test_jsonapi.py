"""v0.6.0: stock from a public JSON endpoint (shops whose page is rendered by JavaScript)."""
import json
import unittest
from decimal import Decimal
from unittest.mock import patch
import test_app  # noqa: F401
from test_v04 import AppCase
from merchants import jsonapi
from network import Page, FetchError
import worker
from store import db

# Shape of the real https://api.estk.me/user/shop/products/1 response (two products, both sold out at the time).
API = {'code': 0, 'data': {'product': {'id': 1, 'title': 'ESTKme P-series', 'items': [
    {'id': 3, 'title': 'ESTKme Max', 'price': '200.00', 'stock': 0, 'status': 1},
    {'id': 13, 'title': 'ESTKme Plus+', 'price': '160.00', 'stock': 0, 'status': 1}]}}}
CFG = jsonapi.PRESETS['jsonapi']


def with_stock(n, item=1):
    data = json.loads(json.dumps(API))
    data['data']['product']['items'][item]['stock'] = n
    return data


class ClassifyTests(unittest.TestCase):
    def test_sold_out_and_in_stock(self):
        self.assertEqual(jsonapi.classify(API, CFG)[:2], ('out_of_stock', Decimal('160.00')))
        status, price, detail = jsonapi.classify(with_stock(5), CFG)
        self.assertEqual((status, price), ('available', Decimal('160.00')))
        self.assertIn('Plus+', detail)

    def test_only_the_selected_product_counts(self):
        self.assertEqual(jsonapi.classify(with_stock(9, item=0), CFG)[0], 'out_of_stock')  # Max restocked, Plus+ not

    def test_stock_as_string_and_missing_pieces_are_errors_not_stock(self):
        data = with_stock('3')
        self.assertEqual(jsonapi.classify(data, CFG)[0], 'available')
        for bad in (CFG | {'match_value': '99'}, CFG | {'list_path': 'data.nope'}, CFG | {'stock_field': 'qty'},
                    CFG | {'list_path': 'data.product.title'}):
            with self.assertRaises(ValueError, msg=bad):
                jsonapi.classify(API, bad)
        broken = with_stock(None)
        with self.assertRaises(ValueError):
            jsonapi.classify(broken, CFG)

    def test_duplicate_match_is_ambiguous(self):
        data = json.loads(json.dumps(API))
        data['data']['product']['items'].append({'id': 13, 'stock': 1})
        with self.assertRaises(ValueError):
            jsonapi.classify(data, CFG)


class CheckTests(unittest.TestCase):
    def check(self, body):
        with patch('merchants.jsonapi.fetch', return_value=Page(CFG['url'], 200, html=body)):
            return jsonapi.check(CFG)

    def test_reads_the_endpoint(self):
        self.assertEqual(self.check(json.dumps(API))[0], 'out_of_stock')

    def test_not_json_and_api_errors(self):
        with self.assertRaises(FetchError):
            self.check('<html>blocked</html>')
        with self.assertRaises(ValueError):
            self.check(json.dumps({'code': 401, 'message': 'login required'}))

    def test_asks_for_json(self):
        with patch('merchants.jsonapi.fetch', return_value=Page(CFG['url'], 200, html=json.dumps(API))) as f:
            jsonapi.check(CFG)
        self.assertEqual(f.call_args.kwargs.get('kind'), 'json')


class TaskTests(AppCase):
    def test_create_check_and_notification_link(self):
        self.login()
        r = self.req('/api/tasks', data=CFG)
        self.assertEqual(r.status_code, 201, r.json)
        presets = self.client.get('/api/dashboard').json['presets']
        self.assertIn('jsonapi', presets)
        task = self.client.get('/api/dashboard').json['tasks'][0]
        self.assertEqual(task['config']['page_url'], 'https://store.estk.me/products/1')
        self.assertEqual(jsonapi.order_url(task['config']), 'https://store.estk.me/products/1')
        self.req(f"/api/tasks/{task['id']}/start")
        with db() as c:
            c.execute('UPDATE tasks SET next_check=0')
        sent = []
        with patch('merchants.jsonapi.fetch', return_value=Page(CFG['url'], 200, html=json.dumps(with_stock(2)))), \
                patch('worker.notifications.send', side_effect=lambda *a, **k: sent.append((a, k)) or
                      {'sent': ['telegram'], 'failed': {}, 'complete': True}):
            worker.perform(*worker.claim())
        self.assertTrue(sent)
        self.assertIn('https://store.estk.me/products/1', json.dumps(sent, default=str))
        self.assertEqual(self.client.get('/api/dashboard').json['tasks'][0]['status'], 'available')

    def test_validation(self):
        self.login()
        for bad in (CFG | {'list_path': ''}, CFG | {'match_value': ''}, CFG | {'page_url': 'http://insecure.example.com'},
                    CFG | {'url': 'http://api.estk.me/x'}):
            self.assertEqual(self.req('/api/tasks', data=bad).status_code, 400, bad)
        slim = self.req('/api/tasks', data={k: v for k, v in CFG.items() if k not in ('page_url', 'price_field')})
        self.assertEqual(slim.status_code, 201)


if __name__ == '__main__':
    unittest.main()
