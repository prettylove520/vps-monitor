import json
import time
import unittest
from pathlib import Path
import test_app  # noqa: F401  (sets up the temporary data directory)
from store import db
import adapters
import merchants
import purchases
from merchants import bero

FIXTURES=Path(__file__).parent/'fixtures'
BERO=adapters.PRESETS['bero']

class MerchantRegistryTests(unittest.TestCase):
    def test_registry_and_presets(self):
        self.assertEqual(set(merchants.REGISTRY),{'bero','vmiss','generic'})
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

if __name__=='__main__':unittest.main()
