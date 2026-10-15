"""v0.5.9: optional per-task outbound proxy (HTTP CONNECT and SOCKS5)."""
import base64
import socket
import threading
import unittest
import test_app
from test_v04 import AppCase
from test_app import CFG
import adapters
import network
from network import FetchError, PROXY


class FakeProxy:
    """Accepts one connection, records what the client asked for, answers like a proxy, then hangs up
    (the TLS handshake with the target then fails, which is all these tests need)."""
    def __init__(self, kind, reject=False):
        self.kind, self.reject, self.seen = kind, reject, b''
        self.server = socket.socket()
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        conn, _ = self.server.accept()
        conn.settimeout(3)
        try:
            if self.kind == 'http':
                data = b''
                while b'\r\n\r\n' not in data:
                    data += conn.recv(4096)
                self.seen = data
                conn.sendall(b'HTTP/1.1 403 Forbidden\r\n\r\n' if self.reject else b'HTTP/1.1 200 Connection established\r\n\r\n')
            else:
                greeting = conn.recv(16)
                self.seen = greeting
                if greeting[2:] == b'\x00\x02':  # username/password offered
                    conn.sendall(b'\x05\x02')
                    auth = conn.recv(64)
                    self.seen += auth
                    conn.sendall(b'\x01\x00')
                else:
                    conn.sendall(b'\x05\x00')
                request = conn.recv(64)
                self.seen += request
                conn.sendall(b'\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00' if self.reject else b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00')
        except OSError:
            pass
        finally:
            conn.close()
            self.server.close()


class ParseTests(unittest.TestCase):
    def test_accepts_http_and_socks5_with_optional_credentials(self):
        self.assertEqual(network.parse_proxy('socks5://u:p@10.0.0.2:1080'), ('socks5', '10.0.0.2', 1080, 'u', 'p'))
        self.assertEqual(network.parse_proxy('http://proxy.example.com:8080'), ('http', 'proxy.example.com', 8080, '', ''))

    def test_rejects_other_shapes(self):
        for bad in ('ftp://h:1', 'socks5://h', 'http://h:80/path', 'http://h:80 x', 'h:80', 'socks5://:1', 'http://h:80?x=1'):
            with self.assertRaises(ValueError, msg=bad):
                network.parse_proxy(bad)


class ProxyFetchTests(unittest.TestCase):
    def fetch_through(self, proxy_url):
        token = PROXY.set(proxy_url)
        try:
            with self.assertRaises(FetchError) as caught:
                network.fetch('https://app.vmiss.com/store/us-los-angeles-tri')
            return caught.exception
        finally:
            PROXY.reset(token)

    def test_http_proxy_gets_a_connect_with_credentials(self):
        proxy = FakeProxy('http')
        self.fetch_through('http://user:secret@127.0.0.1:%d' % proxy.port)
        proxy.thread.join(5)
        self.assertTrue(proxy.seen.startswith(b'CONNECT app.vmiss.com:443 HTTP/1.1'))
        self.assertIn(b'Proxy-Authorization: Basic ' + base64.b64encode(b'user:secret'), proxy.seen)

    def test_socks5_proxy_is_asked_for_the_hostname_not_a_local_ip(self):
        proxy = FakeProxy('socks5')
        self.fetch_through('socks5://127.0.0.1:%d' % proxy.port)
        proxy.thread.join(5)
        self.assertIn(b'\x05\x01\x00\x03' + bytes([len('app.vmiss.com')]) + b'app.vmiss.com\x01\xbb', proxy.seen)  # ATYP domain, port 443

    def test_socks5_username_password(self):
        proxy = FakeProxy('socks5')
        self.fetch_through('socks5://me:pw@127.0.0.1:%d' % proxy.port)
        proxy.thread.join(5)
        self.assertIn(b'\x01\x02me\x02pw', proxy.seen)

    def test_a_refusing_proxy_is_reported_as_proxy_problem(self):
        proxy = FakeProxy('socks5', reject=True)
        error = self.fetch_through('socks5://127.0.0.1:%d' % proxy.port)
        self.assertIn('代理', str(error))
        self.assertFalse(error.blocked)  # a broken proxy must not pause the task as "blocked by the site"

    def test_unreachable_proxy(self):
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
        self.assertIsInstance(self.fetch_through('socks5://127.0.0.1:%d' % port), FetchError)


class ProxyConfigTests(AppCase):
    def test_saved_with_the_task_but_never_exported(self):
        self.login()
        r = self.req('/api/tasks', data=CFG | {'proxy': 'socks5://u:p@10.0.0.2:1080'})
        self.assertEqual(r.status_code, 201, r.json)
        task = self.client.get('/api/dashboard').json['tasks'][0]
        self.assertEqual(task['config']['proxy'], 'socks5://u:p@10.0.0.2:1080')
        exported = self.client.get('/api/tasks/export').json
        self.assertNotIn('proxy', exported['tasks'][0])
        self.assertNotIn('10.0.0.2', str(exported))

    def test_bad_proxy_is_rejected_and_empty_is_ignored(self):
        self.login()
        self.assertEqual(self.req('/api/tasks', data=CFG | {'proxy': 'nonsense'}).status_code, 400)
        self.assertEqual(self.req('/api/tasks', data=CFG | {'proxy': '  '}).status_code, 201)
        self.assertNotIn('proxy', adapters.validate(CFG | {'proxy': ''}))


if __name__ == '__main__':
    unittest.main()
