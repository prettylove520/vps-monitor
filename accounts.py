"""BERO's normal email/password login and encrypted, persistent cookie sessions.

This module never submits an order. Challenges fail closed and require human action.
"""
import http.cookiejar
import json
import time
from types import SimpleNamespace
from contextlib import contextmanager
from urllib.parse import urlsplit, urljoin, urlencode
from urllib.request import Request
from bs4 import BeautifulSoup
from network import PinnedHTTPS, public_ips, validate_url
from notifications import cipher
from store import db

ORIGIN='https://bero-host.de'

with db() as c:
    c.execute('''CREATE TABLE IF NOT EXISTS merchant_accounts (
        merchant TEXT PRIMARY KEY, credentials TEXT NOT NULL, session TEXT,
        status TEXT NOT NULL, message TEXT NOT NULL, last_verified REAL NOT NULL DEFAULT 0,
        next_check REAL NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
        lease REAL NOT NULL DEFAULT 0)''')

SESSION_LOST=('blocked','manual_required','expired','login_failed')

class LoginIssue(ValueError):
    def __init__(self,status,message):
        super().__init__(message)
        self.status=status

def decode(encoded):
    return json.loads(cipher().decrypt(encoded.encode()).decode())

def encode(value):
    return cipher().encrypt(json.dumps(value,ensure_ascii=False).encode()).decode()

def account_row():
    with db() as c:
        row=c.execute("SELECT * FROM merchant_accounts WHERE merchant='bero'").fetchone()
    return dict(row) if row else None

def public_status():
    row=account_row()
    if not row:
        return {'email':'','password_saved':False,'session_saved':False,'auto_relogin':False,
                'status':'not_configured','message':'尚未连接 BERO 账户','last_verified':0,'busy':False}
    credentials=decode(row['credentials'])
    return {'email':credentials['email'],'password_saved':bool(credentials['password']),
            'session_saved':bool(row['session']),'auto_relogin':credentials['auto_relogin'],
            'status':row['status'],'message':row['message'],'last_verified':row['last_verified'],
            'busy':row['lease']>time.time()}

def save_credentials(data):
    if not isinstance(data,dict):
        raise ValueError('账户设置格式不正确')
    email=data.get('email','')
    password=data.get('password','')
    auto=data.get('auto_relogin',False)
    if (not isinstance(email,str) or not isinstance(password,str) or not isinstance(auto,bool)
            or len(email)>254 or len(password)>500):
        raise ValueError('邮箱、密码或自动登录设置格式不正确')
    email=email.strip()
    if email.count('@')!=1 or any(ch.isspace() for ch in email) or '.' not in email.split('@')[-1] or not email.split('@')[0]:
        raise ValueError('请填写 BERO 登录邮箱')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute("SELECT * FROM merchant_accounts WHERE merchant='bero'").fetchone()
        previous=decode(old['credentials']) if old else {}
        if not password:
            if previous.get('email')!=email or not previous.get('password'):
                raise ValueError('第一次配置或更换邮箱时，请填写密码')
            password=previous['password']
        credentials={'email':email,'password':password,'auto_relogin':auto}
        changed=any(credentials[k]!=previous.get(k) for k in ('email','password'))
        if old and not changed:
            c.execute("UPDATE merchant_accounts SET credentials=?,revision=revision+1,lease=0 WHERE merchant='bero'",(encode(credentials),))
        else:
            c.execute('''INSERT INTO merchant_accounts(merchant,credentials,status,message)
                VALUES ('bero',?,'credentials_saved','凭证已保存，请点击验证登录')
                ON CONFLICT(merchant) DO UPDATE SET credentials=excluded.credentials,session=NULL,
                status=excluded.status,message=excluded.message,last_verified=0,next_check=0,
                revision=revision+1,lease=0''',(encode(credentials),))
    return public_status()

def disconnect():
    with db() as c:
        c.execute("DELETE FROM merchant_accounts WHERE merchant='bero'")

COOKIE_FIELDS=('version','name','value','port','port_specified','domain','domain_specified',
               'domain_initial_dot','path','path_specified','secure','expires','discard',
               'comment','comment_url','rfc2109')

class BeroSession:
    def __init__(self,saved=None):
        self.timeout=8
        self.jar=http.cookiejar.CookieJar()
        for item in saved or []:
            if item.get('domain','').lstrip('.')!='bero-host.de':
                continue
            self.jar.set_cookie(http.cookiejar.Cookie(**{k:item[k] for k in COOKIE_FIELDS},rest=item.get('rest',{})))
        self.jar.clear_expired_cookies()

    def snapshot(self):
        return [dict({k:getattr(cookie,k) for k in COOKIE_FIELDS},rest=cookie._rest)
                for cookie in self.jar if cookie.domain.lstrip('.')=='bero-host.de' and not cookie.is_expired()]

    def request(self,path,fields=None,*,json_payload=None,referer=None):
        url=urljoin(ORIGIN,path)
        if fields is not None and json_payload is not None:
            raise ValueError('请求正文类型冲突')
        is_json=json_payload is not None
        body=json.dumps(json_payload,ensure_ascii=False).encode() if is_json else urlencode(fields).encode() if fields is not None else None
        method='POST' if body is not None else 'GET'
        for _ in range(3):
            p=validate_url(url)
            if p.hostname!='bero-host.de':
                raise LoginIssue('blocked','登录跳转到其他网站，请人工检查；未转发账号密码或 Cookie')
            req=Request(url,data=body,method=method,headers={'User-Agent':'VPS-Monitor/1.0',
                'Accept':'application/json' if is_json else 'text/html','Accept-Encoding':'identity'})
            if body is not None:
                req.add_header('Content-Type','application/json' if is_json else 'application/x-www-form-urlencoded')
                req.add_header('Origin',ORIGIN)
                if referer and validate_url(referer).hostname!='bero-host.de':
                    raise ValueError('禁止向其他网站发送会话')
                req.add_header('Referer',referer or ORIGIN+'/login')
                if is_json:
                    req.add_header('X-Livewire','')
            self.jar.add_cookie_header(req)
            conn=PinnedHTTPS('bero-host.de',public_ips('bero-host.de')[0])
            conn.timeout=self.timeout
            try:
                conn.request(method,(p.path or '/')+('?' +p.query if p.query else ''),body=body,headers=dict(req.header_items()))
                response=conn.getresponse()
                self.jar.extract_cookies(SimpleNamespace(info=lambda:response.headers),req)
                if response.status in (401,403,429):
                    raise LoginIssue('blocked','BERO 拒绝访问或限制登录，请人工处理；自动登录已停止')
                if response.status in (301,302,303,307,308):
                    if is_json:
                        raise LoginIssue('manual_required','结账请求发生跳转，已停止，请检查登录状态')
                    target=response.getheader('Location')
                    if not target:
                        raise LoginIssue('error','登录跳转缺少地址')
                    target=urljoin(url,target)
                    if validate_url(target).hostname!='bero-host.de':
                        raise LoginIssue('blocked','登录跳转到其他网站，已停止，不会转发密码或 Cookie')
                    if method=='POST' and response.status in (307,308):
                        raise LoginIssue('blocked','登录提交要求重复转发，请人工检查')
                    if method=='POST':
                        method,body='GET',None
                    url=target
                    continue
                if response.status!=200 or ('application/json' if is_json else 'text/html') not in response.getheader('Content-Type',''):
                    raise LoginIssue('error','BERO 登录页面返回异常，请稍后再验证')
                content=response.read(2_000_001)
                if len(content)>2_000_000:
                    raise LoginIssue('error','登录页面过大，停止解析')
                decoded=content.decode('utf-8-sig')
                return json.loads(decoded) if is_json else decoded
            finally:
                conn.close()
        raise LoginIssue('error','BERO 登录跳转过多，请人工检查')

def inspect_login(html):
    soup=BeautifulSoup(html,'html.parser')
    login_forms=[form for form in soup.select('form') if form.select_one('input[name="email"]') and form.select_one('input[name="password"]')]
    logout=False
    for element in soup.select('a[href],form[action]'):
        target=urlsplit(urljoin(ORIGIN,element.get('href') or element.get('action','')))
        if target.hostname=='bero-host.de' and target.path.rstrip('/')=='/logout':
            logout=True
    if logout and not login_forms:
        return 'authenticated',None
    if (soup.select_one('.g-recaptcha,[name="g-recaptcha-response"],[name="cf-turnstile-response"],[data-sitekey]')
            or 'just a moment' in soup.get_text(' ',strip=True).lower()
            or 'sorry, you have been blocked' in soup.get_text(' ',strip=True).lower()):
        raise LoginIssue('manual_required','登录需要验证码或安全验证，请人工完成；程序不会绕过验证')
    if soup.select_one('input[autocomplete="one-time-code"],input[name="otp"],input[name="totp"],input[name="two_factor_code"],input[name="code"]'):
        raise LoginIssue('manual_required','BERO 要求二步验证，请人工完成；自动登录已停止')
    if len(login_forms)!=1:
        raise LoginIssue('manual_required','无法确认登录状态，页面可能已变化；请人工核实')
    form=login_forms[0]
    action=urljoin(ORIGIN,form.get('action',''))
    if action!=ORIGIN+'/login' or form.get('method','').lower()!='post':
        raise LoginIssue('manual_required','登录表单地址已变化，停止提交凭证')
    token=form.select_one('input[name="_token"]')
    if not token or not token.get('value'):
        raise LoginIssue('error','登录页面缺少会话验证参数')
    return 'login',token['value']

def connect(*, interactive=True):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute("SELECT * FROM merchant_accounts WHERE merchant='bero'").fetchone()
        if not row:
            raise ValueError('请先保存 BERO 邮箱和密码')
        row=dict(row)
        if row['lease']>time.time():
            raise ValueError('正在验证登录，请稍候')
        if interactive and row['status'] in ('login_failed','blocked','manual_required') and row['next_check']>time.time():
            raise ValueError('请先处理登录问题，至少 5 分钟后再验证；修改凭证可重新尝试')
        c.execute("UPDATE merchant_accounts SET lease=? WHERE merchant='bero'",(time.time()+120,))
    status,message,session='error','登录验证失败，请检查网络后再试',row['session']
    last=row['last_verified']
    interval=300
    try:
        credentials=decode(row['credentials'])
        client=BeroSession(decode(session) if session else None)
        state,token=inspect_login(client.request('/login'))
        reused=state=='authenticated'
        if state!='authenticated':
            session=None
            if not interactive and not credentials['auto_relogin']:
                raise LoginIssue('expired','登录会话已失效，请点击验证登录；未启用自动重新登录')
            fields={'_token':token,'email':credentials['email'],'password':credentials['password'],
                    'remember':'on','redirect_to_after_login':''}
            state,_=inspect_login(client.request('/login',fields))
            if state!='authenticated':
                raise LoginIssue('login_failed','邮箱或密码未被接受，请核对凭证；不会反复尝试登录')
            state,_=inspect_login(client.request('/login'))
            if state!='authenticated':
                raise LoginIssue('manual_required','登录后会话无法复用，请人工检查')
        saved=client.snapshot()
        if not saved:
            raise LoginIssue('manual_required','登录页面未建立可保存的 Cookie，会话无法确认')
        status='authenticated'
        message='已复用保存的登录会话' if reused else '已登录并加密保存会话；未下单或付款'
        session=encode(saved)
        last=time.time()
        interval=1800
    except LoginIssue as error:
        status,message=error.status,str(error)
        if status in ('blocked','manual_required','expired','login_failed'):
            session=None
    except Exception:
        # Do not expose credentials, cookies, HTML responses or network exception text.
        pass
    with db() as c:
        updated=c.execute('''UPDATE merchant_accounts SET status=?,message=?,session=?,last_verified=?,
            next_check=?,lease=0 WHERE merchant='bero' AND revision=? AND credentials=?''',
            (status,message,session,last,time.time()+interval,row['revision'],row['credentials'])).rowcount
    if not updated:
        raise ValueError('验证期间账户设置发生变化，请重新验证')
    return public_status()

def usable():
    """A saved session that can be tried. A transient network 'error' (site busy during a restock)
    must not stop an armed purchase; real login problems clear the session, so they fail here."""
    row=account_row()
    return bool(row and row['session'] and row['last_verified'] and row['status'] in ('authenticated','error'))

def maintain():
    row=account_row()
    if (not row or not row['session'] or not row['last_verified'] or row['next_check']>time.time()
            or row['lease']>time.time() or row['status'] not in ('authenticated','error')):
        return None
    result=connect(interactive=False)
    if result['status']!='authenticated' and result['status']!=row['status']:
        return result['message']
    return None

@contextmanager
def checkout_session():
    """Lend the verified account session to one checkout preflight, serially."""
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute("SELECT * FROM merchant_accounts WHERE merchant='bero'").fetchone()
        if not row or not row['session'] or not row['last_verified'] or row['status'] not in ('authenticated','error'):
            raise ValueError('先到商家账户验证 BERO 登录')
        row=dict(row)
        if row['lease']>time.time():
            raise ValueError('商家会话正在使用，请稍后预检')
        c.execute("UPDATE merchant_accounts SET lease=? WHERE merchant='bero'",(time.time()+300,))
    client=None
    try:
        client=BeroSession(decode(row['session']))
        state,token=inspect_login(client.request('/login'))
        if state!='authenticated':
            credentials=decode(row['credentials'])
            if not credentials['auto_relogin']:
                raise LoginIssue('expired','登录会话已失效，预检停止；请重新验证登录')
            state,_=inspect_login(client.request('/login',{'_token':token,'email':credentials['email'],
                'password':credentials['password'],'remember':'on','redirect_to_after_login':''}))
            if state!='authenticated':
                raise LoginIssue('login_failed','自动重新登录失败，预检停止；请人工核对账户')
            state,_=inspect_login(client.request('/login'))
            if state!='authenticated':
                raise LoginIssue('manual_required','登录会话无法复用，预检停止')
        yield client
        current=account_row()
        if not current or current['revision']!=row['revision'] or current['credentials']!=row['credentials']:
            raise ValueError('账户设置在预检期间发生变化，请重新验证')
    except LoginIssue as error:
        # Only a real login problem discards the saved session. A busy or failing site (HTTP 503,
        # timeouts) says nothing about the login, so keep the session and status for the next try.
        if error.status in SESSION_LOST:
            with db() as c:
                c.execute("UPDATE merchant_accounts SET session=NULL,status=?,message=?,lease=0,next_check=? WHERE merchant='bero' AND revision=? AND credentials=?",
                          (error.status,str(error),time.time()+300,row['revision'],row['credentials']))
        raise
    finally:
        # Preserve refreshed cookies, without overwriting a changed/deleted account.
        with db() as c:
            c.execute("UPDATE merchant_accounts SET session=?,lease=0 WHERE merchant='bero' AND revision=? AND credentials=? AND session IS NOT NULL",
                      (encode(client.snapshot()) if client else row['session'],row['revision'],row['credentials']))
