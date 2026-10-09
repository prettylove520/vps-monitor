import json
import time
import unittest
from unittest.mock import patch
import test_app as helpers
from test_checkout import Merchant, fixture
from test_accounts import COOKIE
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

ORDER=['selectPackage[109]','jumpToStep[1000]',"saveIndividualConfig[{'runtime': '365'}]",'addCode[]',
       'jumpToStep[1400]','toggleAgbConfirmation[]','jumpToStep[1500]']

class PurchaseTests(unittest.TestCase):
    def setUp(self):
        with db() as c:
            for table in ('tasks','merchant_accounts','hosts','logs','purchase_attempts','purchase_policies','checkout_preflights'):
                c.execute('DELETE FROM '+table)
            self.id=c.execute('INSERT INTO tasks(config,created,enabled) VALUES (?,?,1)',(json.dumps(adapters.validate(adapters.PRESETS['bero'])),time.time())).lastrowid
        accounts.save_credentials({'email':'account@example.com','password':'private-password','auto_relogin':True})
        with db() as c:
            c.execute("UPDATE merchant_accounts SET session=?,status='authenticated',last_verified=?",(accounts.encode([COOKIE]),time.time()))
        purchases.save_policy(self.id,{'max_total':'44.10','payment':'balance'})
        self.policy=purchases.arm(self.id,'44.10')
        self.client=app.test_client()
        with self.client.session_transaction() as s:s.update(admin=True,csrf='csrf')

    def attempts(self):
        with db() as c:return [dict(r) for r in c.execute('SELECT * FROM purchase_attempts')]

    def test_full_order_in_verified_sequence_and_balance_confirmation(self):
        merchant=Merchant(stock=True)
        report=bero.purchase(merchant,BERO_CFG,self.policy,self.id)
        self.assertEqual(report['status'],'purchased');self.assertTrue(report['paid'])
        self.assertEqual(merchant.methods(),ORDER)
        self.assertEqual(report['final_total_eur'],'44.10')
        self.assertEqual(self.attempts()[0]['state'],'success')
        # snapshots are passed back unchanged, the coupon is the only property update
        for i,p in enumerate(merchant.sent):self.assertEqual(p['components'][0]['snapshot'],merchant.snapshots[i])
        self.assertEqual([p['components'][0]['updates'] for p in merchant.sent].count({'enteredCode':'DISCORD10'}),1)
        with self.assertRaises(purchases.AttemptExists):
            bero.purchase(Merchant(stock=True),BERO_CFG,self.policy,self.id)

    def test_out_of_stock_at_checkout_reserves_nothing(self):
        merchant=Merchant(stock=False)
        report=bero.purchase(merchant,BERO_CFG,self.policy,self.id)
        self.assertEqual(report['status'],'out_of_stock');self.assertFalse(report['order_submitted'])
        self.assertEqual(len(merchant.sent),4);self.assertEqual(self.attempts(),[])

    def test_insufficient_balance_stops_before_checkout(self):
        merchant=Merchant(stock=True,balances=('40,00',))
        report=bero.purchase(merchant,BERO_CFG,self.policy,self.id)
        self.assertEqual(report['status'],'insufficient_balance');self.assertEqual(merchant.sent,[])

    def test_check_page_price_change_or_wrong_specs_stop(self):
        for old,new in [('44,10','44,11'),('365 Tage','30 Tage')]:
            merchant=Merchant(stock=True,check_html=fixture('bero-check109.html').replace(old,new))
            try:
                report=bero.purchase(merchant,BERO_CFG,self.policy,self.id)
                self.assertEqual(report['status'],'price_exceeded')
            except bero.PreflightError:
                pass
            self.assertNotIn('toggleAgbConfirmation[]',merchant.methods());self.assertEqual(self.attempts(),[])

    def test_changed_order_form_stops(self):
        for old,new in [('jumpToStep(1500)','placeOrder'),('wire:click="toggleAgbConfirmation"','wire:click="other"')]:
            merchant=Merchant(stock=True,check_html=fixture('bero-check109.html').replace(old,new))
            with self.assertRaises(bero.PreflightError):bero.purchase(merchant,BERO_CFG,self.policy,self.id)
            self.assertEqual(len(merchant.sent),5);self.assertEqual(self.attempts(),[])

    def test_unexpected_next_step_stops(self):
        merchant=Merchant(stock=True,next_step=1300)
        report=bero.purchase(merchant,BERO_CFG,self.policy,self.id)
        self.assertEqual(report['status'],'unexpected_flow');self.assertEqual(len(merchant.sent),4)

    def test_terms_toggle_failure_releases_reservation(self):
        merchant=Merchant(stock=True);merchant.snapshots[6]=merchant.snapshots[5]  # agb stays false
        with self.assertRaises(bero.PreflightError):bero.purchase(merchant,BERO_CFG,self.policy,self.id)
        self.assertNotIn('jumpToStep[1500]',merchant.methods());self.assertEqual(self.attempts(),[])

    def test_lost_submit_response_is_uncertain_and_never_resent(self):
        merchant=Merchant(stock=True,balances=('44,10','44,10'))
        original=merchant.request
        def flaky(path,**kwargs):
            if kwargs and kwargs['json_payload']['components'][0]['calls'][0]['params']==[1500]:
                merchant.sent.append(kwargs['json_payload']);raise OSError('connection reset')
            return original(path,**kwargs)
        merchant.request=flaky
        report=bero.purchase(merchant,BERO_CFG,self.policy,self.id)
        self.assertEqual(report['status'],'uncertain');self.assertTrue(report['order_submitted'])
        self.assertEqual(merchant.methods().count('jumpToStep[1500]'),1)
        self.assertEqual(self.attempts()[0]['state'],'uncertain')

    def test_disarmed_policy_blocks_terms_and_submit(self):
        order=bero.OrderView(Merchant(),__import__('test_checkout').initial(),target=TARGET)
        for method,params in (('toggleAgbConfirmation',[]),('jumpToStep',[1500])):
            with self.assertRaises(bero.PreflightError):order.call(method,params)
        purchases.disarm(self.id)
        with self.assertRaises(ValueError):checkout.enqueue(self.id,'purchase')

    def test_run_next_purchase_ignores_host_spacing_and_finishes_task(self):
        checkout.enqueue(self.id,'purchase')
        with db() as c:c.execute("INSERT INTO hosts VALUES ('bero-host.de',?)",(time.time()+100,))
        notices=[]
        with patch('merchants.bero.accounts.checkout_session') as session:
            session.return_value.__enter__.return_value=Merchant(stock=True)
            self.assertTrue(checkout.run_next(notify=lambda *a:notices.append(a)))
        job=checkout.latest(self.id)
        self.assertEqual(job['mode'],'purchase');self.assertEqual(job['result']['status'],'purchased')
        self.assertEqual(notices[0][2]['status'],'purchased')
        self.assertEqual(purchases.policy_for(self.id)['status'],'purchased')
        with db() as c:self.assertEqual(c.execute('SELECT enabled FROM tasks WHERE id=?',(self.id,)).fetchone()[0],0)
        with self.assertRaises(ValueError):checkout.enqueue(self.id,'purchase')

    def test_worker_queues_purchase_before_restock_notification(self):
        with db() as c:row=dict(c.execute('SELECT * FROM tasks WHERE id=?',(self.id,)).fetchone())
        cfg=json.loads(row['config']);row['status']='out_of_stock'
        result={'status':'available','detail':'In stock','price':'49.00','currency':'EUR'}
        with patch('worker.get_html',return_value=''),patch('worker.adapters.inspect',return_value=result),patch('worker.notifications.send') as send:
            worker.perform(row,cfg)
        send.assert_not_called()
        self.assertEqual(checkout.latest(self.id)['mode'],'purchase')
        with db() as c:self.assertLessEqual(c.execute('SELECT next_check FROM tasks WHERE id=?',(self.id,)).fetchone()[0],time.time()+17)

    def test_notice_texts(self):
        with patch('worker.notifications.send',return_value={'sent':['bark'],'failed':{},'complete':True}) as send:
            worker.checkout_notice(self.id,'purchase',{'status':'purchased','message':'ok','final_total_eur':'44.10'})
            worker.checkout_notice(self.id,'purchase',{'status':'out_of_stock','message':'x'})
            worker.checkout_notice(self.id,'preflight',{'status':'ready','message':'x','balance_sufficient':True})
            worker.checkout_notice(self.id,'preflight',{'status':'error','message':'changed'})
        self.assertEqual(send.call_count,2)
        self.assertTrue(send.call_args_list[0].kwargs['urgent']);self.assertFalse(send.call_args_list[1].kwargs['urgent'])

    def test_health_check_queues_dry_run_every_interval(self):
        with db() as c:c.execute("DELETE FROM settings WHERE key='checkout_health_last'")
        worker.health_check()
        self.assertEqual(checkout.latest(self.id)['mode'],'preflight')
        with db() as c:c.execute('DELETE FROM checkout_preflights')
        worker.health_check();self.assertIsNone(checkout.latest(self.id))

    def test_arm_api_requires_confirmation_balance_and_login(self):
        h={'X-CSRF-Token':'csrf'};path=f'/api/tasks/{self.id}/purchase/arm'
        self.assertIn(app.test_client().post(path,json={}).status_code,(401,403))
        self.client.put(f'/api/tasks/{self.id}/purchase-policy',json={'payment':'balance','max_total':'44.10'},headers=h)
        self.assertFalse(purchases.is_armed(purchases.policy_for(self.id)))  # saving disarms
        self.assertEqual(self.client.post(path,json={'confirm_total':'49.00'},headers=h).status_code,400)
        self.assertEqual(self.client.post(path,json={'confirm_total':'44,10'},headers=h).status_code,200)
        self.assertTrue(purchases.is_armed(purchases.policy_for(self.id)))
        self.assertEqual(self.client.post(f'/api/tasks/{self.id}/purchase/disarm',json={},headers=h).status_code,200)
        self.client.put(f'/api/tasks/{self.id}/purchase-policy',json={'payment':'manual','max_total':'44.10'},headers=h)
        self.assertEqual(self.client.post(path,json={'confirm_total':'44.10'},headers=h).status_code,400)
        self.client.put(f'/api/tasks/{self.id}/purchase-policy',json={'payment':'balance','max_total':'44.10'},headers=h)
        with db() as c:c.execute("UPDATE merchant_accounts SET status='expired'")
        self.assertEqual(self.client.post(path,json={'confirm_total':'44.10'},headers=h).status_code,400)

    def test_clear_attempts_requires_typed_confirmation(self):
        bero.purchase(Merchant(stock=True),BERO_CFG,self.policy,self.id)
        h={'X-CSRF-Token':'csrf'};path=f'/api/tasks/{self.id}/purchase-attempts/clear'
        task=self.client.get('/api/dashboard').json['tasks'][0]
        self.assertEqual(task['purchase_attempt']['state'],'success');self.assertTrue(task['checkout_supported'])
        self.assertEqual(self.client.post(path,json={'confirm':'yes'},headers=h).status_code,400)
        self.assertEqual(self.client.post(path,json={'confirm':'已核实'},headers=h).status_code,200)
        self.assertEqual([a['state'] for a in self.attempts()],['cleared'])
        self.assertIsNone(purchases.attempt_for(self.id))
        # history is kept, and the task can be armed again for a new purchase
        purchases.save_policy(self.id,{'payment':'balance','max_total':'44.10'})
        self.assertTrue(purchases.is_armed(purchases.arm(self.id,'44.10')))

    def test_english_account_ui(self):
        """Logged-in BERO accounts may render English labels (seen live 2026-10-09)."""
        import test_checkout
        original=test_checkout.fixture
        def english(name):
            return (original(name).replace('Laufzeit:','Runtime:').replace('INKL. MWST.','INCL. VAT.')
                    .replace('Ausverkauft','Sold out').replace('Kontostand','Account Balance'))
        with patch('test_checkout.fixture',english):
            self.assertEqual(adapters.inspect(english('bero.html'),adapters.PRESETS['bero'])['status'],'out_of_stock')
            report=bero.preflight(Merchant(stock=True),BERO_CFG,self.policy)
            self.assertEqual(report['status'],'ready')
            self.assertEqual(bero.purchase(Merchant(stock=True),BERO_CFG,self.policy,self.id)['status'],'purchased')

    def test_store_page_problem_is_reported_specifically(self):
        merchant=Merchant(stock=True)
        changed=fixture('bero.html').replace('120 GB','240 GB')
        merchant.request=lambda path,**k:changed
        with self.assertRaisesRegex(bero.PreflightError,'商品页检查未通过'):
            bero.preflight(merchant,BERO_CFG,self.policy)

    def test_duplicated_or_nested_package_card(self):
        import test_checkout
        doubled=lambda: test_checkout.initial().replace('</div>',fixture('bero.html')+'</div>',1)
        for page in (doubled(),test_checkout.initial().replace('Anniversary M','Anniversary M <button wire:click="selectPackage(109)">Select</button>',1)):
            merchant=Merchant(stock=True);original=merchant.request
            merchant.request=lambda path,_o=original,_p=page,**k:_p if not k and path==TARGET.store else _o(path,**k)
            self.assertEqual(bero.preflight(merchant,BERO_CFG,self.policy)['status'],'ready')

    def test_resumes_from_order_state_left_on_overview(self):
        import html as h, test_checkout
        merchant=Merchant(stock=True);original=merchant.request
        start=('<meta name="csrf-token" content="t"><script data-update-uri="/livewire-test/update"></script>'
               '<div wire:name="public.order.order" wire:snapshot="'+h.escape(test_checkout.snapshot(1100),quote=True)+'">'
               '<button wire:click.prevent="jumpToStep(1000)">ZURÜCK</button></div>')
        back=[{'components':[{'snapshot':test_checkout.snapshot(1000),'effects':{'html':'<button wire:click.prevent="backFromConfiguration()">ZURÜCK</button>'}}]},
              {'components':[{'snapshot':test_checkout.snapshot(500,package=108),'effects':{'html':fixture('bero.html')}}]}]
        calls=[]
        def request(path,**k):
            if not k and path==TARGET.store:return start
            if k and back:calls.append(k['json_payload']['components'][0]['calls'][0]['method']);return back.pop(0)
            return original(path,**k)
        merchant.request=request
        report=bero.preflight(merchant,BERO_CFG,self.policy)
        self.assertEqual(calls,['jumpToStep','backFromConfiguration']);self.assertEqual(report['status'],'ready')

    def test_money_parser(self):
        self.assertEqual(str(bero.money('1.044,10')),'1044.10');self.assertEqual(str(bero.money('44.10')),'44.10')
        for bad in ('44','4,4','-1,00','1e3'):
            with self.assertRaises(bero.PreflightError):bero.money(bad)

if __name__=='__main__':unittest.main()
