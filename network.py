"""HTTPS-only public-address fetch with DNS pinning; no cookies or login sessions."""
import http.client
import ipaddress
import socket
import ssl
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

class Page:
    """Result of a single fetch: status 200 with html, or a same-site redirect (follow=False)."""
    def __init__(self, url, status, html='', location=''):
        self.url, self.status, self.html, self.location = url, status, html, location

def fetch(url, follow=True):
    for _ in range(4):
        p = validate_url(url)
        host = p.hostname.encode('idna').decode()
        ip = public_ips(host)[0]
        conn = PinnedHTTPS(host, ip)
        try:
            conn.request('GET', (p.path or '/') + ('?' + p.query if p.query else ''), headers={
                'User-Agent': 'VPS-Watch/1.0', 'Accept': 'text/html', 'Accept-Encoding': 'identity'})
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
                raise FetchError('访问被拒绝，任务已暂停（HTTP %s）' % response.status, blocked=True)
            if response.status != 200:
                retry = response.getheader('Retry-After', '')
                try:
                    seconds = float(retry) if retry.isdigit() else parsedate_to_datetime(retry).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    seconds = 0
                raise FetchError('网站返回 HTTP %s' % response.status, retry_after=max(0, seconds))
            if 'text/html' not in response.getheader('Content-Type', ''):
                raise FetchError('返回内容不是 HTML 页面')
            body = response.read(3_000_001)
            if len(body) > 3_000_000:
                raise FetchError('页面超过 3 MB，停止解析')
            return Page(url, 200, html=body.decode('utf-8-sig'))
        except FetchError:
            raise
        except (OSError, UnicodeError, http.client.HTTPException):
            raise FetchError('连接超时、TLS 错误或页面编码不支持') from None
        finally:
            conn.close()
    raise FetchError('页面重定向次数过多')

def get_html(url):
    return fetch(url).html
