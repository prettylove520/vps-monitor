import time
import unittest
from unittest.mock import patch
from werkzeug.security import generate_password_hash
import test_app  # noqa: F401
from app import app
from store import db, set_setting
import twofactor

PASSWORD='correct-horse-battery'

class TwoFactorTests(unittest.TestCase):
    def setUp(self):
        twofactor.force_disable()
        set_setting('admin_password',generate_password_hash(PASSWORD))
        with db() as c:c.execute('DELETE FROM attempts')
        self.client=app.test_client()
        with self.client.session_transaction() as s:s.update(admin=True,csrf='csrf')

    def post(self,path,data,client=None):
        return (client or self.client).post('/api'+path,json=data,headers={'X-CSRF-Token':'csrf'})

    def enable(self):
        setup=self.post('/security/totp/setup',{}).json
        self.assertTrue(setup['qr'].startswith('data:image/svg+xml;base64,'))
        self.assertIn('otpauth://totp/',setup['uri'])
        self.assertEqual(self.post('/security/totp/enable',{'code':'000000'}).status_code,400)
        code=twofactor.code_at(setup['secret'],int(time.time()//30)-1)  # previous step: allowed drift
        self.assertEqual(self.post('/security/totp/enable',{'code':code}).status_code,200)
        return setup['secret']

    def test_rfc6238_vector(self):
        secret='GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'  # base32("12345678901234567890")
        self.assertEqual(twofactor.code_at(secret,59//30),'287082')
        self.assertEqual(twofactor.code_at(secret,1111111109//30),'081804')

    def test_login_requires_code_once_enabled_and_blocks_replay(self):
        secret=self.enable()
        self.assertTrue(self.client.get('/api/security').json['totp_enabled'])
        anon=app.test_client()
        with anon.session_transaction() as s:s['csrf']='csrf'
        r=self.post('/login',{'password':PASSWORD},anon)
        self.assertEqual(r.status_code,401);self.assertTrue(r.json['totp_required'])
        self.assertEqual(self.post('/login',{'password':PASSWORD,'code':'123456'},anon).status_code,401)
        with db() as c:c.execute('DELETE FROM attempts')
        later=time.time()+30
        code=twofactor.code_at(secret,int(later//30))
        with patch('twofactor.time.time',return_value=later):
            self.assertEqual(self.post('/login',{'password':PASSWORD,'code':code},anon).status_code,200)
            anon2=app.test_client()
            with anon2.session_transaction() as s:s['csrf']='csrf'
            self.assertEqual(self.post('/login',{'password':PASSWORD,'code':code},anon2).status_code,401)  # replay
        # wrong password never reveals whether 2FA is on
        r=self.post('/login',{'password':'wrong-password-x'},app.test_client())
        self.assertNotIn('totp_required',r.json or {})

    def test_disable_needs_password_and_code(self):
        secret=self.enable()
        later=time.time()+60
        code=twofactor.code_at(secret,int(later//30))
        with patch('twofactor.time.time',return_value=later):
            self.assertEqual(self.post('/security/totp/disable',{'password':'nope','code':code}).status_code,400)
            self.assertTrue(twofactor.enabled())
            self.assertEqual(self.post('/security/totp/disable',{'password':PASSWORD,'code':code}).status_code,200)
        self.assertFalse(twofactor.enabled())

    def test_force_disable_and_setup_guard(self):
        self.enable()
        self.assertEqual(self.post('/security/totp/setup',{}).status_code,400)
        twofactor.force_disable()
        self.assertFalse(twofactor.enabled())

if __name__=='__main__':unittest.main()
