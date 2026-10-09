import json
import html
import time
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch, MagicMock
import test_app as helpers
from test_accounts import COOKIE, LOGIN, AUTHENTICATED
from app import app
from store import db
import accounts
import adapters
import checkout
from merchants import bero
BERO_CFG=adapters.validate(adapters.PRESETS['bero'])
TARGET=bero.target(BERO_CFG)
import purchases
import worker

FIXTURES=Path(__file__).parent/'fixtures'
def fixture(name):
    return (FIXTURES/name).read_text(encoding='utf-8')

def snapshot(step=500,stock=False,package=109,agb=False):
    return json.dumps({'data':{'step':step,'package':package,'packageAvailable':stock,'agbConfirmation':agb},
                      'memo':{'id':'synthetic-id','name':'public.order.order'},'checksum':'synthetic-test-checksum'})

def initial():
    return '<meta name="csrf-token" content="synthetic-csrf"><script data-update-uri="/livewire-test/update"></script><div wire:name="public.order.order" wire:snapshot="'+html.escape(snapshot(package=108),quote=True)+'">'+fixture('bero.html')+'</div>'

def money_page(balance='44,10'):
    return fixture('bero-money.html').replace('44,10',balance)

class Merchant:
    """Replays the verified live flow: select -> config -> overview -> coupon -> CHECK(1400) -> agb -> submit."""
    def __init__(self,stock=False,coupon_html=None,balances=('44,10','0,00'),check_html=None,next_step=1400,final_effects=None):
        self.sent=[];self.gets=[];self.balances=list(balances)
        self.snapshots=[snapshot(package=108),snapshot(),snapshot(1000),snapshot(1100),snapshot(1100,stock),
                        snapshot(next_step,stock),snapshot(next_step,stock,agb=True),snapshot(1500,stock,agb=True)]
        coupon=coupon_html or fixture('bero-coupon109.html')
        if stock:
            coupon=coupon.replace('disabled=""','wire:click.prevent="jumpToStep(%d)"'%next_step)
        check=check_html or fixture('bero-check109.html')
        self.pages=[fixture('bero-select109.html'),fixture('bero-config109.html'),fixture('bero-overview109.html'),coupon,
                    check+'<input name="email" value="private@example.com"><input name="_token" type="hidden" value="private-token">',check]
        self.final_effects=final_effects or {'redirect':'https://bero-host.de/customer/products'}
    def request(self,path,**kwargs):
        if not kwargs:
            self.gets.append(path)
            if path==bero.BALANCE_PAGE:
                return money_page(self.balances.pop(0) if len(self.balances)>1 else self.balances[0])
            return initial()
        self.sent.append(kwargs['json_payload'])
        i=len(self.sent)-1
        if i==6:
            return {'components':[{'snapshot':self.snapshots[7],'effects':self.final_effects}]}
        return {'components':[{'snapshot':self.snapshots[i+1],'effects':{'html':self.pages[i]}}]}
    def methods(self):
        return [p['components'][0]['calls'][0]['method']+str(p['components'][0]['calls'][0]['params']) for p in self.sent]

class CheckoutTests(unittest.TestCase):
    def setUp(self):
        with db() as c:
            c.execute('DELETE FROM tasks');c.execute('DELETE FROM merchant_accounts');c.execute('DELETE FROM hosts');c.execute('DELETE FROM logs')
            self.id=c.execute('INSERT INTO tasks(config,created) VALUES (?,?)',(json.dumps(adapters.validate(adapters.PRESETS['bero'])),time.time())).lastrowid
        self.policy=purchases.save_policy(self.id,{'max_total':'44.10','payment':'manual','auto_preflight':False})
        self.client=app.test_client()
        with self.client.session_transaction() as s:s.update(admin=True,csrf='csrf')

    def account(self,auto=True):
        accounts.save_credentials({'email':'account@example.com','password':'private-password','auto_relogin':auto})
        with db() as c:
            c.execute("UPDATE merchant_accounts SET session=?,status='authenticated',last_verified=?",(accounts.encode([COOKIE]),time.time()))

    def test_actual_soldout_coupon_flow_and_immutable_snapshots(self):
        merchant=Merchant();report=bero.preflight(merchant,BERO_CFG,self.policy)
        self.assertEqual(report['status'],'out_of_stock')
        self.assertEqual(report['overview_total_eur'],'44.10')
        self.assertTrue(report['coupon_accepted'])
        self.assertEqual(report['renewal_eur'],'49.00')
        self.assertFalse(report['paid']);self.assertFalse(report['order_submitted'])
        self.assertEqual([p['components'][0]['calls'][0]['method'] for p in merchant.sent],['selectPackage','jumpToStep','saveIndividualConfig','addCode'])
        for i,p in enumerate(merchant.sent):self.assertEqual(p['components'][0]['snapshot'],merchant.snapshots[i])
        self.assertNotIn('checksum',json.dumps(report));self.assertNotIn('synthetic-csrf',json.dumps(report))

    def test_disabled_next_never_called_even_if_snapshot_claims_stock(self):
        merchant=Merchant();merchant.snapshots[4]=snapshot(1100,True)
        report=bero.preflight(merchant,BERO_CFG,self.policy)
        self.assertEqual(report['status'],'out_of_stock');self.assertEqual(len(merchant.sent),4)

    def test_in_stock_preflight_reaches_check_page_and_never_orders(self):
        merchant=Merchant(stock=True);report=bero.preflight(merchant,BERO_CFG,self.policy)
        self.assertEqual(report['status'],'ready');self.assertTrue(report['final_checkout_verified'])
        self.assertEqual(report['final_total_eur'],'44.10');self.assertTrue(report['balance_sufficient'])
        self.assertEqual(len(merchant.sent),5);self.assertEqual(merchant.methods()[-1],'jumpToStep[1400]')
        self.assertFalse(report['paid']);self.assertFalse(report['order_submitted'])
        self.assertNotIn('private@example.com',json.dumps(report));self.assertNotIn('private-token',json.dumps(report))
        methods=[c['method'] for c in report['stages'][-1]['controls']]
        self.assertIn('toggleAgbConfirmation',methods);self.assertIn('jumpToStep',methods)

    def test_preflight_unknown_next_step_stops(self):
        merchant=Merchant(stock=True,next_step=1300)
        report=bero.preflight(merchant,BERO_CFG,self.policy)
        self.assertEqual(report['status'],'needs_checkout_adapter');self.assertFalse(report['order_submitted'])

    def test_coupon_rejected_and_high_price_stop(self):
        for page,status in [(fixture('bero-coupon109.html').replace('(DISCORD10)','(OTHER)'), 'coupon_rejected'),
                            (fixture('bero-coupon109.html').replace('44,10','44,11'), 'price_exceeded')]:
            merchant=Merchant(coupon_html=page)
            report=bero.preflight(merchant,BERO_CFG,self.policy)
            self.assertEqual(report['status'],status);self.assertEqual(len(merchant.sent),4)

    def test_changed_specs_or_cycle_stop_before_coupon(self):
        for old,new in [('365 Tage','720 Tage'),('12 GB','24 GB'),('120 GB','240 GB')]:
            merchant=Merchant();merchant.pages[2]=merchant.pages[2].replace(old,new)
            with self.assertRaises(bero.PreflightError):bero.preflight(merchant,BERO_CFG,self.policy)
            self.assertEqual(len(merchant.sent),3)

    def test_ambiguous_and_zero_price_rejected(self):
        soup=bero.BeautifulSoup(fixture('bero-coupon109.html'),'html.parser')
        soup.append(bero.BeautifulSoup('<p class="fw-bold">44,10 € INKL. MWST.</p>','html.parser'))
        with self.assertRaises(bero.PreflightError):bero.overview_quote(soup,TARGET)
        with self.assertRaises(bero.PreflightError):bero.overview_quote(bero.BeautifulSoup(fixture('bero-coupon109.html').replace('44,10','0,00'),'html.parser'),TARGET)

    def test_rejects_changed_package_snapshot_or_redirect(self):
        for value in (snapshot(package=108),snapshot().replace('synthetic-id','other-id')):
            merchant=Merchant();merchant.snapshots[1]=value
            with self.assertRaises(bero.PreflightError):bero.preflight(merchant,BERO_CFG,self.policy)
        merchant=MagicMock();merchant.request.side_effect=[initial(),{'components':[{'snapshot':snapshot(),'effects':{'redirect':'https://other.example'}}]}]
        with self.assertRaises(bero.PreflightError):bero.preflight(merchant,BERO_CFG,self.policy)

    def test_same_origin_endpoint_and_ui_action_whitelist(self):
        for target in ('https://other.example/update','https://bero-host.de:8443/livewire-test/update','/login'):
            with self.assertRaises(bero.PreflightError):bero.OrderView(MagicMock(),target=TARGET,html=initial().replace('/livewire-test/update',target))
        client=MagicMock();order=bero.OrderView(client,initial(),target=TARGET)
        for method,params,updates in [('purchase',[],{}),('jumpToStep',[2000],{}),('jumpToStep',[1500],{}),('toggleAgbConfirmation',[],{}),('selectPackage',[108],{}),('selectPackage',[109],{'packageAvailable':True}),('selectPackage',[109],{'enteredCode':'DISCORD10'})]:
            with self.assertRaises(bero.PreflightError):order.call(method,params,updates)
        client.request.assert_not_called()

    def test_challenge_stops(self):
        merchant=Merchant();merchant.pages[0]+='<div class="g-recaptcha"></div>'
        with self.assertRaises(bero.PreflightError):bero.preflight(merchant,BERO_CFG,self.policy)
        self.assertEqual(len(merchant.sent),1)

    def test_queue_requires_login_dedups_and_has_cooldown(self):
        with self.assertRaises(ValueError):checkout.enqueue(self.id)
        self.account();checkout.enqueue(self.id)
        with self.assertRaises(ValueError):checkout.enqueue(self.id)
        self.assertEqual(checkout.latest(self.id)['state'],'queued')
        with patch('merchants.bero.accounts.checkout_session') as session:
            session.return_value.__enter__.return_value=Merchant()
            self.assertTrue(checkout.run_next())
        self.assertEqual(checkout.latest(self.id)['result']['status'],'out_of_stock')
        with self.assertRaises(ValueError):checkout.enqueue(self.id)
        self.assertFalse(checkout.run_next())

    def test_api_auth_csrf_edit_clears_report(self):
        path=f'/api/tasks/{self.id}/checkout-preflight'
        self.assertEqual(app.test_client().get(path).status_code,401)
        self.assertEqual(self.client.post(path,json={}).status_code,403)
        self.account()
        self.assertEqual(self.client.post(path,json={},headers={'X-CSRF-Token':'csrf'}).status_code,202)
        self.assertEqual(self.client.get('/api/dashboard').json['tasks'][0]['checkout_preflight']['state'],'queued')
        self.client.put(f'/api/tasks/{self.id}',json=adapters.PRESETS['bero'],headers={'X-CSRF-Token':'csrf'})
        self.assertIsNone(checkout.latest(self.id));self.assertIsNone(purchases.policy_for(self.id))

    def test_paused_revision_invalidates_queued_job(self):
        self.account();checkout.enqueue(self.id)
        with db() as c:c.execute('UPDATE tasks SET revision=revision+1')
        with patch('merchants.bero.accounts.checkout_session') as session:self.assertTrue(checkout.run_next())
        session.assert_not_called();self.assertEqual(checkout.latest(self.id)['state'],'outdated')

    def test_unknown_network_error_is_secret_free(self):
        for error in (RuntimeError('private-password private-cookie'),ValueError('private-password private-cookie')):
            self.account()
            with db() as c:c.execute('DELETE FROM checkout_preflights');c.execute('DELETE FROM hosts')
            checkout.enqueue(self.id)
            with patch('merchants.bero.accounts.checkout_session',side_effect=error):checkout.run_next()
            self.assertEqual(checkout.latest(self.id)['state'],'error')
            self.assertNotIn('private-password',json.dumps(checkout.latest(self.id)))

    def test_auto_preflight_only_on_stock_transition(self):
        self.account();purchases.save_policy(self.id,{'max_total':'44.10','payment':'manual','auto_preflight':True})
        with db() as c:row=dict(c.execute('SELECT * FROM tasks WHERE id=?',(self.id,)).fetchone())
        cfg=json.loads(row['config']);row['status']='out_of_stock'
        result={'status':'available','detail':'In stock','price':'49.00','currency':'EUR'}
        with patch('worker.get_html',return_value=''),patch('worker.adapters.inspect',return_value=result),patch('worker.notifications.send',return_value={'sent':[],'failed':{},'complete':True}):worker.perform(row,cfg)
        self.assertEqual(checkout.latest(self.id)['state'],'queued')
        row['status']='available'
        with patch('worker.checkout.enqueue') as enqueue,patch('worker.get_html',return_value=''),patch('worker.adapters.inspect',return_value=result),patch('worker.notifications.send',return_value={'sent':[],'failed':{},'complete':True}):worker.perform(row,cfg)
        enqueue.assert_not_called();self.assertFalse(purchases.policy_for(self.id)['automatic_purchase_enabled'])

class CheckoutSessionTests(unittest.TestCase):
    def setUp(self):
        with db() as c:c.execute('DELETE FROM merchant_accounts')
        accounts.save_credentials({'email':'account@example.com','password':'private-password','auto_relogin':True})
        with db() as c:c.execute("UPDATE merchant_accounts SET session=?,status='authenticated',last_verified=?",(accounts.encode([COOKIE]),time.time()))

    def client(self,pages):
        client=MagicMock();client.request.side_effect=pages;client.snapshot.return_value=[COOKIE|{'value':'refreshed-cookie'}]
        return client

    def test_checkout_reuses_and_persists_refreshed_cookies(self):
        client=self.client([AUTHENTICATED])
        with patch('accounts.BeroSession',return_value=client):
            with accounts.checkout_session() as selected:self.assertIs(selected,client)
        client.request.assert_called_once_with('/login')
        self.assertEqual(accounts.decode(accounts.account_row()['session'])[0]['value'],'refreshed-cookie')
        self.assertEqual(accounts.account_row()['lease'],0)

    def test_expired_cookie_relogs_only_if_enabled(self):
        client=self.client([LOGIN,AUTHENTICATED,AUTHENTICATED])
        with patch('accounts.BeroSession',return_value=client):
            with accounts.checkout_session():pass
        self.assertEqual(client.request.call_args_list[1].args[1]['remember'],'on')
        accounts.save_credentials({'email':'account@example.com','password':'','auto_relogin':False})
        client=self.client([LOGIN])
        with patch('accounts.BeroSession',return_value=client):
            with self.assertRaises(accounts.LoginIssue):
                with accounts.checkout_session():self.fail('expired session must not be loaned')
        self.assertFalse(accounts.public_status()['session_saved'])

    def test_account_lease_and_change_invalidate_preflight(self):
        with db() as c:c.execute('UPDATE merchant_accounts SET lease=?',(time.time()+100,))
        with self.assertRaises(ValueError):
            with accounts.checkout_session():pass
        with db() as c:c.execute('UPDATE merchant_accounts SET lease=0')
        client=self.client([AUTHENTICATED])
        with patch('accounts.BeroSession',return_value=client):
            with self.assertRaises(ValueError):
                with accounts.checkout_session():accounts.disconnect()
        self.assertIsNone(accounts.account_row())

    def test_json_transport_no_redirect_replay(self):
        conn=MagicMock();response=conn.getresponse.return_value
        response.status=302;response.headers=Message()
        client=accounts.BeroSession([COOKIE])
        with patch('accounts.public_ips',return_value=['1.1.1.1']),patch('accounts.PinnedHTTPS',return_value=conn):
            with self.assertRaises(accounts.LoginIssue):client.request('/livewire-test/update',json_payload={'test':True},referer=TARGET.store)
        self.assertEqual(conn.request.call_count,1)

    def test_json_transport_sends_csrf_origin_cookie_and_reads_json(self):
        conn=MagicMock();response=conn.getresponse.return_value
        response.status=200;response.headers=Message();response.getheader.return_value='application/json'
        response.read.return_value=b'{"components":[]}'
        client=accounts.BeroSession([COOKIE]);payload={'_token':'synthetic-token','components':[]}
        with patch('accounts.public_ips',return_value=['1.1.1.1']),patch('accounts.PinnedHTTPS',return_value=conn):
            self.assertEqual(client.request('/livewire-test/update',json_payload=payload,referer=TARGET.store),{'components':[]})
        args=conn.request.call_args
        self.assertEqual(args.args,('POST','/livewire-test/update'))
        self.assertEqual(json.loads(args.kwargs['body']),payload)
        self.assertEqual(args.kwargs['headers']['Content-type'],'application/json')
        self.assertEqual(args.kwargs['headers']['Origin'],accounts.ORIGIN)
        self.assertEqual(args.kwargs['headers']['Referer'],TARGET.store)
        self.assertEqual(args.kwargs['headers']['Cookie'],'bero_session=secret-cookie-value')
