import hmac
import json
import os
import secrets
import time
from datetime import timedelta
from functools import wraps
from flask import Flask, jsonify, request, session, send_from_directory
from werkzeug.security import check_password_hash
import adapters
import notifications
import purchases
import accounts
import checkout
import merchants
import twofactor
import stock
import updates
import updater
from version import VERSION
from network import FetchError
from store import db, setting, secret, task_dict, log

app=Flask(__name__,static_folder='static')
app.config.update(SECRET_KEY=secret('session.key'),MAX_CONTENT_LENGTH=262144,
    SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Strict',
    SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE','1')=='1',PERMANENT_SESSION_LIFETIME=timedelta(hours=8))

def auth(fn):
    @wraps(fn)
    def inner(*args,**kwargs):
        if not session.get('admin'):
            return jsonify(error='请先登录'),401
        return fn(*args,**kwargs)
    return inner

@app.before_request
def csrf():
    if request.method in ('POST','PUT','DELETE','PATCH'):
        if request.content_type!='application/json':
            return jsonify(error='需要 JSON 请求'),415
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),session.get('csrf') or '!missing'):
            return jsonify(error='会话已过期，请刷新页面'),403

@app.after_request
def headers(response):
    response.headers['Cache-Control']='no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response

@app.errorhandler(ValueError)
@app.errorhandler(FetchError)
def invalid(error):
    return jsonify(error=str(error)),400

@app.errorhandler(404)
def missing(error):
    return jsonify(error='未找到该内容'),404

@app.get('/')
def index():
    return send_from_directory('static','index.html')

@app.get('/healthz')
def health():
    return jsonify(application='vps-watch',version=VERSION)

@app.get('/api/session')
def current():
    session.setdefault('csrf',secrets.token_urlsafe(32))
    return jsonify(authenticated=bool(session.get('admin')),csrf=session['csrf'])

@app.post('/api/login')
def login():
    password=(request.get_json() or {}).get('password','')
    if not isinstance(password,str) or len(password)>300:
        raise ValueError('密码格式错误')
    ip=request.remote_addr or 'unknown'
    now=time.time()
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM attempts WHERE until<?',(now,))
        r=c.execute('SELECT * FROM attempts WHERE ip=?',(ip,)).fetchone()
        if r and r['count']>=5:
            return jsonify(error='尝试次数过多，请 15 分钟后再试'),429
        c.execute('INSERT INTO attempts VALUES (?,1,?) ON CONFLICT(ip) DO UPDATE SET count=count+1',(ip,now+900))
    stored=setting('admin_password')
    if not stored or not check_password_hash(stored,password):
        return jsonify(error='密码不正确或尚未初始化管理员'),401
    if twofactor.enabled():
        code=(request.get_json() or {}).get('code','')
        if not code:
            return jsonify(error='请输入手机验证器中的 6 位验证码',totp_required=True),401
        if not twofactor.verify(code):
            return jsonify(error='验证码不正确或已使用过',totp_required=True),401
    with db() as c:
        c.execute('DELETE FROM attempts WHERE ip=?',(ip,))
    session.clear()
    session.update(admin=True,csrf=secrets.token_urlsafe(32))
    session.permanent=True
    return jsonify(ok=True,csrf=session['csrf'])

@app.get('/api/security')
@auth
def security_status():
    return jsonify(totp_enabled=twofactor.enabled())

@app.post('/api/security/totp/setup')
@auth
def totp_setup():
    return jsonify(twofactor.begin_setup())

@app.post('/api/security/totp/enable')
@auth
def totp_enable():
    twofactor.finish_setup((request.get_json() or {}).get('code',''))
    log(None,'info','已启用管理员两步验证')
    return jsonify(totp_enabled=True)

@app.post('/api/security/totp/disable')
@auth
def totp_disable():
    data=request.get_json() or {}
    stored=setting('admin_password')
    password=data.get('password','')
    if not isinstance(password,str) or not stored or not check_password_hash(stored,password):
        raise ValueError('管理员密码不正确')
    twofactor.disable(data.get('code',''))
    log(None,'warning','已关闭管理员两步验证')
    return jsonify(totp_enabled=False)

@app.get('/api/update')
@auth
def update_state():
    return jsonify(version=VERSION,latest=updates.status(),can_apply=updater.available(),progress=updater.status(),
                   armed=purchases.armed_count())

@app.post('/api/update/check')
@auth
def update_check():
    updates.check_now()
    return update_state()

@app.post('/api/update/apply')
@auth
def update_apply():
    password=(request.get_json() or {}).get('password','')
    stored=setting('admin_password')
    if not isinstance(password,str) or not stored or not check_password_hash(stored,password):
        raise ValueError('管理员密码不正确')
    latest=updater.request()
    log(None,'warning','已从网页发起更新到 v%s'%latest['version'])
    return jsonify(ok=True,progress=updater.status())

@app.post('/api/logout')
@auth
def logout():
    session.clear()
    return jsonify(ok=True)

@app.get('/api/dashboard')
@auth
def dashboard():
    with db() as c:
        tasks=[task_dict(r) for r in c.execute('SELECT * FROM tasks ORDER BY id DESC')]
        logs=[dict(r) for r in c.execute('SELECT logs.*,json_extract(tasks.config,\'$.name\') task_name FROM logs LEFT JOIN tasks ON tasks.id=logs.task_id ORDER BY logs.id DESC LIMIT 100')]
    for task in tasks:
        task['purchase_policy']=purchases.policy_for(task['id'])
        task['checkout_preflight']=checkout.latest(task['id'])
        task['purchase_attempt']=purchases.attempt_for(task['id'])
        task['checkout_supported']=bool(getattr(merchants.REGISTRY.get(task['config']['adapter']),'SUPPORTS_CHECKOUT',False))
        task['stock']=stock.summary(task['id'])
    heartbeat=float(setting('worker_heartbeat','0'))
    return jsonify(tasks=tasks,logs=logs,presets=adapters.PRESETS,version=VERSION,update=updates.status(),update_can_apply=updater.available(),
                   worker_online=time.time()-heartbeat<180,worker_heartbeat=heartbeat,
                   notifications_configured=bool(notifications.configured_channels()),
                   telegram_configured=bool(setting('telegram_token') and setting('telegram_chat')))

@app.post('/api/tasks')
@auth
def create():
    cfg=adapters.validate(request.get_json())
    with db() as c:
        if c.execute('SELECT count(*) FROM tasks').fetchone()[0]>=100:
            raise ValueError('最多支持 100 个任务')
        id=c.execute('INSERT INTO tasks(config,created) VALUES (?,?)',(json.dumps(cfg,ensure_ascii=False),time.time())).lastrowid
    log(id,'info','任务已创建，默认暂停；请先检查规则')
    return jsonify(id=id),201

EXPORT_FORMAT='vps-watch-tasks'

@app.get('/api/tasks/export')
@auth
def export_tasks():
    """Task settings only: no account credentials, notification keys, purchase settings or history."""
    with db() as c:
        configs=[json.loads(r[0]) for r in c.execute('SELECT config FROM tasks ORDER BY id')]
    return jsonify(format=EXPORT_FORMAT,version=VERSION,exported=time.time(),tasks=configs)

@app.post('/api/tasks/import')
@auth
def import_tasks():
    data=request.get_json() or {}
    if not isinstance(data,dict) or data.get('format')!=EXPORT_FORMAT or not isinstance(data.get('tasks'),list):
        raise ValueError('不是 VPS Monitor 导出的任务文件')
    if not data['tasks']:
        raise ValueError('文件里没有任务')
    configs=[]
    for i,item in enumerate(data['tasks'],1):
        try:
            configs.append(adapters.validate(item))
        except (ValueError,FetchError,KeyError,TypeError,AttributeError) as error:
            raise ValueError('第 %d 个任务无效：%s'%(i,error)) from None
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        existing={(json.loads(r[0])['adapter'],json.dumps(json.loads(r[0]),sort_keys=True)) for r in c.execute('SELECT config FROM tasks')}
        fresh=[cfg for cfg in configs if (cfg['adapter'],json.dumps(cfg,sort_keys=True)) not in existing]
        if len(existing)+len(fresh)>100:
            raise ValueError('导入后超过 100 个任务')
        for cfg in fresh:
            c.execute('INSERT INTO tasks(config,created) VALUES (?,?)',(json.dumps(cfg,ensure_ascii=False),time.time()))
    log(None,'info','已导入 %d 个任务（默认暂停），跳过 %d 个重复任务'%(len(fresh),len(configs)-len(fresh)))
    return jsonify(imported=len(fresh),skipped=len(configs)-len(fresh))

@app.get('/api/tasks/<int:id>/stock-history')
@auth
def stock_history(id):
    return jsonify(summary=stock.summary(id),restocks=stock.restocks(id)[:50])

@app.put('/api/tasks/<int:id>')
@auth
def edit(id):
    cfg=adapters.validate(request.get_json())
    with db() as c:
        count=c.execute("UPDATE tasks SET config=?,enabled=0,queued=0,lease=0,revision=revision+1,status='pending',result=NULL,failures=0,notified=0,next_check=0 WHERE id=?",(json.dumps(cfg,ensure_ascii=False),id)).rowcount
        c.execute('DELETE FROM purchase_policies WHERE task_id=?',(id,))
        c.execute('DELETE FROM checkout_preflights WHERE task_id=?',(id,))
        c.execute('DELETE FROM task_notifications WHERE task_id=?',(id,))
    if not count:
        return jsonify(error='任务不存在'),404
    log(id,'info','配置已更新并暂停；请重新检查')
    return jsonify(ok=True)

@app.put('/api/tasks/<int:id>/purchase-policy')
@auth
def purchase_policy(id):
    policy=purchases.save_policy(id,request.get_json())
    log(id,'info','购买设置已保存；自动付款需单独启用')
    return jsonify(policy=policy)

@app.post('/api/tasks/<int:id>/purchase/arm')
@auth
def arm_purchase(id):
    policy=purchases.arm(id,(request.get_json() or {}).get('confirm_total'))
    log(id,'warning','已启用自动付款：补货时用商家账户余额自动下单一次，上限 %s %s'%(policy['max_total'],policy.get('currency','')))
    return jsonify(policy=policy)

@app.post('/api/tasks/<int:id>/purchase/disarm')
@auth
def disarm_purchase(id):
    policy=purchases.disarm(id)
    if not policy:
        raise ValueError('尚未保存购买设置')
    log(id,'info','已关闭自动付款')
    return jsonify(policy=policy)

@app.post('/api/tasks/<int:id>/purchase-attempts/clear')
@auth
def clear_purchase_attempts(id):
    purchases.clear_attempts(id,(request.get_json() or {}).get('confirm'))
    log(id,'info','已清除购买记录；可以重新启用自动付款')
    return jsonify(ok=True)

@app.post('/api/tasks/<int:id>/checkout-preflight')
@auth
def enqueue_preflight(id):
    checkout.enqueue(id)
    return jsonify(state='queued'),202

@app.get('/api/tasks/<int:id>/checkout-history')
@auth
def checkout_history(id):
    return jsonify(checkout.history(id))

@app.get('/api/tasks/<int:id>/checkout-preflight')
@auth
def preflight_status(id):
    return jsonify(checkout.latest(id))

@app.post('/api/tasks/<int:id>/<action>')
@auth
def action(id,action):
    if action not in ('start','pause','check'):
        raise ValueError('未知操作')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM tasks WHERE id=?',(id,)).fetchone()
        if not row:
            return jsonify(error='任务不存在'),404
        if action=='pause':
            c.execute('UPDATE tasks SET enabled=0,queued=0,revision=revision+1,lease=0 WHERE id=?',(id,))
            c.execute('UPDATE task_notifications SET revision=(SELECT revision FROM tasks WHERE id=?) WHERE task_id=?',(id,id))
        else:
            if row['lease']>time.time():
                return jsonify(error='任务正在检查，请稍候'),409
            last=row['last_check'] or 0
            due=max(time.time(),last+15,row['next_check'] if row['status']=='error' else 0)
            if action=='start':
                c.execute('UPDATE tasks SET enabled=1,next_check=? WHERE id=?',(due,id))
            else:
                c.execute('UPDATE tasks SET queued=1,next_check=? WHERE id=?',(due,id))
    log(id,'info',{'start':'已启用监控','pause':'已暂停监控','check':'已加入单次检查队列'}[action])
    return jsonify(ok=True)

@app.delete('/api/tasks/<int:id>')
@auth
def delete(id):
    with db() as c:
        c.execute('DELETE FROM tasks WHERE id=?',(id,))
    return jsonify(ok=True)

@app.get('/api/settings')
@auth
def settings():
    return jsonify(chat_id=setting('telegram_chat'),token_saved=bool(setting('telegram_token')),
                   enabled=setting('telegram_enabled','1')=='1')

@app.put('/api/settings')
@auth
def update_settings():
    data=request.get_json() or {}
    token=data.get('token','')
    chat=data.get('chat_id','')
    if not isinstance(token,str) or not isinstance(chat,str):
        raise ValueError('设置格式不正确')
    notifications.save(token.strip(),chat.strip(),data.get('enabled'))
    return jsonify(ok=True)

@app.post('/api/settings/test')
@auth
def test_notification():
    notifications.send_telegram('VPS Monitor 测试通知：连接正常。此消息不代表补货或已下单。')
    return jsonify(ok=True)

@app.get('/api/settings/bark')
@auth
def bark_settings():
    return jsonify(notifications.bark_settings())

@app.put('/api/settings/bark')
@auth
def update_bark_settings():
    notifications.save_bark(request.get_json())
    return jsonify(ok=True)

@app.post('/api/settings/bark/test')
@auth
def test_bark_notification():
    notifications.send_bark('Bark 测试提醒：请确认手机收到并按设置响铃。此消息不代表补货或已下单。')
    return jsonify(ok=True)

@app.get('/api/settings/wechat')
@auth
def wechat_settings():
    return jsonify(notifications.wechat_settings())

@app.put('/api/settings/wechat')
@auth
def update_wechat_settings():
    notifications.save_wechat(request.get_json())
    return jsonify(notifications.wechat_settings())

@app.post('/api/settings/wechat/test')
@auth
def test_wechat_notification():
    channel=(request.get_json() or {}).get('channel')
    if channel not in notifications.WECHAT:
        raise ValueError('请选择微信推送渠道')
    notifications.send_wechat(channel,'VPS Monitor 测试通知\n连接正常。此消息不代表补货或已下单。')
    return jsonify(ok=True)

@app.delete('/api/settings/wechat/<channel>')
@auth
def delete_wechat_settings(channel):
    notifications.delete_wechat(channel)
    return jsonify(notifications.wechat_settings())

@app.get('/api/accounts/bero')
@auth
def bero_account_status():
    return jsonify(accounts.public_status())

@app.put('/api/accounts/bero')
@auth
def save_bero_account():
    result=accounts.save_credentials(request.get_json())
    log(None,'info','BERO 账户设置已保存；没有下单或付款')
    return jsonify(result)

@app.post('/api/accounts/bero/connect')
@auth
def connect_bero_account():
    result=accounts.connect()
    log(None,'info' if result['status']=='authenticated' else 'warning','BERO 登录：'+result['message'])
    return jsonify(result)

@app.delete('/api/accounts/bero')
@auth
def disconnect_bero_account():
    accounts.disconnect()
    log(None,'info','已删除本程序保存的 BERO 凭证和会话；未注销其他设备')
    return jsonify(ok=True)

if __name__=='__main__':
    app.run(host='127.0.0.1',port=int(os.environ.get('PORT','18080')),debug=False)
