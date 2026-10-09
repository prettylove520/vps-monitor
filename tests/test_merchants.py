import json
import time
import unittest
from pathlib import Path
import test_app  # noqa: F401  (sets up the temporary data directory)
from store import db
import adapters
import merchants
import purchases
from merchants import bero, whmcs
from network import Page, FetchError
from unittest.mock import patch
import worker

FIXTURES=Path(__file__).parent/'fixtures'
BERO=adapters.PRESETS['bero']

class MerchantRegistryTests(unittest.TestCase):
    def test_registry_and_presets(self):
        self.assertEqual(set(merchants.REGISTRY),{'bero','whmcs','vmiss','generic'})
        self.assertTrue(merchants.get('bero').SUPPORTS_CHECKOUT)
        self.assertFalse(merchants.get('vmiss').SUPPORTS_CHECKOUT)
        with self.assertRaises(ValueError):merchants.get('unknown')
        with self.assertRaises(ValueError):merchants.checkout_merchant({'adapter':'generic'})

    def test_bero_task_fields_are_configurable_and_validated(self):
        cfg=adapters.validate(BERO|{'package_id':'108','package_name':'Anniversary S','runtime_days':'365',
                                    'specs':' CPU: 2 Kerne \n\nRAM: 6 GB','coupon':'','max_price':'39.00'})
        self.assertEqual((cfg['package_id'],cfg['runtime_days'],cfg['specs']),(108,365,'CPU: 2 Kerne\nRAM: 6 GB'))
        t=bero.target(cfg)
        self.assertEqual(t.select,'selectPackage(108)')
        self.assertEqual(t.specs,('Anniversary S','CPU: 2 Kerne','RAM: 6 GB','Laufzeit: 365 Tage'))
        bad=[{'url':'https://example.com/server/x'},{'url':'https://bero-host.de/login'},{'package_id':'abc'},
             {'package_id':'0'},{'runtime_days':'0'},{'package_name':''},{'coupon':'BAD CODE!'},{'currency':'USD'}]
        for delta in bad:
            with self.assertRaises(ValueError,msg=delta):adapters.validate(BERO|delta)

    def test_other_package_is_monitored_by_its_own_id_and_specs(self):
        page=(FIXTURES/'bero.html').read_text(encoding='utf-8')
        other=page.replace('selectPackage(109)','selectPackage(108)').replace('Anniversary M','Anniversary S')
        cfg=adapters.validate(BERO|{'package_id':108,'package_name':'Anniversary S'})
        self.assertEqual(adapters.inspect(other,cfg)['status'],'out_of_stock')
        with self.assertRaises(ValueError):adapters.inspect(page,cfg)  # card for 108 missing
        with self.assertRaises(ValueError):  # specs must match
            adapters.inspect(other,cfg|{'specs':'CPU: 8 Kerne'})

    def test_coupon_is_optional(self):
        soup=bero.BeautifulSoup((FIXTURES/'bero-overview109.html').read_text(encoding='utf-8'),'html.parser')
        cfg=adapters.validate(BERO|{'coupon':''})
        self.assertTrue(bero.overview_quote(soup,bero.target(cfg))['coupon_accepted'])
        self.assertFalse(bero.overview_quote(soup,bero.target(adapters.validate(BERO)))['coupon_accepted'])

class PolicyAndMigrationTests(unittest.TestCase):
    def setUp(self):
        with db() as c:
            for t in ('tasks','purchase_attempts','purchase_policies','merchant_accounts'):
                c.execute('DELETE FROM '+t)

    def test_legacy_bero_task_is_migrated_without_revision_change(self):
        legacy={k:v for k,v in BERO.items() if k not in ('package_id','package_name','runtime_days','specs')}
        with db() as c:
            tid=c.execute('INSERT INTO tasks(config,created,revision) VALUES (?,?,7)',(json.dumps(legacy),time.time())).lastrowid
        adapters.migrate()
        with db() as c:row=c.execute('SELECT config,revision FROM tasks WHERE id=?',(tid,)).fetchone()
        cfg=json.loads(row[0])
        self.assertEqual((cfg['package_id'],cfg['package_name'],cfg['runtime_days'],row[1]),(109,'Anniversary M',365,7))
        self.assertEqual(adapters.validate(cfg)['package_id'],109)

    def test_user_sets_cap_and_monitor_only_merchants_cannot_buy(self):
        with db() as c:
            tid=c.execute('INSERT INTO tasks(config,created) VALUES (?,?)',(json.dumps(adapters.validate(BERO)),time.time())).lastrowid
            vid=c.execute('INSERT INTO tasks(config,created) VALUES (?,?)',(json.dumps(adapters.validate(adapters.PRESETS['vmiss'])),time.time())).lastrowid
        self.assertEqual(purchases.save_policy(tid,{'payment':'balance'})['max_total'],'49.00')  # defaults to page cap
        self.assertEqual(purchases.save_policy(tid,{'payment':'balance','max_total':'44,1'})['max_total'],'44.10')
        self.assertEqual(purchases.save_policy(tid,{'payment':'balance'})['max_total'],'44.10')  # keeps previous
        for bad in ('0','-1','abc','1.234'):
            with self.assertRaises(ValueError):purchases.save_policy(tid,{'payment':'balance','max_total':bad})
        with self.assertRaises(ValueError):purchases.save_policy(vid,{'payment':'balance'})

    def test_legacy_attempt_still_blocks_rearming(self):
        with db() as c:
            tid=c.execute('INSERT INTO tasks(config,created) VALUES (?,?)',(json.dumps(adapters.validate(BERO)),time.time())).lastrowid
            c.execute("INSERT INTO purchase_attempts(task_id,fingerprint,state,created) VALUES (?,'BERO:AnniversaryM:109:365:one-purchase','uncertain',?)",(tid,time.time()))
        self.assertEqual(purchases.attempt_for(tid)['state'],'uncertain')
        purchases.save_policy(tid,{'payment':'balance','max_total':'44.10'})
        with self.assertRaises(ValueError):purchases.arm(tid,'44.10')

class WhmcsTests(unittest.TestCase):
    def cfg(self,**kw):
        return adapters.validate(adapters.PRESETS['whmcs']|{'name':'Test'}|kw)

    def test_link_parsing_and_validation(self):
        cfg=self.cfg(url='https://my.example.com/cart.php?a=add&pid=123')
        self.assertEqual((cfg['url'],cfg['pid'],cfg['max_price']),('https://my.example.com/','123',''))
        self.assertEqual(whmcs.order_url(cfg),'https://my.example.com/cart.php?a=add&pid=123')
        cfg=self.cfg(url='https://example.com/billing/cart.php?a=add&pid=5&billingcycle=annually')
        self.assertEqual(whmcs.order_url(cfg),'https://example.com/billing/cart.php?a=add&pid=5')
        cfg=self.cfg(url='https://example.com/billing/',pid='42',max_price='10.00')
        self.assertEqual(whmcs.order_url(cfg),'https://example.com/billing/cart.php?a=add&pid=42')
        for bad in ({'url':'https://example.com/'},{'url':'https://example.com/','pid':'x1'},
                    {'url':'https://example.com/cart.php?a=add&pid=1','pid':'2'},{'url':'http://example.com/','pid':'1'}):
            with self.assertRaises(Exception,msg=bad):self.cfg(**bad)
        with self.assertRaises(ValueError):  # empty price cap only allowed where the price is unknown
            adapters.validate(adapters.PRESETS['generic']|{'name':'x','url':'https://example.com/','scope':'#a','identity':'a',
                              'price_selector':'.p','in_stock':'in','out_stock':'out','max_price':''})

    def test_classification(self):
        page=lambda status,html='',location='':Page('https://h/cart.php?a=add&pid=1',status,html,location)
        oos=(FIXTURES/'whmcs-outofstock.html').read_text(encoding='utf-8')
        self.assertEqual(whmcs.classify(page(302,location='https://h/cart.php?a=confproduct&i=0'))[0],'available')
        self.assertEqual(whmcs.classify(page(200,oos))[0],'out_of_stock')
        self.assertEqual(whmcs.classify(page(200,'<h1>缺货</h1><p>该商品暂时缺货</p>'))[0],'out_of_stock')
        self.assertEqual(whmcs.classify(page(200,'<form id="frmConfigureProduct"></form>'))[0],'available')
        for bad in (page(302,location='https://h/cart.php?a=view'),page(302,location='https://h/login'),
                    page(200,'<p>hello</p>'),page(200,oos+'<form id="frmConfigureProduct"></form>')):
            with self.assertRaises(ValueError):whmcs.classify(bad)

    def test_check_and_worker_flow(self):
        cfg=self.cfg(url='https://my.example.com/cart.php?a=add&pid=123')
        with patch('merchants.whmcs.fetch',return_value=Page('u',302,location='https://my.example.com/cart.php?a=confproduct&i=0')) as f:
            result=adapters.check(cfg,None)
        f.assert_called_once_with('https://my.example.com/cart.php?a=add&pid=123',follow=False)
        self.assertEqual((result['status'],result['price']),('available',None))
        challenge=Page('u',200,'<title>Just a moment...</title>')
        with patch('merchants.whmcs.fetch',return_value=challenge):
            with self.assertRaises(FetchError) as ctx:adapters.check(cfg,None)
        self.assertTrue(ctx.exception.blocked)
        with db() as c:
            c.execute('DELETE FROM tasks')
            tid=c.execute('INSERT INTO tasks(config,created,enabled) VALUES (?,?,1)',(json.dumps(cfg),time.time())).lastrowid
            row=dict(c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())
        with patch('merchants.whmcs.fetch',return_value=Page('u',302,location='https://my.example.com/cart.php?a=confproduct&i=0')),\
             patch('worker.get_html') as get_html,patch('worker.notifications.send',return_value={'sent':['bark'],'failed':{},'complete':True}) as send:
            worker.perform(row,cfg)
        get_html.assert_not_called();send.assert_called_once()
        with db() as c:self.assertEqual(c.execute('SELECT status FROM tasks WHERE id=?',(tid,)).fetchone()[0],'available')

if __name__=='__main__':unittest.main()
