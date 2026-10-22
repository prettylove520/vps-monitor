"""HTTPS-only public-address fetch with DNS pinning; no cookies or login sessions."""
import base64
import contextvars
import http.client
import ipaddress
import socket
import ssl
import struct
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urljoin

class FetchError(Exception):
    def __init__(self, message, blocked=False, retry_after=0):
        super().__init__(message)
        self.blocked, self.retry_after = blocked, retry_after

def validate_url(url):
    try:
        p = urlsplit(url)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443) or p.fragment:
            raise ValueError()
        if any(ord(ch) < 33 for ch in url) or len(url) > 1500:
            raise ValueError()
        host = p.hostname.encode('idna').decode()
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global:
                raise ValueError()
        except ValueError:
            if ':' in host or host.replace('.', '').isdigit() or host in ('localhost',) or '.' not in host:
                raise ValueError()
        return p
    except (ValueError, UnicodeError):
        raise FetchError('仅支持公开网站的 HTTPS 地址（443 端口，无账号密码）') from None

def public_ips(host):
    try:
        ips = list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    except OSError:
        raise FetchError('域名解析失败') from None
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise FetchError('禁止访问内网、回环或保留地址', blocked=True)
    return ips

class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, ip):
        super().__init__(host, timeout=20, context=ssl.create_default_context())
        self.ip = ip

    def connect(self):
        sock = socket.create_connection((self.ip, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise

# Optional per-task outbound proxy (the person's own HTTP / SOCKS5 proxy, e.g. a home connection). The worker sets
# it around one task's checks, so the many fetch() call sites do not need an extra argument.
PROXY = contextvars.ContextVar('proxy', default='')

class ProxyError(OSError):
    pass

def parse_proxy(value):
    """'http://[user:pass@]host:port' or 'socks5://[user:pass@]host:port' -> (scheme, host, port, user, password)."""
    try:
        p = urlsplit(value)
        if p.scheme not in ('http', 'socks5') or not p.hostname or not p.port or p.path not in ('', '/') or p.query or p.fragment:
            raise ValueError()
        if len(value) > 300 or any(ord(ch) < 33 for ch in value):
            raise ValueError()
        return p.scheme, p.hostname, p.port, p.username or '', p.password or ''
    except ValueError:
        raise ValueError('代理地址格式不正确，应为 http://主机:端口 或 socks5://主机:端口（可带 用户:密码@）') from None

def _recv(sock, n):
    data = b''
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ProxyError('代理连接被关闭')
        data += chunk
    return data

class Socks5HTTPS(http.client.HTTPSConnection):
    def __init__(self, host, proxy):
        super().__init__(host, timeout=20, context=ssl.create_default_context())
        self.proxy = proxy

    def connect(self):
        _, phost, pport, user, password = self.proxy
        sock = socket.create_connection((phost, pport), timeout=self.timeout)
        try:
            host = self.host.encode()
            sock.sendall(b'\x05\x02\x00\x02' if user else b'\x05\x01\x00')
            version, method = _recv(sock, 2)
            if version != 5 or method == 0xFF:
                raise ProxyError('SOCKS5 代理拒绝了连接方式')
            if method == 2:
                u, pw = user.encode(), password.encode()
                sock.sendall(b'\x01' + bytes([len(u)]) + u + bytes([len(pw)]) + pw)
                if _recv(sock, 2)[1] != 0:
                    raise ProxyError('SOCKS5 代理用户名或密码不正确')
            sock.sendall(b'\x05\x01\x00\x03' + bytes([len(host)]) + host + struct.pack('>H', 443))
            head = _recv(sock, 4)
            if head[1] != 0:
                raise ProxyError('SOCKS5 代理无法连接目标网站（代码 %d）' % head[1])
            if head[3] == 3:
                _recv(sock, _recv(sock, 1)[0] + 2)
            else:
                _recv(sock, {1: 4, 4: 16}.get(head[3], 0) + 2)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise

def _connection(host, ip, proxy):
    if not proxy:
        return PinnedHTTPS(host, ip)
    scheme, phost, pport, user, password = proxy
    if scheme == 'socks5':
        return Socks5HTTPS(host, proxy)
    conn = http.client.HTTPSConnection(phost, pport, timeout=20, context=ssl.create_default_context())
    headers = {}
    if user:
        headers['Proxy-Authorization'] = 'Basic ' + base64.b64encode(('%s:%s' % (user, password)).encode()).decode()
    conn.set_tunnel(host, 443, headers)
    return conn

class Page:
    """Result of a single fetch: status 200 with html, or a same-site redirect (follow=False)."""
    def __init__(self, url, status, html='', location=''):
        self.url, self.status, self.html, self.location = url, status, html, location

def fetch(url, follow=True, kind='html'):
    proxy = parse_proxy(PROXY.get()) if PROXY.get() else None
    for _ in range(4):
        p = validate_url(url)
        host = p.hostname.encode('idna').decode()
        ip = None if proxy else public_ips(host)[0]  # with a proxy, it resolves the name
        conn = _connection(host, ip, proxy)
        try:
            conn.request('GET', (p.path or '/') + ('?' + p.query if p.query else ''), headers={
                'User-Agent': 'VPS-Monitor/1.0', 'Accept': 'application/json' if kind == 'json' else 'text/html', 'Accept-Encoding': 'identity'})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location:
                    raise FetchError('重定向缺少地址')
                new = urljoin(url, location)
                if validate_url(new).hostname != p.hostname:
                    raise FetchError('网站跳转到其他域名，请人工检查', blocked=True)
                if not follow:
                    return Page(url, response.status, location=new)
                url = new
                continue
            if response.status in (401, 403):
                raise FetchError('访问被拒绝，任务已暂停（HTTP %s）%s' % (
                    response.status, '' if proxy else '；如果网站按 IP 拦截，可在任务里设置自己的代理出口'), blocked=True)
            if response.status != 200:
                retry = response.getheader('Retry-After', '')
                try:
                    seconds = float(retry) if retry.isdigit() else parsedate_to_datetime(retry).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    seconds = 0
                raise FetchError('网站返回 HTTP %s' % response.status, retry_after=max(0, seconds))
            wanted = 'json' if kind == 'json' else 'text/html'
            if wanted not in response.getheader('Content-Type', ''):
                raise FetchError('返回内容不是 JSON 数据' if kind == 'json' else '返回内容不是 HTML 页面')
            body = response.read(3_000_001)
            if len(body) > 3_000_000:
                raise FetchError('页面超过 3 MB，停止解析')
            return Page(url, 200, html=body.decode('utf-8-sig'))
        except FetchError:
            raise
        except ProxyError as e:
            raise FetchError('代理不可用：%s' % e) from None
        except (OSError, UnicodeError, http.client.HTTPException):
            raise FetchError('连接超时、TLS 错误或页面编码不支持') from None
        finally:
            conn.close()
    raise FetchError('页面重定向次数过多')

def get_html(url):
    return fetch(url).html
