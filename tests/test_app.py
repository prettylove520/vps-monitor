import os
import sys
import tempfile
import unittest
import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

TEMP=tempfile.TemporaryDirectory()
os.environ['WATCH_DATA']=TEMP.name
os.environ['COOKIE_SECURE']='0'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import app
from store import db,set_setting,setting
from werkzeug.security import generate_password_hash
import adapters,network,worker,notifications
import purchases

FIXTURE='''<div id="plan"><h2>Annual VPS 365 days</h2><b class="price">49.00 EUR</b><span>In stock</span></div>'''
CFG=adapters.PRESETS['generic']|dict(name='Test',url='https://example.com/product',max_price='50.00',scope='#plan',identity='Annual VPS 365 days',price_selector='.price',in_stock='In stock',out_stock='Sold out')

class Tests(unittest.TestCase):
    def setUp(self):
        with db() as c:
            c.execute('DELETE FROM merchant_accounts')
            c.execute('DELETE FROM purchase_attempts');c.execute('DELETE FROM purchase_policies')
            c.execute('DELETE FROM logs');c.execute('DELETE FROM tasks');c.execute('DELETE FROM hosts');c.execute('DELETE FROM attempts');c.execute('DELETE FROM settings')
        set_setting('admin_password',generate_password_hash('test-password-123'))
        self.client=app.test_client()
        self.csrf=self.client.get('/api/session').json['csrf']
    def req(self,url,method='POST',data=None):
        return self.client.open(url,method=method,json=data or {},headers={'X-CSRF-Token':self.csrf})
    def login(self):
        r=self.req('/api/login',data={'password':'test-password-123'})
        self.assertEqual(r.status_code,200)
        self.csrf=r.json['csrf']
    def create(self):
        self.login()
        r=self.req('/api/tasks',data=CFG)
        self.assertEqual(r.status_code,201)
        return r.json['id']
    def test_auth_and_csrf(self):
        self.assertEqual(self.client.get('/healthz').json['application'],'vps-watch')
        self.assertEqual(self.client.get('/api/dashboard').status_code,401)
        self.assertEqual(self.client.post('/api/login',json={'password':'test-password-123'}).status_code,403)
        self.login();self.assertEqual(self.client.get('/api/dashboard').status_code,200)
    def test_login_limit(self):
        for _ in range(5):self.req('/api/login',data={'password':'wrong'})
        self.assertEqual(self.req('/api/login',data={'password':'test-password-123'}).status_code,429)
    def test_crud(self):
        id=self.create()
        self.assertEqual(self.client.get('/api/dashboard').json['tasks'][0]['enabled'],0)
        self.req(f'/api/tasks/{id}/start')
        self.assertEqual(self.client.get('/api/dashboard').json['tasks'][0]['enabled'],1)
        self.req(f'/api/tasks/{id}',method='PUT',data=CFG|{'name':'Edited'})
        r=self.client.get('/api/dashboard').json['tasks'][0]
        self.assertEqual((r['config']['name'],r['enabled']),('Edited',0))
        self.req(f'/api/tasks/{id}',method='DELETE')
        self.assertEqual(self.client.get('/api/dashboard').json['tasks'],[])
    def test_public_address_checks(self):
        for url in ['http://example.com','https://127.0.0.1/','https://10.0.0.1/','https://[::1]/','https://a:b@example.com/','https://example.com:8080/']:
            with self.assertRaises(network.FetchError):network.validate_url(url)
        with patch('network.socket.getaddrinfo',return_value=[(2,1,6,'',('10.0.0.1',443))]):
            with self.assertRaises(network.FetchError):network.public_ips('example.com')
    def test_generic_parser(self):
        self.assertEqual(adapters.inspect(FIXTURE,CFG)['status'],'available')
        self.assertEqual(adapters.inspect(FIXTURE.replace('In stock','Sold out In stock'),CFG)['status'],'out_of_stock')
        self.assertEqual(adapters.inspect(FIXTURE.replace('49.00','59.00'),CFG)['status'],'price_exceeded')
        for bad in [FIXTURE.replace('Annual VPS','Other VPS'),FIXTURE.replace('EUR','CAD'),FIXTURE+FIXTURE,FIXTURE.replace('In stock','Wait')]:
            with self.assertRaises(ValueError):adapters.inspect(bad,CFG)
    def test_both_merchant_fixtures(self):
        base=Path(__file__).parent/'fixtures'
        self.assertEqual(adapters.inspect((base/'bero.html').read_text(encoding='utf-8'),adapters.PRESETS['bero'])['status'],'out_of_stock')
        self.assertEqual(adapters.inspect((base/'vmiss.html').read_text(encoding='utf-8'),adapters.PRESETS['vmiss'])['status'],'out_of_stock')
    def test_validation(self):
        for cfg in [CFG|{'interval':1},CFG|{'max_price':'NaN'},CFG|{'scope':'['},CFG|{'in_stock':''}]:
            with self.assertRaises(ValueError):adapters.validate(cfg)
    def test_notification_secrets(self):
        self.login()
        token='123456789:'+'a'*30
        r=self.req('/api/settings',method='PUT',data={'token':token,'chat_id':'123456'})
        self.assertEqual(r.status_code,200)
        self.assertNotIn(token,setting('telegram_token'))
        self.assertNotIn(token,self.client.get('/api/settings').text)
        self.assertEqual(notifications.credentials(),(token,'123456'))
    def test_worker_dedup_and_pause_on_block(self):
        id=self.create();self.req(f'/api/tasks/{id}/start')
        job=worker.claim();self.assertIsNotNone(job)
        with patch('worker.get_html',return_value=FIXTURE),patch('worker.notifications.send',return_value={'sent':['telegram'],'failed':{},'complete':True}) as send:
            worker.perform(*job);self.assertEqual(send.call_count,1)
        with db() as c:
            c.execute('UPDATE tasks SET next_check=0 WHERE id=?',(id,));c.execute('DELETE FROM hosts')
        with patch('worker.get_html',return_value=FIXTURE),patch('worker.notifications.send') as send:
            worker.perform(*worker.claim());self.assertEqual(send.call_count,0)
        with db() as c:
            c.execute('UPDATE tasks SET next_check=0 WHERE id=?',(id,));c.execute('DELETE FROM hosts')
        with patch('worker.get_html',side_effect=network.FetchError('403',blocked=True)),patch('worker.notifications.send',return_value={'sent':['telegram'],'failed':{},'complete':True}):
            worker.perform(*worker.claim())
        r=self.client.get('/api/dashboard').json['tasks'][0]
        self.assertEqual((r['enabled'],r['status']),(0,'blocked'))
    def test_pause_inflight_discards_result(self):
        id=self.create();self.req(f'/api/tasks/{id}/check');job=worker.claim()
        self.req(f'/api/tasks/{id}/pause')
        with patch('worker.get_html',return_value=FIXTURE),patch('worker.notifications.send') as send:
            worker.perform(*job);self.assertEqual(send.call_count,0)
    def test_host_cooldown(self):
        id=self.create();self.req('/api/tasks',data=CFG|{'name':'Another'})
        for t in self.client.get('/api/dashboard').json['tasks']:self.req(f"/api/tasks/{t['id']}/start")
        self.assertIsNotNone(worker.claim());self.assertIsNone(worker.claim())
    def test_retry_after(self):
        id=self.create();self.req(f'/api/tasks/{id}/start')
        with patch('worker.get_html',side_effect=network.FetchError('429',retry_after=7200)):
            worker.perform(*worker.claim())
        r=self.client.get('/api/dashboard').json['tasks'][0]
        self.assertGreater(r['next_check'],time.time()+7100)
        self.req(f'/api/tasks/{id}/check')
        self.assertIsNone(worker.claim())
    def test_failed_notify_retries(self):
        id=self.create();self.req(f'/api/tasks/{id}/start')
        with patch('worker.get_html',return_value=FIXTURE),patch('worker.notifications.send',side_effect=ValueError('delivery failed')):
            worker.perform(*worker.claim())
        r=self.client.get('/api/dashboard').json['tasks'][0]
        self.assertEqual(r['notified'],0)
        self.assertIn('delivery failed',r['result']['detail'])

    def configure_bark(self):
        notifications.save_bark({'url':'https://api.day.app/testDeviceKey12345','enabled':True,
                                 'level':'critical','volume':8,'sound':'alarm','repeat':True})

    def bark_connection(self, code=200, status=200):
        conn=MagicMock()
        conn.getresponse.return_value.status=status
        conn.getresponse.return_value.read.return_value=json.dumps({'code':code}).encode()
        return conn

    def test_bark_settings_private_and_preserved(self):
        self.login()
        key='testDeviceKey12345'
        r=self.req('/api/settings/bark',method='PUT',data={'url':'https://api.day.app/'+key,'enabled':True})
        self.assertEqual(r.status_code,200)
        self.assertNotIn(key,setting('bark_endpoint'))
        self.assertNotIn(key,self.client.get('/api/settings/bark').text)
        self.assertTrue(self.client.get('/api/dashboard').json['notifications_configured'])
        saved=setting('bark_endpoint')
        self.assertEqual(self.req('/api/settings/bark',method='PUT',data={'url':'','volume':10}).status_code,200)
        self.assertEqual(saved,setting('bark_endpoint'))
        self.assertEqual(notifications.bark_settings()['volume'],10)
        self.req('/api/settings/bark',method='PUT',data={'enabled':False})
        self.assertFalse(self.client.get('/api/dashboard').json['notifications_configured'])

    def test_bark_setting_validation(self):
        for url in ('http://api.day.app/testKey123','https://127.0.0.1/testKey123',
                    'https://api.day.app/testKey123/title/body','https://api.day.app/testKey123?level=critical',
                    'https://user:pass@api.day.app/testKey123','https://api.day.app:8443/testKey123',
                    'https://api.day.app/testKey123%2Ftitle'):
            with self.assertRaises(ValueError):notifications.save_bark({'url':url,'enabled':True})
        with self.assertRaises(ValueError):notifications.save_bark({'enabled':True})
        self.configure_bark()
        saved=setting('bark_options')
        for delta in ({'volume':11},{'volume':True},{'volume':1.5},{'level':'unknown'},
                      {'enabled':'true'},{'repeat':1},{'sound':'../x'},{'url':None}):
            with self.assertRaises(ValueError):notifications.save_bark(delta)
        self.assertEqual(saved,setting('bark_options'))

    def test_bark_separate_server_key_and_old_config(self):
        self.configure_bark()
        settings=notifications.bark_settings()
        self.assertEqual(settings['server_url'],'https://api.day.app')
        self.assertTrue(settings['key_saved'])
        notifications.save_bark({'server_url':'https://api.day.app/','device_key':''})
        self.assertTrue(notifications.bark_settings()['key_saved'])
        with self.assertRaises(ValueError):notifications.save_bark({'server_url':'https://other.example','device_key':''})
        with self.assertRaises(ValueError):notifications.save_bark({'server_url':'https://api.day.app/testDeviceKey12345','device_key':'testDeviceKey12345'})
        notifications.save_bark({'server_url':'https://push.example.com','device_key':'anotherDeviceKey12345'})
        self.assertEqual(notifications.bark_settings()['server_url'],'https://push.example.com')
        self.assertNotIn('anotherDeviceKey12345',json.dumps(notifications.bark_settings()))

    def test_bark_critical_payload_and_normal_error(self):
        self.configure_bark()
        conn=self.bark_connection()
        with patch('notifications.public_ips',return_value=['1.1.1.1']),patch('notifications.PinnedHTTPS',return_value=conn) as cls:
            notifications.send_bark('补货测试',url='https://bero-host.de/server/test')
        cls.assert_called_once_with('api.day.app','1.1.1.1')
        self.assertEqual(conn.request.call_args.args,('POST','/push'))
        payload=json.loads(conn.request.call_args.kwargs['body'])
        self.assertEqual((payload['device_key'],payload['level'],payload['volume'],payload['call']),
                         ('testDeviceKey12345','critical','8','1'))
        self.assertEqual(payload['url'],'https://bero-host.de/server/test')
        conn.close.assert_called_once()
        with patch('notifications.public_ips',return_value=['1.1.1.1']),patch('notifications.PinnedHTTPS',return_value=conn):
            notifications.send_bark('异常测试',urgent=False)
        payload=json.loads(conn.request.call_args.kwargs['body'])
        self.assertEqual(payload['level'],'active')
        self.assertNotIn('call',payload);self.assertNotIn('volume',payload)

    def test_bark_network_failures_are_redacted(self):
        self.configure_bark()
        with patch('network.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]),patch('notifications.PinnedHTTPS') as conn:
            with self.assertRaises(ValueError) as error:notifications.send_bark('test')
            conn.assert_not_called()
            self.assertNotIn('testDeviceKey12345',str(error.exception))
        for code,status in ((400,200),(200,302),(200,500)):
            conn=self.bark_connection(code,status)
            with patch('notifications.public_ips',return_value=['1.1.1.1']),patch('notifications.PinnedHTTPS',return_value=conn):
                with self.assertRaises(ValueError):notifications.send_bark('test')
            conn.close.assert_called_once()
        conn=self.bark_connection();conn.request.side_effect=OSError('secret testDeviceKey12345')
        with patch('notifications.public_ips',return_value=['1.1.1.1']),patch('notifications.PinnedHTTPS',return_value=conn):
            with self.assertRaises(ValueError) as error:notifications.send_bark('test')
            self.assertNotIn('testDeviceKey12345',str(error.exception))

    def test_bark_only_and_separate_test_routes(self):
        self.login();self.configure_bark()
        with patch('notifications.send_telegram') as tg,patch('notifications.send_bark') as bark:
            report=notifications.send('test')
            self.assertTrue(report['complete']);tg.assert_not_called();bark.assert_called_once()
        with patch('notifications.send_telegram') as tg,patch('notifications.send_bark') as bark:
            self.assertEqual(self.req('/api/settings/bark/test').status_code,200)
            tg.assert_not_called();bark.assert_called_once()
        with patch('notifications.send_telegram') as tg,patch('notifications.send_bark') as bark:
            self.assertEqual(self.req('/api/settings/test').status_code,200)
            tg.assert_called_once();bark.assert_not_called()

    def test_partial_delivery_retries_only_failed_channel(self):
        id=self.create();self.req(f'/api/tasks/{id}/start')
        self.configure_bark();notifications.save('123456789:'+'a'*30,'123456')
        def next_job():
            with db() as c:
                c.execute('UPDATE tasks SET next_check=0 WHERE id=?',(id,));c.execute('DELETE FROM hosts')
            return worker.claim()
        with patch('worker.get_html',return_value=FIXTURE),patch('notifications.send_telegram') as tg,patch('notifications.send_bark',side_effect=ValueError('failed')) as bark:
            worker.perform(*worker.claim());tg.assert_called_once();bark.assert_called_once()
        r=self.client.get('/api/dashboard').json['tasks'][0]
        self.assertEqual(r['notified'],0);self.assertIn('Bark 发送失败',r['result']['detail'])
        self.assertGreater(r['next_check'],time.time()+58)
        # Re-open the database to simulate another worker process after restart.
        with db() as c:self.assertEqual(json.loads(c.execute('SELECT channels FROM task_notifications').fetchone()[0]),['telegram'])
        self.req(f'/api/tasks/{id}/pause');self.req(f'/api/tasks/{id}/start')
        with patch('worker.get_html',return_value=FIXTURE),patch('notifications.send_telegram') as tg,patch('notifications.send_bark') as bark:
            worker.perform(*next_job());tg.assert_not_called();bark.assert_called_once()
        self.assertEqual(self.client.get('/api/dashboard').json['tasks'][0]['notified'],1)
        with patch('worker.get_html',return_value=FIXTURE),patch('notifications.send') as send:
            worker.perform(*next_job());send.assert_not_called()
        with patch('worker.get_html',return_value=FIXTURE.replace('In stock','Sold out')),patch('notifications.send') as send:
            worker.perform(*next_job());send.assert_not_called()
        with patch('worker.get_html',return_value=FIXTURE),patch('notifications.send_telegram') as tg,patch('notifications.send_bark') as bark:
            worker.perform(*next_job());tg.assert_called_once();bark.assert_called_once()

    def test_disabled_channel_and_failure_isolation(self):
        self.configure_bark();notifications.save('123456789:'+'a'*30,'123456')
        with patch('notifications.send_telegram',side_effect=RuntimeError('private url')) as tg,patch('notifications.send_bark') as bark:
            report=notifications.send('test')
            tg.assert_called_once();bark.assert_called_once()
        self.assertEqual(report['sent'],['bark']);self.assertFalse(report['complete'])
        self.assertNotIn('private url',json.dumps(report))
        notifications.save('','123456',enabled=False)
        with patch('notifications.send_telegram') as tg,patch('notifications.send_bark') as bark:
            report=notifications.send('test');tg.assert_not_called();bark.assert_called_once()
        self.assertTrue(report['complete'])

    def bero_policy(self):
        self.login()
        id=self.req('/api/tasks',data=adapters.PRESETS['bero']).json['id']
        r=self.req(f'/api/tasks/{id}/purchase-policy',method='PUT',data={'payment':'balance','max_total':'44.10'})
        self.assertEqual(r.status_code,200)
        return id,r.json['policy']

    def quote(self):
        return {'package_id':109,'quantity':1,'runtime_days':365,'currency':'EUR',
                'coupon':'DISCORD10','coupon_accepted':True,'final_total_verified':True,
                'total':'44.10','payment':'balance','sufficient_balance':True}

    def test_purchase_policy_saves_without_enabling(self):
        id,policy=self.bero_policy()
        self.assertFalse(policy['automatic_purchase_enabled'])
        self.assertEqual(policy['max_total'],'44.10')
        with self.assertRaises(ValueError):purchases.reserve_attempt(id,self.quote())
        with db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM purchase_attempts').fetchone()[0],0)

    def test_purchase_quote_guards(self):
        _,policy=self.bero_policy()
        purchases.validate_quote(policy,self.quote())
        for delta in ({'total':'44.11'},{'total':'NaN'},{'coupon_accepted':False},
                      {'quantity':2},{'runtime_days':30},{'sufficient_balance':False},
                      {'final_total_verified':False},{'currency':'USD'}):
            with self.assertRaises(ValueError):purchases.validate_quote(policy,self.quote()|delta)

    def test_uncertain_attempt_never_retries(self):
        id,policy=self.bero_policy()
        # Simulate an independently verified executor, without any merchant request.
        policy|={'automatic_purchase_enabled':True,'status':'armed'}
        with db() as c:c.execute('UPDATE purchase_policies SET policy=? WHERE task_id=?',(json.dumps(policy),id))
        with patch('purchases.VERIFIED_CHECKOUT_ADAPTERS',frozenset({'BERO'})):
            attempt=purchases.reserve_attempt(id,self.quote())
            purchases.record_outcome(attempt)
            with self.assertRaises(ValueError):purchases.reserve_attempt(id,self.quote())
        with db() as c:self.assertEqual(c.execute('SELECT state FROM purchase_attempts WHERE id=?',(attempt,)).fetchone()[0],'uncertain')

if __name__=='__main__':unittest.main()
