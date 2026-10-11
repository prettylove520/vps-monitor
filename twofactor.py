"""Optional TOTP two-factor login for the admin (RFC 6238, SHA-1, 6 digits, 30 s)."""
import base64
import hashlib
import hmac
import io
import json
import secrets
import struct
import time
from urllib.parse import quote
from notifications import cipher
from store import setting, set_setting

ISSUER='VPS Monitor'

def code_at(secret,counter):
    key=base64.b32decode(secret+'='*(-len(secret)%8))
    digest=hmac.new(key,struct.pack('>Q',counter),hashlib.sha1).digest()
    offset=digest[-1]&0x0F
    value=struct.unpack('>I',digest[offset:offset+4])[0]&0x7FFFFFFF
    return '%06d'%(value%1_000_000)

def _load(key):
    encoded=setting(key)
    return json.loads(cipher().decrypt(encoded.encode()).decode()) if encoded else None

def _save(key,value):
    set_setting(key,cipher().encrypt(json.dumps(value).encode()).decode() if value else '')

def enabled():
    data=_load('admin_totp')
    return bool(data and data.get('enabled'))

def _match(secret,code,now=None):
    """Return the matching time-step counter (±1 step for clock drift), or None."""
    if not isinstance(code,str):
        return None
    code=code.strip().replace(' ','')
    if len(code)!=6 or not code.isdigit():
        return None
    counter=int((now or time.time())//30)
    for step in (counter,counter-1,counter+1):
        if hmac.compare_digest(code_at(secret,step),code):
            return step
    return None

def verify(code,now=None):
    """Check a login code; each code is accepted only once (replay protection)."""
    data=_load('admin_totp')
    if not data or not data.get('enabled'):
        return True
    step=_match(data['secret'],code,now)
    if step is None or step<=data.get('last',-1):
        return False
    _save('admin_totp',data|{'last':step})
    return True

def qr_svg(text):
    import qrcode
    import qrcode.image.svg
    image=qrcode.make(text,image_factory=qrcode.image.svg.SvgPathImage,box_size=8,border=2)
    buffer=io.BytesIO()
    image.save(buffer)
    return 'data:image/svg+xml;base64,'+base64.b64encode(buffer.getvalue()).decode()

def begin_setup(account='admin'):
    if enabled():
        raise ValueError('两步验证已经启用；如需更换，请先关闭')
    secret=base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')
    _save('admin_totp_pending',{'secret':secret,'created':time.time()})
    uri='otpauth://totp/%s:%s?secret=%s&issuer=%s'%(quote(ISSUER),quote(account),secret,quote(ISSUER))
    return {'secret':secret,'uri':uri,'qr':qr_svg(uri)}

def finish_setup(code):
    pending=_load('admin_totp_pending')
    if not pending or time.time()-pending['created']>900:
        raise ValueError('绑定已过期，请重新开始')
    step=_match(pending['secret'],code)
    if step is None:
        raise ValueError('验证码不正确，请确认手机时间准确后重试')
    _save('admin_totp',{'secret':pending['secret'],'enabled':True,'last':step})
    _save('admin_totp_pending',None)

def disable(code):
    if not enabled():
        return
    if not verify(code):
        raise ValueError('验证码不正确')
    _save('admin_totp',None)

def force_disable():
    """Recovery from the server shell (manage.py disable-2fa)."""
    _save('admin_totp',None)
    _save('admin_totp_pending',None)
