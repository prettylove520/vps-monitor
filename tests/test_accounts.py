import time
import json
import unittest
from unittest.mock import patch, MagicMock
from email.message import Message
import test_app as helpers
from app import app
from store import db
import accounts

LOGIN='''<form method="POST" action="https://bero-host.de/login"><input name="_token" value="fresh-csrf"><input type="email" name="email"><input type="password" name="password"><input type="checkbox" name="remember"></form>'''
AUTHENTICATED='<h1>Account</h1><form method="POST" action="https://bero-host.de/logout"></form>'
COOKIE={'version':0,'name':'bero_session','value':'secret-cookie-value','port':None,'port_specified':False,
        'domain':'bero-host.de','domain_specified':False,'domain_initial_dot':False,'path':'/',
        'path_specified':True,'secure':True,'expires':None,'discard':True,'comment':None,
        'comment_url':None,'rfc2109':False,'rest':{'HttpOnly':None}}

class AccountTests(unittest.TestCase):
    def setUp(self):
        with db() as c:c.execute('DELETE FROM merchant_accounts')
        self.credentials={'email':'account@example.com','password':'private-password','auto_relogin':True}

    def prepare_client(self,pages):
        client=MagicMock()
        client.request.side_effect=pages
        client.snapshot.return_value=[COOKIE]
        return client

    def save(self):
        accounts.save_credentials(self.credentials)

    def login(self):
        self.save()
        client=self.prepare_client([LOGIN,AUTHENTICATED,AUTHENTICATED])
        with patch('accounts.BeroSession',return_value=client):result=accounts.connect()
        return result,client

    def test_credentials_encrypted_and_never_returned(self):
        self.save()
        raw=accounts.account_row()['credentials']
        self.assertNotIn('private-password',raw)
        self.assertEqual(accounts.public_status()['email'],'account@example.com')
        self.assertNotIn('private-password',json.dumps(accounts.public_status()))
        accounts.save_credentials(self.credentials|{'password':'','auto_relogin':False})
        self.assertEqual(accounts.decode(accounts.account_row()['credentials'])['password'],'private-password')
        self.assertFalse(accounts.public_status()['auto_relogin'])
        with self.assertRaises(ValueError):accounts.save_credentials({'email':'new@example.com','password':''})

    def test_normal_form_login_and_cookie_persistence(self):
        result,client=self.login()
        self.assertEqual(result['status'],'authenticated')
        self.assertTrue(result['session_saved'])
        self.assertEqual(client.request.call_count,3)
        post=client.request.call_args_list[1].args[1]
        self.assertEqual((post['_token'],post['email'],post['password'],post['remember']),
                         ('fresh-csrf','account@example.com','private-password','on'))
        with db() as c:encoded=c.execute("SELECT session FROM merchant_accounts WHERE merchant='bero'").fetchone()[0]
        self.assertNotIn('secret-cookie-value',encoded)
        self.assertEqual(accounts.decode(encoded),[COOKIE])

    def test_restart_reuses_session_without_password_submission(self):
        self.login()
        client=self.prepare_client([AUTHENTICATED])
        with patch('accounts.BeroSession',return_value=client) as cls:result=accounts.connect()
        cls.assert_called_once_with([COOKIE])
        client.request.assert_called_once_with('/login')
        self.assertIn('复用',result['message'])

    def test_cookiejar_survives_serialization_and_expires(self):
        jar=accounts.BeroSession([COOKIE])
        saved=jar.snapshot()
        self.assertEqual(saved[0]['value'],'secret-cookie-value')
        self.assertEqual(accounts.BeroSession(saved).snapshot(),saved)
        self.assertEqual(accounts.BeroSession([COOKIE|{'expires':time.time()-1}]).snapshot(),[])
        self.assertEqual(accounts.BeroSession([COOKIE|{'domain':'other.example'}]).snapshot(),[])

    def test_expired_session_auto_relogin_uses_saved_credentials(self):
        self.login()
        with db() as c:c.execute("UPDATE merchant_accounts SET next_check=0")
        client=self.prepare_client([LOGIN,AUTHENTICATED,AUTHENTICATED])
        with patch('accounts.BeroSession',return_value=client):self.assertIsNone(accounts.maintain())
        self.assertEqual(client.request.call_args_list[1].args[1]['password'],'private-password')
        self.assertEqual(accounts.public_status()['status'],'authenticated')

    def test_expired_session_without_permission_stops(self):
        self.login()
        accounts.save_credentials(self.credentials|{'password':'','auto_relogin':False})
        with db() as c:c.execute("UPDATE merchant_accounts SET next_check=0")
        client=self.prepare_client([LOGIN])
        with patch('accounts.BeroSession',return_value=client):message=accounts.maintain()
        client.request.assert_called_once_with('/login')
        self.assertIn('失效',message)
        self.assertEqual(accounts.public_status()['status'],'expired')
        self.assertFalse(accounts.public_status()['session_saved'])
        self.assertIsNone(accounts.maintain())

    def test_challenges_stop_without_submitting_credentials(self):
        for page in ('<input autocomplete="one-time-code">','<div class="g-recaptcha"></div>',
                     '<title>Just a moment</title>',LOGIN.replace('https://bero-host.de/login','https://other.example/login'),
                     '<a href="https://other.example/logout">Logout</a>'):
            with db() as c:c.execute('DELETE FROM merchant_accounts')
            self.save()
            client=self.prepare_client([page])
            with patch('accounts.BeroSession',return_value=client):result=accounts.connect()
            client.request.assert_called_once_with('/login')
            self.assertEqual(result['status'],'manual_required')
            self.assertFalse(result['session_saved'])
            self.assertIsNone(accounts.maintain())

    def test_login_failure_and_cooldown(self):
        self.save();client=self.prepare_client([LOGIN,LOGIN])
        with patch('accounts.BeroSession',return_value=client):result=accounts.connect()
        self.assertEqual(result['status'],'login_failed')
        with self.assertRaises(ValueError):accounts.connect()
        self.assertIsNone(accounts.maintain())

    def test_network_error_redacts_secrets(self):
        self.save()
        client=self.prepare_client([]);client.request.side_effect=RuntimeError('private-password secret-cookie-value')
        with patch('accounts.BeroSession',return_value=client):result=accounts.connect()
        self.assertEqual(result['status'],'error')
        self.assertNotIn('private-password',json.dumps(result))
        self.assertNotIn('secret-cookie-value',json.dumps(result))

    def test_credential_change_discards_session_and_stale_operation(self):
        self.login()
        accounts.save_credentials(self.credentials|{'password':'changed-password'})
        self.assertFalse(accounts.public_status()['session_saved'])
        self.assertEqual(accounts.public_status()['last_verified'],0)
        client=self.prepare_client([AUTHENTICATED])
        def change_account(*args):
            accounts.disconnect()
            accounts.save_credentials(self.credentials|{'email':'new@example.com'})
            return AUTHENTICATED
        client.request.side_effect=change_account
        with patch('accounts.BeroSession',return_value=client):
            with self.assertRaises(ValueError):accounts.connect()
        self.assertEqual(accounts.public_status()['email'],'new@example.com')
        self.assertFalse(accounts.public_status()['session_saved'])

    def test_cross_host_redirect_never_forwards_cookie_or_password(self):
        conn=MagicMock();response=conn.getresponse.return_value
        response.status=302;response.getheader.return_value='https://other.example/login'
        response.headers=Message()
        jar=accounts.BeroSession([COOKIE])
        with patch('accounts.public_ips',return_value=['1.1.1.1']),patch('accounts.PinnedHTTPS',return_value=conn):
            with self.assertRaises(accounts.LoginIssue):jar.request('/login',{'password':'private-password'})
        self.assertEqual(conn.request.call_count,1)
        conn.close.assert_called_once()

    def test_transport_extracts_and_reuses_cookie(self):
        conn=MagicMock();response=conn.getresponse.return_value
        response.status=200
        response.headers=Message()
        response.headers.add_header('Set-Cookie','bero_session=transport-cookie; Path=/; Secure; HttpOnly')
        response.getheader.return_value='text/html; charset=utf-8'
        response.read.return_value=LOGIN.encode()
        client=accounts.BeroSession()
        with patch('accounts.public_ips',return_value=['1.1.1.1']),patch('accounts.PinnedHTTPS',return_value=conn):
            client.request('/login')
            client.request('/login',{'_token':'fresh-csrf','email':'account@example.com','password':'private-password'})
        self.assertEqual(client.snapshot()[0]['value'],'transport-cookie')
        headers=conn.request.call_args.kwargs['headers']
        self.assertEqual(headers['Cookie'],'bero_session=transport-cookie')
        self.assertEqual(headers['Origin'],'https://bero-host.de')
        self.assertEqual(conn.request.call_args.args[0],'POST')
        self.assertEqual(conn.close.call_count,2)

    def test_api_auth_and_secret_redaction(self):
        client=app.test_client()
        self.assertEqual(client.get('/api/accounts/bero').status_code,401)
        with client.session_transaction() as s:s.update(admin=True,csrf='test-csrf')
        headers={'X-CSRF-Token':'test-csrf'}
        self.assertEqual(client.put('/api/accounts/bero',json=self.credentials).status_code,403)
        response=client.put('/api/accounts/bero',json=self.credentials,headers=headers)
        self.assertEqual(response.status_code,200)
        self.assertNotIn('private-password',response.text)
        self.assertNotIn('private-password',client.get('/api/accounts/bero').text)
        login=self.prepare_client([LOGIN,AUTHENTICATED,AUTHENTICATED])
        with patch('accounts.BeroSession',return_value=login):
            response=client.post('/api/accounts/bero/connect',json={},headers=headers)
        self.assertEqual(response.json['status'],'authenticated')
        self.assertNotIn('secret-cookie-value',response.text)
        self.assertEqual(client.delete('/api/accounts/bero',json={},headers=headers).status_code,200)
        self.assertEqual(accounts.public_status()['status'],'not_configured')
