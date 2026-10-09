import base64
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, unquote
from cryptography.fernet import Fernet
from store import secret, setting, set_setting, db
from network import validate_url, public_ips, PinnedHTTPS, FetchError

BARK_DEFAULTS={'enabled':False,'level':'critical','volume':8,'repeat':True,'sound':'alarm'}
CHANNEL_LABELS={'telegram':'Telegram','bark':'Bark'}

def cipher():
    return Fernet(base64.urlsafe_b64encode(bytes.fromhex(secret('encryption.key'))))

def credentials():
    encoded=setting('telegram_token')
    return (cipher().decrypt(encoded.encode()).decode() if encoded else '', setting('telegram_chat'))

def save(token, chat, enabled=None):
    if enabled is not None and not isinstance(enabled,bool):
        raise ValueError('启用状态格式不正确')
    if not re.fullmatch(r'-?\d{1,25}|@[A-Za-z0-9_]{3,64}',chat):
        raise ValueError('聊天 ID 格式不正确')
    if token:
        if not re.fullmatch(r'\d{5,20}:[A-Za-z0-9_-]{20,120}',token):
            raise ValueError('Bot Token 格式不正确')
        set_setting('telegram_token',cipher().encrypt(token.encode()).decode())
    set_setting('telegram_chat',chat)
    if enabled is not None:
        set_setting('telegram_enabled','1' if enabled else '0')

def send_telegram(message):
    token,chat=credentials()
    if not token or not chat:
        raise ValueError('尚未配置 Telegram，请到通知设置填写')
    request=urllib.request.Request('https://api.telegram.org/bot'+token+'/sendMessage',data=urlencode({
        'chat_id':chat,'text':message[:3500],'disable_web_page_preview':'true'}).encode())
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*a,**k):
            return None
    try:
        with urllib.request.build_opener(NoRedirect()).open(request,timeout=15) as r:
            if not json.loads(r.read(65536)).get('ok'):
                raise ValueError()
    except Exception:
        raise ValueError('Telegram 发送失败，请检查 Token、聊天 ID、网络及机器人 /start') from None

def bark_settings():
    encoded=setting('bark_endpoint')
    server='https://api.day.app'
    if encoded:
        host,_=parse_bark_url(cipher().decrypt(encoded.encode()).decode())
        server='https://'+host
    return BARK_DEFAULTS | json.loads(setting('bark_options','{}')) | {
        'server_url':server,'key_saved':bool(encoded),'url_saved':bool(encoded)}

def parse_bark_url(url):
    try:
        p=validate_url(url)
        parts=p.path.strip('/').split('/')
        key=unquote(parts[-1])
        if p.query or len(parts)!=1 or not re.fullmatch(r'[A-Za-z0-9_-]{8,200}',key):
            raise ValueError()
        return p.hostname.encode('idna').decode(),key
    except (FetchError,ValueError,UnicodeError):
        raise ValueError('请填写 Bark 的完整推送地址：https://服务器域名/设备Key；只含 Key，不含标题、正文或参数') from None

def save_bark(data):
    if not isinstance(data,dict):
        raise ValueError('Bark 设置格式不正确')
    current=bark_settings()
    options={k:data.get(k,current[k]) for k in BARK_DEFAULTS}
    if (not isinstance(options['enabled'],bool) or not isinstance(options['repeat'],bool)
            or options['level'] not in ('critical','timeSensitive','active')
            or type(options['volume']) is not int or not 0<=options['volume']<=10
            or not isinstance(options['sound'],str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',options['sound'])):
        raise ValueError('Bark 设置不正确：音量为 0–10，铃声填写 Bark 中的名称')
    url=data.get('url','')
    if not isinstance(url,str):
        raise ValueError('Bark 推送地址格式不正确')
    url=url.strip()
    encoded=setting('bark_endpoint')
    if 'server_url' in data or 'device_key' in data:
        if url:
            raise ValueError('请分别填写服务器地址和设备 Key')
        server=data.get('server_url',current['server_url'])
        key=data.get('device_key','')
        if not isinstance(server,str) or not isinstance(key,str):
            raise ValueError('Bark 服务器地址或设备 Key 格式不正确')
        try:
            p=validate_url(server.strip())
            if p.path not in ('','/') or p.query:
                raise ValueError()
            host=p.hostname.encode('idna').decode()
        except (FetchError,ValueError,UnicodeError):
            raise ValueError('服务器地址填写 https://api.day.app 或自建服务的 HTTPS 域名，不要带 Key 或路径') from None
        key=key.strip()
        if not key and encoded:
            old_host,key=parse_bark_url(cipher().decrypt(encoded.encode()).decode())
            if old_host!=host:
                raise ValueError('更换 Bark 服务器时，请同时填写新服务器的设备 Key')
        if key:
            if not re.fullmatch(r'[A-Za-z0-9_-]{8,200}',key):
                raise ValueError('设备 Key 格式不正确，请只填写 Key')
            encoded=cipher().encrypt(('https://'+host+'/'+key).encode()).decode()
    elif url:
        host,key=parse_bark_url(url)
        encoded=cipher().encrypt(('https://'+host+'/'+key).encode()).decode()
    if options['enabled'] and not encoded:
        raise ValueError('先填写设备 Key，才能启用 Bark')
    with db() as c:
        for key,value in (('bark_endpoint',encoded),('bark_options',json.dumps(options))):
            c.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))

def send_bark(message, *, urgent=True, url=''):
    encoded=setting('bark_endpoint')
    if not encoded:
        raise ValueError('尚未配置 Bark，请到通知设置填写')
    conn=None
    try:
        host,key=parse_bark_url(cipher().decrypt(encoded.encode()).decode())
        options=bark_settings()
        payload={'device_key':key,'title':'VPS Watch','body':message[:3500],
                 'group':'VPS Watch','level':options['level'] if urgent else 'active',
                 'sound':options['sound']}
        if urgent:
            if options['level']=='critical':
                payload['volume']=str(options['volume'])
            if options['repeat']:
                payload['call']='1'
        if url:
            validate_url(url)
            payload['url']=url
        # Pin the connection to a public DNS result and keep hostname TLS verification.
        conn=PinnedHTTPS(host,public_ips(host)[0])
        conn.timeout=15
        conn.request('POST','/push',body=json.dumps(payload,ensure_ascii=False).encode(),
                     headers={'Content-Type':'application/json; charset=utf-8','User-Agent':'VPS-Watch/1.0'})
        response=conn.getresponse()
        body=response.read(65537)
        if response.status!=200 or len(body)>65536 or json.loads(body).get('code')!=200:
            raise ValueError()
    except Exception:
        # Never include the secret push URL, key, response body or network exception in logs.
        raise ValueError('Bark 发送失败，请检查推送地址、设备 Key 和网络；服务端接收不代表手机已响铃') from None
    finally:
        if conn:
            conn.close()

def configured_channels():
    channels=[]
    if setting('telegram_token') and setting('telegram_chat') and setting('telegram_enabled','1')=='1':
        channels.append('telegram')
    options=bark_settings()
    if options['url_saved'] and options['enabled']:
        channels.append('bark')
    return channels

def send(message, *, delivered=(), urgent=True, url=''):
    channels=configured_channels()
    if not channels:
        raise ValueError('尚未启用通知，请到通知设置连接 Telegram 或 Bark')
    sent=set(delivered)
    failures={}
    pending=[channel for channel in channels if channel not in sent]
    # A failed or slow provider must not prevent the other provider from receiving the alert.
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs={channel:pool.submit(send_telegram,message) if channel=='telegram'
              else pool.submit(send_bark,message,urgent=urgent,url=url) for channel in pending}
        for channel,job in jobs.items():
            try:
                job.result()
                sent.add(channel)
            except Exception:
                failures[channel]=CHANNEL_LABELS[channel]+' 发送失败，请检查该渠道配置或网络'
    return {'sent':sorted(sent),'failed':failures,'complete':all(c in sent for c in channels)}
