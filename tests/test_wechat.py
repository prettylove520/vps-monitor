import json
import unittest
from unittest.mock import patch
import test_app  # noqa: F401
from app import app
from store import db, setting
import notifications

PUSHPLUS='0123456789abcdef0123456789abcdef'
WECOM='693a91f6-7xxx-4bc4-97a0-0ec2sifa5aaa'.replace('x','0').replace('s','5').replace('i','1')
WECOM_URL='https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key='+WECOM

class WechatTests(unittest.TestCase):
    def setUp(self):
        with db() as c:c.execute("DELETE FROM settings WHERE key LIKE 'push_%'")
        self.client=app.test_client()
        with self.client.session_transaction() as s:s.update(admin=True,csrf='csrf')

    def put(self,data):
        return self.client.put('/api/settings/wechat',json=data,headers={'X-CSRF-Token':'csrf'})

    def test_validation_and_encryption(self):
        for channel,bad in (('pushplus','short'),('serverchan','not-a-key'),('wecom','https://evil.example/send?key='+WECOM),('wecom','abc')):
            self.assertEqual(self.put({'channel':channel,'token':bad,'enabled':True}).status_code,400,(channel,bad))
        self.assertEqual(self.put({'channel':'pushplus','token':'','enabled':True}).status_code,400)
        self.assertEqual(self.put({'channel':'nope','token':PUSHPLUS,'enabled':True}).status_code,400)
        r=self.put({'channel':'pushplus','token':PUSHPLUS,'enabled':True})
        self.assertEqual(r.status_code,200);self.assertTrue(r.json['pushplus']['enabled'])
        self.assertNotIn(PUSHPLUS,json.dumps(r.json));self.assertNotIn(PUSHPLUS,setting('push_pushplus'))
        r=self.put({'channel':'wecom','token':WECOM_URL,'enabled':False})
        self.assertTrue(r.json['wecom']['saved']);self.assertFalse(r.json['wecom']['enabled'])
        self.assertEqual(notifications._wechat_saved('wecom')['secret'],WECOM)
        # empty token keeps the saved secret
        self.put({'channel':'wecom','token':'','enabled':True})
        self.assertEqual(notifications._wechat_saved('wecom')['secret'],WECOM)
        self.assertEqual(set(notifications.configured_channels())&{'pushplus','wecom'},{'pushplus','wecom'})
        self.assertEqual(self.client.get('/api/dashboard').json['notifications_configured'],True)
        r=self.client.delete('/api/settings/wechat/wecom',json={},headers={'X-CSRF-Token':'csrf'})
        self.assertFalse(r.json['wecom']['saved'])

    def test_requests_go_to_official_hosts(self):
        cases=[('pushplus',PUSHPLUS,{'code':200},('www.pushplus.plus','/send')),
               ('serverchan','SCT12345abcdefgh',{'code':0},('sctapi.ftqq.com','/SCT12345abcdefgh.send')),
               ('serverchan','sctp7788tABCDEFGH12',{'code':0},('7788.push.ft07.com','/send/sctp7788tABCDEFGH12.send')),
               ('wecom',WECOM,{'errcode':0},('qyapi.weixin.qq.com','/cgi-bin/webhook/send?key='+WECOM))]
        for channel,token,reply,(host,path) in cases:
            self.put({'channel':channel,'token':token,'enabled':True})
            with patch('notifications._post',return_value=reply) as post:
                notifications.send_wechat(channel,'补货提示：测试\n第二行',url='https://example.com/p')
            self.assertEqual(post.call_args.args[:2],(host,path),channel)
            payload=post.call_args.args[2]
            self.assertIn('https://example.com/p',json.dumps(payload,ensure_ascii=False))

    def test_failure_hides_secret_and_send_dispatches(self):
        self.put({'channel':'pushplus','token':PUSHPLUS,'enabled':True})
        with patch('notifications._post',side_effect=OSError(PUSHPLUS)):
            with self.assertRaises(ValueError) as ctx:notifications.send_wechat('pushplus','x')
        self.assertNotIn(PUSHPLUS,str(ctx.exception))
        with patch('notifications._post',return_value={'code':500}):
            with self.assertRaises(ValueError):notifications.send_wechat('pushplus','x')
        with patch('notifications.configured_channels',return_value=['pushplus']),patch('notifications._post',return_value={'code':200}):
            report=notifications.send('hello')
        self.assertEqual(report['sent'],['pushplus']);self.assertTrue(report['complete'])

    def test_test_endpoint(self):
        h={'X-CSRF-Token':'csrf'}
        self.assertEqual(self.client.post('/api/settings/wechat/test',json={'channel':'pushplus'},headers=h).status_code,400)
        self.put({'channel':'pushplus','token':PUSHPLUS,'enabled':True})
        with patch('notifications._post',return_value={'code':200}):
            self.assertEqual(self.client.post('/api/settings/wechat/test',json={'channel':'pushplus'},headers=h).status_code,200)

if __name__=='__main__':unittest.main()
