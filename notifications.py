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
CHANNEL_LABELS={'telegram':'Telegram','bark':'Bark','pushplus':'PushPlus','serverchan':'Server酱','wecom':'企业微信'}

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
        with urllib.request.build_opener(NoRedirect()).open(request,timeout=10) as r:
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
        payload={'device_key':key,'title':'VPS Monitor','body':message[:3500],
                 'group':'VPS Monitor','level':options['level'] if urgent else 'active',
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
        conn.timeout=10
        conn.request('POST','/push',body=json.dumps(payload,ensure_ascii=False).encode(),
                     headers={'Content-Type':'application/json; charset=utf-8','User-Agent':'VPS-Monitor/1.0'})
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

# ---------------------------------------------------------------- WeChat channels
# PushPlus / Server酱 / 企业微信群机器人. Each stores one secret (token / SendKey / webhook key),
# encrypted like the Telegram token, and is posted to a fixed official host.
WECHAT={
 'pushplus':{'label':'PushPlus','pattern':r'[A-Za-z0-9]{16,64}','hint':'PushPlus 的 token（32 位左右的字母数字）'},
 'serverchan':{'label':'Server酱','pattern':r'SCT[A-Za-z0-9]{8,80}|sctp\d+t[A-Za-z0-9]{8,80}','hint':'Server酱 的 SendKey（以 SCT 或 sctp 开头）'},
 'wecom':{'label':'企业微信','pattern':r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}','hint':'企业微信群机器人的 Webhook 地址或其中的 key'},
}

def _wechat_saved(name):
    encoded=setting('push_'+name)
    if not encoded:
        return None
    return json.loads(cipher().decrypt(encoded.encode()).decode())

def wechat_settings():
    result={}
    for name,meta in WECHAT.items():
        saved=_wechat_saved(name)
        result[name]={'label':meta['label'],'saved':bool(saved),'enabled':bool(saved and saved['enabled']),'hint':meta['hint']}
    return result

def _normalise_secret(name,value):
    value=value.strip()
    if name=='wecom' and value.startswith('https://'):
        match=re.fullmatch(r'https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=([0-9a-fA-F-]{36})',value)
        if not match:
            raise ValueError('请粘贴企业微信群机器人的 Webhook 地址（qyapi.weixin.qq.com），或只填其中的 key')
        value=match[1]
    if not re.fullmatch(WECHAT[name]['pattern'],value):
        raise ValueError('格式不正确：请填写'+WECHAT[name]['hint'])
    return value

def save_wechat(data):
    if not isinstance(data,dict) or data.get('channel') not in WECHAT:
        raise ValueError('请选择微信推送渠道')
    name=data['channel']
    token=data.get('token','')
    enabled=data.get('enabled')
    if not isinstance(token,str) or len(token)>300 or not isinstance(enabled,bool):
        raise ValueError('微信推送设置格式不正确')
    saved=_wechat_saved(name) or {}
    secret_value=_normalise_secret(name,token) if token.strip() else saved.get('secret')
    if not secret_value:
        if enabled:
            raise ValueError('先填写%s，才能启用'%WECHAT[name]['hint'])
        set_setting('push_'+name,'')
        return
    set_setting('push_'+name,cipher().encrypt(json.dumps({'secret':secret_value,'enabled':enabled}).encode()).decode())

def delete_wechat(name):
    if name not in WECHAT:
        raise ValueError('未知渠道')
    set_setting('push_'+name,'')

def _post(host,path,payload,*,form=False):
    conn=PinnedHTTPS(host,public_ips(host)[0])
    conn.timeout=10
    try:
        body=urlencode(payload).encode() if form else json.dumps(payload,ensure_ascii=False).encode()
        conn.request('POST',path,body=body,headers={'User-Agent':'VPS-Monitor/1.0',
            'Content-Type':'application/x-www-form-urlencoded' if form else 'application/json; charset=utf-8'})
        response=conn.getresponse()
        content=response.read(65537)
        if response.status!=200 or len(content)>65536:
            raise ValueError()
        return json.loads(content)
    finally:
        conn.close()

def send_wechat(name,message,*,url=''):
    saved=_wechat_saved(name)
    if not saved:
        raise ValueError('尚未配置%s'%WECHAT[name]['label'])
    secret_value=saved['secret']
    title=message.split('\n',1)[0][:60] or 'VPS Monitor'
    text=message[:3500]+('\n'+url if url and url not in message else '')
    try:
        if name=='pushplus':
            ok=_post('www.pushplus.plus','/send',{'token':secret_value,'title':title,'content':text,'template':'txt'}).get('code')==200
        elif name=='serverchan':
            match=re.fullmatch(r'sctp(\d+)t.*',secret_value)
            host='%s.push.ft07.com'%match[1] if match else 'sctapi.ftqq.com'
            path=('/send/%s.send' if match else '/%s.send')%secret_value
            ok=_post(host,path,{'title':title,'desp':text},form=True).get('code')==0
        else:
            ok=_post('qyapi.weixin.qq.com','/cgi-bin/webhook/send?key='+secret_value,
                     {'msgtype':'text','text':{'content':text}}).get('errcode')==0
        if not ok:
            raise ValueError()
    except Exception:
        # Never include the token, response body or network exception text.
        raise ValueError('%s 发送失败，请检查%s和网络'%(WECHAT[name]['label'],WECHAT[name]['hint'])) from None

def configured_channels():
    channels=[]
    if setting('telegram_token') and setting('telegram_chat') and setting('telegram_enabled','1')=='1':
        channels.append('telegram')
    options=bark_settings()
    if options['url_saved'] and options['enabled']:
        channels.append('bark')
    for name in WECHAT:
        saved=_wechat_saved(name)
        if saved and saved['enabled']:
            channels.append(name)
    return channels

def send(message, *, delivered=(), urgent=True, url=''):
    channels=configured_channels()
    if not channels:
        raise ValueError('尚未启用通知，请到通知设置连接 Telegram 或 Bark')
    sent=set(delivered)
    failures={}
    pending=[channel for channel in channels if channel not in sent]
    # A failed or slow provider must not prevent the other provider from receiving the alert.
    def deliver(channel):
        if channel=='telegram':
            return send_telegram(message)
        if channel=='bark':
            return send_bark(message,urgent=urgent,url=url)
        return send_wechat(channel,message,url=url)
    with ThreadPoolExecutor(max_workers=max(1,len(pending))) as pool:
        jobs={channel:pool.submit(deliver,channel) for channel in pending}
        for channel,job in jobs.items():
            try:
                job.result()
                sent.add(channel)
            except Exception:
                failures[channel]=CHANNEL_LABELS[channel]+' 发送失败，请检查该渠道配置或网络'
    return {'sent':sorted(sent),'failed':failures,'complete':all(c in sent for c in channels)}
